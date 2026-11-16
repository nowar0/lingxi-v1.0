"""Package-owned MCP adapters: exact previews, bounded workers, no old catalog writes.

Production runtime selection is host-owned and strict-only; absent verified wiring
is blocked instead of starting the old subprocess worker. Explicit test adapters do
not prove an OS sandbox. A tool's side effects cannot be inferred from its schema.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading
import time

import jsonschema

from app.plugin_manifest import canonical_json
from app.plugin_runtime_selection import BlockedStrictRuntime
from app.plugin_store import PluginError, _contains_credential_key

ADAPTER = 'package-mcp-v1'
MAX_TOOLS = 100
MAX_CATALOG_BYTES = 196608
RISK = ('仅严格 AppContainer + Job stdio 单服务器链路；可用性须由真实宿主观察核验，'
        '缺少能力或权限代理即拒绝，不回退普通进程；schema 不能证明工具无副作用。')
PERSONAL_NOTE = '个人自用 MCP，使用当前用户的本地进程或指定 HTTP 服务，无需发布者信任与沙箱设置。'


def _runtime():
    """Compatibility name for a blocked strict adapter, never a subprocess factory."""
    return BlockedStrictRuntime('mcp')


class PackageMcp:
    def __init__(self, manager, runtime=None):
        self.manager = manager
        self._runtime = runtime
        self._explicit_runtime = runtime is not None
        self._runtime_lock = threading.Lock()

    @property
    def runtime(self):
        # No ManagerProxy initialization or adapter construction during __init__.
        with self._runtime_lock:
            if not self._explicit_runtime:
                # A ManagerProxy may switch ownership after authenticated host
                # setup; never cache/register the old closed manager's runtime.
                runtime = self.manager.resolve_mcp_runtime()
            else:
                runtime = self._runtime
        return self.manager.register_runtime_owner(runtime)

    def _base(self, db, plugin_id, mcp_name, expected_revision, *, allow_loaded=False):
        manager = self.manager
        row = manager._row(db, plugin_id, installed=True)
        manager._expected(row, expected_revision)
        if not row['enabled']:
            raise PluginError('disabled', '插件包已停用；未连接 MCP')
        if row['loaded'] and not manager.execution_coordinator.loaded_count(plugin_id):
            # A persisted/unowned loaded flag is unsafe; managed concurrent calls
            # are counted by their physical-resource owner instead of a bool gate.
            raise PluginError('busy', '插件包存在未归属的运行现场，请核对后再连接')
        manager._ready(row)
        manifest = json.loads(row['manifest'])
        blocks = manager._dependency_blocks(db, manifest, enabled=True)
        if blocks:
            raise PluginError('blocked', '插件包依赖不可用', details=blocks)
        declarations = [item for item in manifest.get('mcp', []) if item['name'] == mcp_name]
        if len(declarations) != 1:
            raise PluginError('unknown_mcp', 'MCP 名称不存在或重名，不能确定连接目标', 422)
        declaration = copy.deepcopy(declarations[0])
        root = (manager.root / 'code' / plugin_id / row['digest']).resolve(strict=True)
        target = {'kind': 'network', 'url': declaration.get('url'), 'redirects': False,
                  'credentials': 'none', 'environment_proxy': False}
        if declaration['transport'] == 'stdio':
            entry = root / declaration['entrypoint']
            if entry.suffix.lower() != '.py' or not entry.is_file() or entry.is_symlink() or not entry.resolve().is_relative_to(root):
                raise PluginError('unsupported_stdio', '包 MCP stdio 仅支持已哈希的包内 Python .py 入口', 422)
            target = {'kind': 'local_process', 'entrypoint': str(entry.resolve()),
                      'sha256': manifest['files'][declaration['entrypoint']],
                      'cwd': str(root), 'interpreter': 'dedicated_frozen_single_server', 'inherits_user_environment': False}
        return manager._bind_trust({'adapter': ADAPTER, 'id': plugin_id, 'expected_revision': row['revision'],
                'digest': row['digest'], 'name': manifest['name'], 'mcp_name': mcp_name,
                'declaration': declaration, 'package_root': str(root), 'target': target,
                'signature_status': manager._signature(manifest)['status'],
                'risk': PERSONAL_NOTE if manager.personal_use else RISK, 'credentials': 'none',
                'execution_mode': 'personal' if manager.personal_use else 'strict',
                'dependency_binding': manager._execution_dependency_binding(db,manifest),
                'config_fingerprint': hashlib.sha256(canonical_json(json.loads(row['config'])).encode()).hexdigest(),
                'state_fingerprint': hashlib.sha256(canonical_json(json.loads(row['state'])).encode()).hexdigest(),
                'note': '个人自用 MCP：本地进程或 HTTP 服务，30 秒调用上限。'})

    def metadata(self, plugin_id):
        with self.manager._lock, self.manager._db() as db:
            row = self.manager._row(db, plugin_id)
            manifest = json.loads(row['manifest'])
            return {'id': plugin_id, 'revision': row['revision'], 'digest': row['digest'],
                    'installed': bool(row['installed']), 'enabled': bool(row['enabled']),
                    'items': copy.deepcopy(manifest.get('mcp', [])), 'connects': False,
                    'credentials': 'none', 'risk': PERSONAL_NOTE if self.manager.personal_use else RISK}

    def _strict_preview(self, value):
        if self.manager.personal_use:
            return value
        if value['declaration']['transport'] != 'stdio':
            raise PluginError('mcp_stdio_required', 'HTTP MCP 不属于严格 stdio 沙箱；未连接', 422)
        provider = getattr(self.runtime, 'preview_spec', None)
        if not callable(provider):
            raise PluginError('sandbox_unavailable', '严格 MCP 宿主预览未接通；未启动进程或回退旧运行时', 503)
        try:
            spec = provider(copy.deepcopy(value))
        except Exception as error:
            raise PluginError(getattr(error, 'code', 'sandbox_unavailable'), '严格 MCP 无法产生准确宿主预览；未执行', 503) from None
        if (type(spec) is not dict or spec.get('mode') != 'stdio_mcp'
                or type(spec.get('call_id')) is not str or type(spec.get('fingerprint')) is not str):
            raise PluginError('sandbox_unavailable', '严格 MCP 预览规格无效；未执行', 503)
        value['strict_mcp_spec'] = copy.deepcopy(spec)
        return value

    def preview_discover(self, plugin_id, name, expected_revision):
        with self.manager._lock, self.manager._db() as db:
            value = self._base(db, plugin_id, name, expected_revision)
            value.update(action='call', mcp_operation='discover', permissions=['mcp.connect', 'mcp.discover'])
            return self.manager._issue(db, self._strict_preview(value))

    def _read_token(self, db, token, fingerprint, operation, *, consume):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise PluginError('invalid_token', 'MCP 授权 token 无效', 403)
        row = db.execute('SELECT * FROM approvals WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['used'] or row['expires'] < time.time() or row['fingerprint'] != fingerprint:
            raise PluginError('invalid_token', 'MCP 授权已过期、使用或指纹不符', 403)
        value = json.loads(row['snapshot'])
        if value.get('adapter') != ADAPTER or value.get('mcp_operation') != operation:
            raise PluginError('invalid_token', '授权不可跨 MCP 操作使用', 403)
        if consume:
            db.execute('UPDATE approvals SET used=1 WHERE token_hash=?', (row['token_hash'],))
        return value

    def _fresh(self, db, snapshot, *, allow_loaded=False):
        fresh = self._base(db, snapshot['id'], snapshot['mcp_name'], snapshot['expected_revision'], allow_loaded=allow_loaded)
        for field in fresh:
            if fresh[field] != snapshot.get(field):
                raise PluginError('revision_conflict', 'MCP 包、目标、签名或依赖已变化；请重新预览')
        return fresh

    def _managed(self, token, fingerprint, operation):
        captured = {}
        def validate(db, snapshot):
            self._fresh(db,snapshot,allow_loaded=True)
            captured['snapshot'] = copy.deepcopy(snapshot)
            return self.manager._row(db,snapshot['id'],installed=True)
        def prepare(snapshot, _row, call_id):
            # Non-production managers retain their generated execution identity for
            # explicitly injected protocol/test adapters. Production/default wiring
            # must still use the exact preapproved strict host call identity.
            explicit_test_adapter = (self._explicit_runtime
                                     and self.manager.require_strict_execution is False
                                     and getattr(self.runtime, 'test_adapter', False) is True)
            if self.manager.require_strict_execution and not explicit_test_adapter and call_id != snapshot.get('strict_mcp_spec', {}).get('call_id'):
                raise PluginError('mcp_call_id_not_preapproved', 'MCP 执行 call_id 必须来自已确认的严格宿主预览', 409)
            return {'package_root':Path(snapshot['package_root']),'entrypoint':{},
                    'approval_snapshot':copy.deepcopy(snapshot),
                    'request':{'protocol':1,'request_id':call_id,'operation':operation,
                               'declaration':copy.deepcopy(snapshot['declaration']),
                               'target':copy.deepcopy(snapshot['target']),'tool':snapshot.get('tool'),
                               'args':copy.deepcopy(snapshot.get('args',{})),
                               'tool_schema':copy.deepcopy(snapshot.get('tool_schema'))}}
        def validate_output(result):
            if operation == 'discover':
                return {'tools':_tools(result.get('tools') if isinstance(result,dict) else None)}
            if not isinstance(result,dict) or 'result' not in result:
                raise PluginError('mcp_protocol','MCP worker 响应无效；不自动重试',502)
            return result
        output = self.manager.execute_managed_operation(token,fingerprint,
            mode='mcp_discover' if operation == 'discover' else 'mcp_execute',runtime=self.runtime,
            validate_snapshot=validate,prepare_request=prepare,validate_result=validate_output)
        return captured['snapshot'],output['result']

    def discover(self, token, fingerprint):
        # Catalog authority is minted only after the shared owner has published a
        # successful terminal result (not merely accepted a finalizer callback).
        value, result = self._managed(token,fingerprint,'discover')
        tools = result['tools']
        with self.manager._lock, self.manager._db() as db:
            self._fresh(db,value,allow_loaded=True)
            catalog = {**value,'action':'call','mcp_operation':'catalog','tools':tools}
            catalog_token = self.manager._issue(db,catalog)
        return {'id':value['id'],'revision':value['expected_revision'],'digest':value['digest'],
                'mcp_name':value['mcp_name'],'tools':tools,'catalog':catalog_token,
                'note':'发现结果只在当前包版本及有限有效期内可用；每次工具执行需独立预览确认。'}

    def preview_call(self, plugin_id, name, expected_revision, catalog_token, catalog_fingerprint, tool, args):
        with self.manager._lock, self.manager._db() as db:
            base = self._base(db, plugin_id, name, expected_revision)
            catalog = self._read_token(db, catalog_token, catalog_fingerprint, 'catalog', consume=False)
            self._fresh(db, catalog)
            if catalog['id'] != plugin_id or catalog['mcp_name'] != name:
                raise PluginError('invalid_catalog', '发现目录不属于此 MCP', 409)
            found = next((item for item in catalog['tools'] if item['name'] == tool), None)
            if not found:
                raise PluginError('unknown_tool', '工具不在已发现目录中', 422)
            args = copy.deepcopy(args)
            if not isinstance(args, dict) or len(canonical_json(args).encode()) > 65536:
                raise PluginError('invalid_args', 'MCP 参数必须是有限 JSON 对象', 422)
            if _contains_credential_key(args):
                raise PluginError('credential_material', '包 MCP 不接受凭据本体或密钥参数', 422)
            try:
                jsonschema.validate(args, found['inputSchema'])
            except (jsonschema.ValidationError, jsonschema.SchemaError) as exc:
                raise PluginError('invalid_args', 'MCP 参数不符合已发现的真实 schema', 422) from exc
            value = {**base, 'action': 'call', 'mcp_operation': 'execute', 'tool': tool,
                     'args': args, 'tool_schema': found['inputSchema'],
                     'tool_definition': found, 'permissions': ['mcp.connect', 'mcp.call'],
                     'targets': [base['target']], 'arguments_are_exact': True}
            return self.manager._issue(db, self._strict_preview(value))

    def execute(self, token, fingerprint):
        value,result = self._managed(token,fingerprint,'execute')
        return {'ok':True,'id':value['id'],'mcp_name':value['mcp_name'],
                'fingerprint':fingerprint,'result':result['result']}


def _tools(values):
    if not isinstance(values, list) or len(values) > MAX_TOOLS or len(canonical_json(values).encode()) > MAX_CATALOG_BYTES:
        raise PluginError('mcp_catalog_limit', 'MCP 工具目录超出安全限额', 422)
    result, names = [], set()
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not 1 <= len(item['name']) <= 100 or item['name'] in names:
            raise PluginError('mcp_catalog_invalid', 'MCP 工具目录结构无效或重名', 422)
        schema = item.get('inputSchema')
        if not isinstance(schema, dict):
            raise PluginError('mcp_catalog_invalid', 'MCP 工具缺少输入 schema', 422)
        # No remote schema resolution: schemas must be self-contained local JSON.
        def no_external(value):
            if isinstance(value, dict):
                if '$ref' in value and (not isinstance(value['$ref'], str) or not value['$ref'].startswith('#')):
                    raise PluginError('mcp_schema_external', 'MCP schema 不允许外部引用', 422)
                for child in value.values(): no_external(child)
            elif isinstance(value, list):
                for child in value: no_external(child)
        no_external(schema)
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise PluginError('mcp_catalog_invalid', 'MCP 工具 schema 无效', 422) from exc
        names.add(item['name']); result.append(copy.deepcopy(item))
    return result
