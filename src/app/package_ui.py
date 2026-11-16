"""Declarative native forms with a small read/preview-only RPC boundary.

Import does not read a registry, credentials or files. UI sessions never authorize
execution. Returned previews require a separate explicit host confirmation.
"""
from __future__ import annotations

import copy
from collections import deque
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time

from app.plugin_manifest import canonical_json, validate_ui, validate_ui_value
from app.plugin_store import PluginError, _contains_credential_key

MAX_MESSAGE = 65536
MAX_SESSIONS = 128
MAX_REQUESTS = 600
TTL_SECONDS = 300
REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{8,100}$')
LIKELY_KEY = re.compile(r'(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\b(?:bearer|api[_-]?key|access[_-]?token)\s*[:= ]\s*\S{8,})')


def _safe(value):
    """Do not serialize probable secrets even when hidden under an innocent key."""
    if _contains_credential_key(value):
        raise PluginError('ui_secret_material', 'UI 不接受或显示密钥字段；仅使用普通非敏感配置', 422)
    if isinstance(value, str) and LIKELY_KEY.search(value):
        raise PluginError('ui_secret_material', 'UI 不接受或显示疑似真实 Key', 422)
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PluginError('ui_invalid_value', 'UI 对象key必须是字符串', 422)
            _safe(child)
    elif isinstance(value, list):
        for child in value:
            _safe(child)
    try:
        canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise PluginError('ui_invalid_value', 'UI 只接受有限 JSON 数据', 422) from exc


def _binding(row):
    return {'id': row['id'], 'revision': row['revision'], 'digest': row['digest']}


class PackageUI:
    """Single-host-process bounded session store; restart invalidates every session."""
    def __init__(self, manager, *, clock=time.monotonic, ttl=TTL_SECONDS):
        if type(ttl) not in (int, float) or not 1 <= ttl <= 600:
            raise ValueError('UI TTL invalid')
        self.manager, self.clock, self.ttl = manager, clock, ttl
        self._sessions = {}
        self._lock = threading.RLock()

    def _live(self, db, plugin_id, expected_revision=None):
        manager = self.manager
        row = manager._row(db, plugin_id, installed=True)
        if expected_revision is not None:
            manager._expected(row, expected_revision)
        manager._ready(row)
        manifest = json.loads(row['manifest'])
        config_only='config_schema' in manifest and 'ui' not in manifest
        if not row['enabled'] and not config_only:
            raise PluginError('ui_disabled', '插件包已停用；UI 会话不可用', 409)
        blocks = [] if config_only else manager._dependency_blocks(db, manifest, enabled=True)
        if blocks:
            raise PluginError('ui_dependencies', '插件依赖不可用；UI 会话失效', 409, blocks)
        if 'ui' not in manifest and 'config_schema' not in manifest:
            raise PluginError('ui_not_declared', '插件包没有声明式 UI', 404)
        from app.plugin_manifest import config_form
        schema = validate_ui(manifest['ui'], manifest['tools']) if 'ui' in manifest else config_form(manifest['config_schema'])
        _safe(schema)
        return row, schema

    def _purge(self):
        now = self.clock()
        for sid in list(self._sessions):
            if self._sessions[sid]['expires'] <= now:
                self._sessions.pop(sid, None)

    def open(self, plugin_id, expected_revision):
        with self._lock, self.manager._lock, self.manager._db() as db:
            self._purge()
            if len(self._sessions) >= MAX_SESSIONS:
                raise PluginError('ui_session_limit', 'UI 会话过多，请等待过期或重启页面', 429)
            row, schema = self._live(db, plugin_id, expected_revision)
            session_id, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[session_id] = {'binding': _binding(row), 'nonce_hash': hashlib.sha256(nonce.encode()).hexdigest(),
                'expires': self.clock() + self.ttl, 'next': 1, 'requests': set(), 'times': deque()}
            return {'session_id': session_id, 'nonce': nonce, 'expires_at': time.time() + self.ttl,
                'next_sequence': 1, 'binding': _binding(row), 'schema': copy.deepcopy(schema),
                'approval_required': True, 'allowed_methods': ['schema', 'read_values', 'preview_action'],
                'note': '此会话不是执行批准。只使用宿主原生表单；没有 HTML/JS、文件/网络/进程/eval RPC。'}

    def close(self):
        with self._lock:
            self._sessions.clear()

    def _envelope(self, plugin_id, message):
        if not isinstance(message, dict) or set(message) != {'session_id', 'nonce', 'sequence', 'request_id', 'method', 'params'}:
            raise PluginError('ui_message_shape', 'UI RPC 字段必须完整且不能包含未知字段', 422)
        try:
            encoded_size = len(canonical_json(message).encode('utf-8'))
        except (TypeError, ValueError, RecursionError) as exc:
            raise PluginError('ui_message_shape', 'UI RPC 不是有限 JSON', 422) from exc
        if encoded_size > MAX_MESSAGE:
            raise PluginError('ui_message_size', 'UI RPC 超过容量限制', 413)
        sid, nonce = message['session_id'], message['nonce']
        if not isinstance(sid, str) or not 20 <= len(sid) <= 100 or not isinstance(nonce, str) or not 20 <= len(nonce) <= 100:
            raise PluginError('ui_session_invalid', 'UI 会话标识无效', 403)
        self._purge()
        session = self._sessions.get(sid)
        if not session or not secrets.compare_digest(session['nonce_hash'], hashlib.sha256(nonce.encode()).hexdigest()):
            raise PluginError('ui_session_invalid', 'UI 会话已过期或不匹配', 403)
        if session['binding']['id'] != plugin_id:
            raise PluginError('ui_cross_package', 'UI 会话不能跨插件使用', 403)
        if type(message['sequence']) is not int or message['sequence'] != session['next']:
            raise PluginError('ui_sequence', 'UI 消息顺序无效；请重新打开会话', 409)
        request_id = message['request_id']
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) or request_id in session['requests']:
            raise PluginError('ui_replay', 'UI request_id 重复或无效', 409)
        now = self.clock()
        times = session['times']
        while times and times[0] <= now - 60:
            times.popleft()
        if len(times) >= 60 or sum(value > now - 1 for value in times) >= 10 or len(session['requests']) >= MAX_REQUESTS:
            raise PluginError('ui_rate_limit', 'UI 消息超过频率或会话容量限制', 429)
        # Accepted envelopes are consumed even if method/input validation fails.
        # A retry needs a NEW sequence and request ID, never automatic replay.
        session['next'] += 1
        session['requests'].add(request_id)
        times.append(now)
        return session

    def _values(self, row, schema):
        config, state = json.loads(row['config']), json.loads(row['state'])
        _safe(config); _safe(state)
        values = {}
        for field in schema['fields']:
            value = field.get('default')
            if 'source' in field:
                kind, key = field['source'].split('.', 1)
                value = (config if kind == 'config' else state).get(key, value)
            validate_ui_value(field, value, required=False)
            _safe(value)
            values[field['id']] = copy.deepcopy(value)
        if len(canonical_json(values).encode('utf-8')) > MAX_MESSAGE:
            raise PluginError('ui_message_size', 'UI 字段值超过容量限制', 413)
        return values

    def rpc(self, plugin_id, message):
        with self._lock, self.manager._lock:
            session = self._envelope(plugin_id, message)
            with self.manager._db() as db:
                row, schema = self._live(db, plugin_id)
                if _binding(row) != session['binding']:
                    self._sessions.pop(message['session_id'], None)
                    raise PluginError('ui_stale_session', '包版本、配置或状态已变化；请重新打开 UI', 409)
                method, params = message['method'], message['params']
                if method not in ('schema', 'read_values', 'preview_action') or not isinstance(params, dict):
                    raise PluginError('ui_method', 'UI RPC 方法不允许或参数无效', 422)
                if method in ('schema', 'read_values') and params:
                    raise PluginError('ui_params', '此 UI 方法不接受参数', 422)
                if method == 'schema':
                    result = {'schema': copy.deepcopy(schema)}
                elif method == 'read_values':
                    result = {'values': self._values(row, schema)}
                else:
                    if set(params) != {'action_id', 'values'} or not isinstance(params['action_id'], str) or not isinstance(params['values'], dict):
                        raise PluginError('ui_params', 'preview_action 仅接受 action_id 和字段值对象', 422)
                    action = next((a for a in schema['actions'] if a['id'] == params['action_id']), None)
                    if action is None:
                        raise PluginError('ui_action', 'UI 动作未声明', 422)
                    declared = {f['id']: f for f in schema['fields']}
                    if set(params['values']) - declared.keys():
                        raise PluginError('ui_unknown_field', 'UI 提交包含未声明字段', 422)
                    values = self._values(row, schema)
                    for fid, value in params['values'].items():
                        field = declared[fid]
                        if field['type'] == 'readonly':
                            raise PluginError('ui_readonly', 'UI readonly 字段不能提交', 422)
                        validate_ui_value(field, value, required=field.get('required', False))
                        _safe(value); values[fid] = copy.deepcopy(value)
                    if len(canonical_json(values).encode('utf-8')) > MAX_MESSAGE:
                        raise PluginError('ui_message_size', 'UI 字段值超过容量限制', 413)
                    for fid, field in declared.items():
                        validate_ui_value(field, values.get(fid), required=field.get('required', False))
                    # End this read transaction before manager opens its own mutation
                    # preview transaction. The manager lock keeps revision stable.
                    binding, snapshot = _binding(row), dict(row)
            if method == 'preview_action':
                if action['kind'] == 'tool':
                    arguments = {name: values[fid] for name, fid in action.get('arguments', {}).items()}
                    _safe(arguments)
                    definition = next(t for t in json.loads(snapshot['manifest'])['tools'] if t['name'] == action['tool'])
                    for target in definition.get('targets', []):
                        if target['kind'] == 'path':
                            original = arguments.get(target['argument'])
                            if not isinstance(original, str) or not Path(original).is_absolute() or str(Path(original).resolve()) != original:
                                raise PluginError('ui_absolute_path_required',
                                    '声明式界面的路径目标须填写规范绝对路径；请修正后重新预览，未创建执行授权。', 422)
                    preview = self.manager.preview_call(plugin_id, action['tool'], arguments, binding['revision'])
                    # UI never silently changes a form's intent. The native package
                    # manager may normalize paths, but this stricter UI must receive
                    # already canonical absolute values before issuing an approval.
                    if preview['snapshot'].get('args') != arguments:
                        raise PluginError('ui_absolute_path_required',
                            '声明式界面的路径目标须填写规范绝对路径；相对路径或需解析的路径未获授权，请修正后重新预览。', 422)
                else:
                    kind = 'config' if action['kind'] == 'save_config' else 'state'
                    updated = json.loads(snapshot[kind])
                    for fid in params['values']:
                        field = declared[fid]
                        source = field.get('source', '')
                        if source.startswith(kind + '.'):
                            updated[source.split('.', 1)[1]] = values[fid]
                    _safe(updated)
                    preview = self.manager.preview_action(plugin_id, kind, binding['revision'], value=updated)
                result = {'preview': preview, 'execution': 'separate-explicit-user-confirmation-required',
                          'execute_path': '/v1/plugin-packages/calls/execute' if action['kind'] == 'tool' else '/v1/plugin-packages/commit'}
            return {'session_id': message['session_id'], 'request_id': message['request_id'],
                'sequence': message['sequence'], 'next_sequence': session['next'], 'binding': session['binding'], **result}
