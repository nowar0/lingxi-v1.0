"""Explicitly configured portable-manager API; no import or default APPDATA access.

No browser request may configure roots, select Python modules, launch/kill apps, or
activate data. Production tools come only from program_manager's pinned loader.
Opaque plans remain host-owned. Restarted hosts may discover only bounded verified
journals under the approved state root, then explicitly adopt read/recovery handles;
adoption never authorizes commit or repair. The fixed offline CLI remains available.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA = re.compile(r'^[0-9a-f]{64}$')
_PLAN_ID = re.compile(r'^[A-Za-z0-9_-]{32,100}$')
MAX_PLAN_JSON_BYTES = 8 * 1024**2
MAX_PLAN_TOTAL_BYTES = 32 * 1024**2
MAX_DISCOVERY_DIRS = 1000
MAX_DISCOVERY_RECORD_BYTES = 16 * 1024**2
_TRANSACTION_DIR = re.compile(r'^(tx|uninstall)-[A-Za-z0-9_-]{8,100}$')
_JOURNAL_FILES = frozenset({'plan.json', 'events.jsonl', 'journal.json'})
_DISCOVERY_PHASES = frozenset({'preview','commit_attempted','confirmed','staged','installed',
    'rolled_back','installed_recovered','restored_previous','aborted_before_publish',
    'rolled_back_recovered','uninstalled','confirmation_rejected','confirmation_expired',
    'recover_attempted','unlink_intent','unlinked'}) | frozenset(
    name+suffix for name in ('retain_previous','publish_candidate','rollback_save_current',
        'rollback_publish_previous','rollback_retain_current','recover_previous',
        'recover_retain_current','recover_current') for suffix in ('_intent','_done'))
TERMINAL_PHASES = frozenset({'installed', 'installed_recovered', 'rolled_back',
                            'rolled_back_recovered', 'restored_previous',
                            'aborted_before_publish', 'uninstalled'})


class ManagerApiError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


def _fail(code, message, status=422):
    raise ManagerApiError(code, message, status)


def _absolute(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or len(raw) > 4000 or '\x00' in raw:
        _fail('invalid_path', '路径无效')
    path = Path(raw)
    if not path.is_absolute() or raw.startswith(('\\\\', '//')) or '..' in path.parts:
        _fail('invalid_path', '仅允许显式本地绝对路径，不接受网络或父目录跳转')
    return Path(os.path.abspath(path))


def _overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _ordinary_ancestors(path):
    # Called only when an explicitly configured service handles an operation.
    for part in (path, *path.parents):
        if (part.is_symlink() or part.is_junction()
                or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)):
            _fail('linked_path', '程序管理路径不允许链接或重解析点')
    if path.is_file() and path.stat().st_nlink != 1:
        _fail('linked_path', '程序管理文件不允许硬链接')
    return path


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass
class _Plan:
    path: Path
    fingerprint: str
    kind: str
    plan: dict
    expires: float
    size_bytes: int = 0
    attempted: bool = False
    cancelled: bool = False


@dataclass
class _Discovery:
    path: Path
    fingerprint: str
    observation: str
    expires: float


@dataclass
class _Recovery:
    plan_id: str
    fingerprint: str
    expires: float
    status_fingerprint: str


class ManagerService:
    """Host-only constructor. API clients cannot supply any of these capabilities."""
    def __init__(self, approved_root, state_dir, data_dir, *, tool_loader=None,
                 clock=time.monotonic, ttl=300, max_plans=100, protected_program_roots=()):
        self.root, self.state, self.data = map(_absolute, (approved_root, state_dir, data_dir))
        if (self.state == self.root or not self.state.is_relative_to(self.root)
                or self.data == self.root or not self.data.is_relative_to(self.root)
                or _overlap(self.data, self.state)):
            _fail('invalid_configuration', '宿主程序根、事务目录与隔离数据目录配置不安全')
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 1 <= ttl <= 600:
            raise ValueError('bounded TTL required')
        if type(max_plans) is not int or not 1 <= max_plans <= 100:
            raise ValueError('max_plans must be 1..100')
        if tool_loader is not None and not callable(tool_loader):
            raise TypeError('tool_loader is host-only callable')
        self._loader, self._clock, self._ttl, self._maximum = tool_loader, clock, ttl, max_plans
        if type(protected_program_roots) is not tuple:
            raise TypeError('protected program roots are host-owned immutable paths')
        self.protected_program_roots = tuple(_absolute(path) for path in protected_program_roots)
        self._plans, self._recoveries, self._discoveries = {}, {}, {}
        # Rejected oversized previews may already have durable tool journals.
        # Remember a bounded key BEFORE invoking the same tool again.
        self._capacity_rejections = set()
        self._lock = threading.RLock()

    def metadata(self):
        return {'configured': True, 'approved_root': str(self.root),
                'state_dir': str(self.state), 'data_dir': str(self.data),
                'approval_required': True, 'automatic_start': False,
                'automatic_process_stop': False, 'data_activation': False,
                'recovery_after_restart': 'fixed-offline-cli-only',
                'owned_journal_discovery': True,
                'notice': '只管理宿主显式授权目录；预览不是执行批准，不读取默认数据或Key'}

    def _path(self, value, role):
        path = _ordinary_ancestors(_absolute(value))
        if path == self.root or not path.is_relative_to(self.root):
            _fail('outside_approved_root', '路径不在宿主明确允许的程序根内')
        if any(_overlap(path, protected) for protected in self.protected_program_roots):
            _fail('manager_self_protected', '不能将管理器自身或其运行资源作为来源/安装/卸载目标')
        if _overlap(path, self.state) or _overlap(path, self.data):
            _fail('protected_directory', '程序来源或目标不能包含事务或用户数据目录')
        if role == 'target' and any(p.lower().endswith(('.previous', '.stage', '.swap'))
                                    for p in path.relative_to(self.root).parts):
            _fail('protected_directory', '不允许将保留版本或暂存目录作为程序目标')
        return path

    def _tools(self):
        for path in (self.root, self.state, self.data):
            _ordinary_ancestors(path)
        if not self.root.is_dir():
            _fail('invalid_configuration', '宿主允许根必须是现有普通目录')
        try:
            if self._loader is None:
                from app.program_manager import _load_fixed_tools
                modules = _load_fixed_tools()
            else:
                modules = self._loader()
            if not isinstance(modules, tuple) or len(modules) != 2:
                raise ValueError('fixed tools missing')
            install, uninstall = modules
            return (install.Installer(self.root, self.state),
                    uninstall.Uninstaller(self.root, self.state))
        except ManagerApiError:
            raise
        except Exception:
            _fail('fixed_tools_unavailable', '已审核内置程序管理工具不可用；没有执行或使用外部Python回退', 503)

    def _purge(self):
        now = self._clock()
        self._plans = {ident: item for ident, item in self._plans.items()
                       if item.attempted or (not item.cancelled and item.expires > now)}
        self._recoveries = {ident: item for ident, item in self._recoveries.items()
                            if item.expires > now}
        self._discoveries = {ident: item for ident, item in self._discoveries.items()
                             if item.expires > now}

    def _get(self, ident):
        if not isinstance(ident, str) or not _PLAN_ID.fullmatch(ident):
            _fail('unknown_plan', '未知或已失效的程序管理计划', 404)
        self._purge()
        plan = self._plans.get(ident)
        if plan is None or plan.cancelled:
            _fail('unknown_plan', '未知或已失效的程序管理计划', 404)
        return plan

    def _register(self, result, kind, *, expected_target):
        if (not isinstance(result, dict) or not isinstance(result.get('plan'), dict)
                or not isinstance(result.get('fingerprint'), str)
                or not _SHA.fullmatch(result['fingerprint']) or result.get('approved') is not False):
            _fail('invalid_tool_result', '内置工具未返回可审阅的未批准计划', 503)
        try:
            size = len(json.dumps(result['plan'], ensure_ascii=False, allow_nan=False).encode('utf-8'))
        except (ValueError, TypeError, RecursionError):
            _fail('invalid_tool_result', '内置计划不是有限JSON', 503)
        if size > MAX_PLAN_JSON_BYTES or sum(item.size_bytes for item in self._plans.values()) + size > MAX_PLAN_TOTAL_BYTES:
            _fail('plan_capacity', '精确计划超过界面容量上限；不截断授权，请使用固定离线CLI审阅完整计划', 413)
        plan = copy.deepcopy(result['plan'])
        path = _ordinary_ancestors(_absolute(result.get('plan_path', '')))
        if (path.name != 'plan.json' or path.parent.parent != self.state
                or not re.fullmatch(r'(?:tx|uninstall)-[A-Za-z0-9_-]{8,100}', path.parent.name)
                or plan.get('allow_root') != str(self.root) or plan.get('state_dir') != str(self.state)
                or plan.get('target') != str(expected_target)
                or _fingerprint(plan) != result['fingerprint']):
            _fail('invalid_tool_result', '内置计划的精确路径、目标或哈希不符', 503)
        expected_action = 'install' if kind == 'install' else 'rollback'
        if kind != 'uninstall' and plan.get('action') != expected_action:
            _fail('invalid_tool_result', '内置计划动作不匹配', 503)
        if kind == 'uninstall':
            if plan.get('data_dir') != str(self.data):
                _fail('invalid_tool_result', '卸载计划未保留宿主指定的数据目录边界', 503)
        elif not isinstance(plan.get('data'), dict) or plan['data'].get('data_dir') != str(self.data):
            _fail('invalid_tool_result', '安装计划未绑定宿主指定的数据目录', 503)
        if len(self._plans) >= self._maximum:
            _fail('plan_limit', '当前程序管理计划过多，请先完成或取消已有计划', 429)
        ident = secrets.token_urlsafe(32)
        self._plans[ident] = _Plan(path, result['fingerprint'], kind, plan, self._clock()+self._ttl, size)
        return {'plan_id': ident, 'kind': kind, 'plan': copy.deepcopy(plan),
                'fingerprint': result['fingerprint'], 'expires_in': self._ttl,
                'approved': False, 'approval_required': True, 'automatic_start': False,
                'data_modified': False}

    def _state_budget(self):
        # Count even unknown journals: restarting the host cannot reset disk quotas.
        # No automatic deletion; discovery only consumes bounded owned records.
        if not self.state.exists():
            return
        _ordinary_ancestors(self.state)
        count, total = 0, 0
        for folder, directories, files in os.walk(self.state, followlinks=False):
            for name in directories+files:
                path = _ordinary_ancestors(Path(folder)/name)
                count += 1
                if path.is_file():
                    total += path.stat().st_size
                if count > 20000 or total > 64*1024**2:
                    _fail('state_capacity', '事务目录已达安全容量上限；保留已有日志，请离线核对，不自动删除', 413)

    @staticmethod
    def _file_identity(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def _journal_snapshot(self, folder):
        """Bounded no-follow observations; never create a lease or repair a journal."""
        _ordinary_ancestors(folder)
        initial = folder.lstat()
        if not stat.S_ISDIR(initial.st_mode):
            raise ValueError('ordinary transaction directory required')
        records, decoded, total = {}, {}, 0
        children = []
        for child in folder.iterdir():
            children.append(child)
            if len(children) > 1000:
                raise ValueError('transaction directory capacity')
        for child in children:
            _ordinary_ancestors(child)
            observed = child.lstat()
            if child.name not in _JOURNAL_FILES:
                # Only the fixed installer's owned staging folders are recognized.
                if child.name not in ('payload', 'verify') or not stat.S_ISDIR(observed.st_mode):
                    raise ValueError('unknown transaction member')
                records[child.name] = {'directory_identity': self._file_identity(observed)}
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise ValueError('ordinary journal file required')
            limit = MAX_PLAN_JSON_BYTES if child.name == 'plan.json' else MAX_DISCOVERY_RECORD_BYTES
            if observed.st_size > limit:
                raise ValueError('journal file capacity')
            with child.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                if self._file_identity(opened) != self._file_identity(observed):
                    raise ValueError('journal changed before read')
                raw = stream.read(limit+1)
                after = os.fstat(stream.fileno())
            if (len(raw) > limit or self._file_identity(after) != self._file_identity(observed)
                    or self._file_identity(child.lstat()) != self._file_identity(observed)):
                raise ValueError('journal changed during read')
            total += len(raw)
            if total > MAX_DISCOVERY_RECORD_BYTES:
                raise ValueError('transaction record capacity')
            records[child.name] = {'identity': self._file_identity(observed),
                                   'sha256': hashlib.sha256(raw).hexdigest()}
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('duplicate journal key')
                    value[key] = item
                return value
            def parse(data):
                return json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
            if child.name == 'events.jsonl':
                decoded[child.name] = [parse(line) for line in raw.splitlines()]
            else:
                decoded[child.name] = parse(raw)
        if set(decoded) != _JOURNAL_FILES or not decoded['events.jsonl']:
            raise ValueError('complete transaction journal required')
        events = decoded['events.jsonl']
        if len(events) > 20000 or decoded['journal.json'] != events[-1]:
            raise ValueError('journal mirror differs or capacity')
        if self._file_identity(folder.lstat()) != self._file_identity(initial):
            raise ValueError('transaction directory changed')
        records['directory'] = self._file_identity(initial)
        return records, decoded

    def _inspect_transaction(self, path, installer, uninstaller):
        match = _TRANSACTION_DIR.fullmatch(path.parent.name)
        if path.name != 'plan.json' or path.parent.parent != self.state or match is None:
            raise ValueError('owned transaction path required')
        before, decoded = self._journal_snapshot(path.parent)
        envelope = decoded['plan.json']
        if (type(envelope) is not dict or set(envelope) != {'plan','fingerprint'}
                or type(envelope['plan']) is not dict or not isinstance(envelope['fingerprint'],str)
                or not _SHA.fullmatch(envelope['fingerprint'])
                or _fingerprint(envelope['plan']) != envelope['fingerprint']):
            raise ValueError('plan fingerprint differs')
        plan, fingerprint = envelope['plan'], envelope['fingerprint']
        kind = 'uninstall' if match[1] == 'uninstall' else plan.get('action')
        if kind not in ('install','rollback','uninstall'):
            raise ValueError('unknown transaction action')
        if (plan.get('id') != path.parent.name or plan.get('allow_root') != str(self.root)
                or plan.get('state_dir') != str(self.state)):
            raise ValueError('transaction authority differs')
        self._path(plan.get('target'), 'target')
        if kind == 'uninstall':
            if plan.get('data_dir') != str(self.data):
                raise ValueError('data authority differs')
            tool, loader = uninstaller, uninstaller._load_uninstall
        else:
            if type(plan.get('data')) is not dict or plan['data'].get('data_dir') != str(self.data):
                raise ValueError('data authority differs')
            if kind == 'install':
                source = plan.get('source')
                if type(source) is not dict or source.get('kind') not in ('directory','archive'):
                    raise ValueError('unknown source authority')
                self._path(source.get('path'), 'source')
                if source['kind'] == 'archive':
                    self._path(source.get('record'), 'source')
                    payload = _ordinary_ancestors(_absolute(source.get('payload')))
                    if not payload.is_relative_to(path.parent/'payload') or payload == path.parent/'payload':
                        raise ValueError('archive payload authority differs')
            elif kind == 'rollback':
                original = _ordinary_ancestors(_absolute(plan.get('installed_plan')))
                if (original.name != 'plan.json' or original.parent.parent != self.state
                        or not _TRANSACTION_DIR.fullmatch(original.parent.name)
                        or not original.parent.name.startswith('tx-')):
                    raise ValueError('rollback origin authority differs')
            tool, loader = installer, installer._load
        folder, loaded, digest = loader(path)
        if folder != path.parent or loaded != plan or digest != fingerprint:
            raise ValueError('fixed tool plan proof differs')
        events = tool._events(folder)
        if events != decoded['events.jsonl'] or events[0].get('phase') != 'preview':
            raise ValueError('fixed tool journal proof differs')
        for index,event in enumerate(events):
            if (type(event) is not dict or event.get('seq') != index+1
                    or event.get('previous_hash') != (_fingerprint(events[index-1]) if index else None)
                    or event.get('phase') not in _DISCOVERY_PHASES
                    or type(event.get('details')) is not dict):
                raise ValueError('journal chain differs')
            if event['phase'] in ('commit_attempted','confirmed') and event.get('details',{}).get('fingerprint') != fingerprint:
                raise ValueError('journal attempt fingerprint differs')
        status = tool.status(path)
        second = tool.status(path)
        after, after_decoded = self._journal_snapshot(folder)
        if (before != after or decoded != after_decoded or _fingerprint(status) != _fingerprint(second)
                or type(status) is not dict or status.get('transaction') != plan['id']
                or status.get('fingerprint') != fingerprint or status.get('target') != plan['target']
                or status.get('phase') != events[-1]['phase'] or status.get('data_modified') is not False
                or status.get('automatic_start') is not False):
            raise ValueError('transaction changed or status unverified')
        attempted = any(event['phase'] == 'commit_attempted' for event in events)
        # Plain previews never acquire execution approval through discovery.
        adoptable = attempted and status['phase'] != 'preview'
        observation = _fingerprint({'files':before,'status':status,'kind':kind,'plan':fingerprint})
        return {'plan':plan,'kind':kind,'fingerprint':fingerprint,'status':status,
                'observation':observation,'adoptable':adoptable,
                'size':len(json.dumps(plan,ensure_ascii=False,allow_nan=False).encode('utf-8'))}

    def transactions(self):
        """Read only host-approved owned journals; unknown entries are never adopted."""
        with self._lock:
            self._purge()
            self._discoveries.clear()  # A fresh listing retires only prior read capabilities.
            self._state_budget()
            output = {'owned_journal_discovery':True,'read_only':True,'complete':True,
                      'entries':[],'unknown_entries':0,'program_modified':False,'data_modified':False}
            if not self.state.exists():
                return output
            children = []
            for child in self.state.iterdir():
                children.append(child)
                if len(children) > MAX_DISCOVERY_DIRS:
                    _fail('discovery_capacity','事务顶层条目超过1000；未截断发现或自动删除，请离线核对',413)
            installer, uninstaller = self._tools()
            for folder in sorted(children,key=lambda item:item.name):
                if folder.name == '.lease':
                    continue  # Already checked ordinary by the state budget.
                match = _TRANSACTION_DIR.fullmatch(folder.name)
                if match is None or not folder.is_dir():
                    output['unknown_entries'] += 1
                    continue
                try:
                    observed = self._inspect_transaction(folder/'plan.json',installer,uninstaller)
                except Exception:
                    output['entries'].append({'transaction_id':folder.name,'verified':False,
                        'adoptable':False,'discovery_id':None,'fingerprint':None,
                        'reason':'unverified_or_changed_journal'})
                    continue
                known = next((ident for ident,record in self._plans.items()
                    if record.path == folder/'plan.json' and record.fingerprint == observed['fingerprint']
                    and record.attempted),None)
                ident = None
                # A new browser page may not possess the existing opaque handle.
                # Issue a fresh read/adoption capability even for known transactions;
                # adoption revalidates and reuses the existing slot, never mints commit.
                if observed['adoptable']:
                    ident = secrets.token_urlsafe(32)
                    self._discoveries[ident] = _Discovery(folder/'plan.json',observed['fingerprint'],
                                                         observed['observation'],self._clock()+self._ttl)
                output['entries'].append({'transaction_id':folder.name,'kind':observed['kind'],
                    'phase':observed['status']['phase'],'target':observed['plan']['target'],
                    'verified':True,'adoptable':observed['adoptable'],'known_plan_id':known,
                    'discovery_id':ident,'fingerprint':observed['fingerprint'],
                    'expires_in':self._ttl if ident else 0,
                    'reason':None if observed['adoptable'] else 'preview_has_no_execution_approval'})
            return output

    def adopt_transaction(self, discovery_id, fingerprint, user_confirmed):
        """Import a read/recovery handle, never a commit or repair authorization."""
        with self._lock:
            self._purge()
            record = self._discoveries.pop(discovery_id,None)
            if (record is None or user_confirmed is not True or fingerprint != record.fingerprint
                    or self._clock() >= record.expires):
                _fail('discovery_confirmation_invalid','事务导入确认已失效或不匹配；不能重试旧批准',409)
            self._state_budget()
            installer,uninstaller = self._tools()
            try:
                observed = self._inspect_transaction(record.path,installer,uninstaller)
            except Exception:
                _fail('discovery_changed','事务现场已变化或无法验证；未执行恢复，请重新发现',409)
            if (observed['fingerprint'] != fingerprint or observed['observation'] != record.observation
                    or not observed['adoptable']):
                _fail('discovery_changed','事务现场已变化或未曾执行；旧发现不能导入',409)
            known = next((ident for ident,plan in self._plans.items()
                          if plan.path == record.path and plan.fingerprint == fingerprint),None)
            if known is not None:
                # An out-of-process commit may have advanced a handle created by
                # this host's preview. Durable evidence retires its old commit capability.
                self._plans[known].attempted = True
                self._plans[known].expires = self._clock()
                self._plans[known].cancelled = False
            if known is None:
                if len(self._plans) >= self._maximum or sum(p.size_bytes for p in self._plans.values())+observed['size'] > MAX_PLAN_TOTAL_BYTES:
                    _fail('plan_capacity','已登记事务容量已满；保留日志，请先核对完成事务',413)
                known = secrets.token_urlsafe(32)
                self._plans[known] = _Plan(record.path,fingerprint,observed['kind'],
                    copy.deepcopy(observed['plan']),self._clock(),observed['size'],attempted=True)
            return {'plan_id':known,'kind':observed['kind'],'transaction_id':observed['plan']['id'],
                    'fingerprint':fingerprint,'phase':observed['status']['phase'],'adopted':True,
                    'approval_attempted':True,'commit_allowed':False,'recovery_requires_preview':True,
                    'program_modified':False,'data_modified':False,'automatic_start':False}

    def _preview_capacity(self):
        self._purge()
        self._state_budget()
        if len(self._plans) >= self._maximum:
            _fail('plan_limit', '计划容量已满；请核对并明确退休已完成事务，未知或未结事务不能丢弃', 429)
        if len(self._capacity_rejections) >= self._maximum:
            _fail('plan_capacity', '界面拒绝预览记录容量已满；请使用固定离线CLI核对已有日志，不再生成新日志', 413)
        if sum(item.size_bytes for item in self._plans.values()) >= MAX_PLAN_TOTAL_BYTES:
            _fail('plan_capacity', '完整计划内存容量已满；先核对并退休已完成事务，未创建新日志', 413)

    def _preview_once(self, key, action):
        digest = _fingerprint(key)
        if digest in self._capacity_rejections:
            _fail('plan_capacity', '相同超容量预览已拒绝；已有日志保留，请使用固定离线CLI，不重复创建事务', 413)
        try:
            return action()
        except ManagerApiError as error:
            if error.code == 'plan_capacity':
                self._capacity_rejections.add(digest)
            raise

    def preview_install(self, source, target, *, archive_record=None, legacy_manifest=None):
        with self._lock:
            self._preview_capacity()
            source, target = self._path(source, 'source'), self._path(target, 'target')
            if _overlap(source, target):
                _fail('overlapping_paths', '来源和目标不能相同或祖先互套')
            optional = {key:self._path(value, 'source') if value is not None else None
                        for key,value in (('archive_record',archive_record),('legacy_manifest',legacy_manifest))}
            installer, _ = self._tools()
            try:
                return self._preview_once({'kind':'install','source':str(source),'target':str(target),
                    'options':{key:str(value) if value is not None else None for key,value in optional.items()}},
                    lambda:self._register(installer.preview(source,target,data_dir=self.data,**optional),
                                          'install',expected_target=target))
            except ManagerApiError:
                raise
            except Exception:
                _fail('preview_refused', '程序安装预览被拒绝；请核对完整性、占用、来源和数据兼容性')

    def preview_rollback(self, installed_plan_id):
        with self._lock:
            self._preview_capacity()
            original = self._get(installed_plan_id)
            if original.kind != 'install' or not original.attempted:
                _fail('wrong_plan_kind', '只允许从本会话已尝试的安装计划预览程序回滚', 409)
            installer, _ = self._tools()
            try:
                return self._preview_once({'kind':'rollback','original':installed_plan_id},
                    lambda:self._register(installer.preview_rollback(original.path,data_dir=self.data),
                                          'rollback',expected_target=Path(original.plan['target'])))
            except ManagerApiError:
                raise
            except Exception:
                _fail('rollback_refused', '回滚预览被拒绝；可能没有上一版本或数据schema不兼容', 409)

    def preview_uninstall(self, target, expected_manifest_sha):
        with self._lock:
            self._preview_capacity()
            target = self._path(target, 'target')
            if not isinstance(expected_manifest_sha, str) or not _SHA.fullmatch(expected_manifest_sha):
                _fail('invalid_fingerprint', '卸载必须提供准确的发布清单SHA256')
            _, uninstaller = self._tools()
            try:
                return self._preview_once({'kind':'uninstall','target':str(target),'manifest':expected_manifest_sha},
                    lambda:self._register(uninstaller.preview(target,expected_manifest_sha,data_dir=self.data),
                                          'uninstall',expected_target=target))
            except ManagerApiError:
                raise
            except Exception:
                _fail('uninstall_preview_refused', '卸载预览被拒绝；未删除程序或用户数据')

    def _confirmed(self, ident, fingerprint, user_confirmed, *, uninstall=False):
        plan = self._get(ident)
        if (plan.kind == 'uninstall') != uninstall:
            _fail('wrong_plan_kind', '安装与卸载批准不能混用', 409)
        if plan.attempted:
            _fail('approval_consumed', '该计划已尝试，不能重复提交；中断需独立恢复确认', 409)
        plan.attempted = True
        if user_confirmed is not True or fingerprint != plan.fingerprint or self._clock() >= plan.expires:
            _fail('confirmation_invalid', '确认已失效或指纹不符；本次批准已消费', 409)
        return plan

    def commit(self, ident, fingerprint, user_confirmed, *, uninstall=False):
        with self._lock:
            plan = self._confirmed(ident, fingerprint, user_confirmed, uninstall=uninstall)
            installer, uninstaller = self._tools()
            try:
                return (uninstaller if uninstall else installer).commit(plan.path, plan.fingerprint)
            except Exception:
                _fail('commit_refused', '提交失败或结果未知；没有自动重试。请先核对状态并单独预览恢复', 409)

    def status(self, ident):
        with self._lock:
            plan = self._get(ident)
            installer, uninstaller = self._tools()
            try:
                result = (uninstaller if plan.kind == 'uninstall' else installer).status(plan.path)
            except Exception:
                _fail('status_refused', '无法核对该已登记计划的状态；不猜测或修改现场', 409)
            return {**result, 'plan_id':ident, 'kind':plan.kind, 'approval_attempted':plan.attempted}

    def retire(self, ident):
        """Release only a proven completed in-memory handle; durable tool journal stays."""
        with self._lock:
            plan = self._get(ident)
            if not plan.attempted:
                _fail('not_attempted', '未尝试预览应使用取消，不是完成退休', 409)
            current = self.status(ident)
            if current.get('phase') not in TERMINAL_PHASES:
                _fail('unfinished_transaction', '未结或结果未知事务必须保留查询/恢复入口，不能退休', 409)
            transaction = current.get('transaction')
            if not isinstance(transaction, str) or transaction != plan.plan.get('id'):
                _fail('unverified_terminal', '终态与准确事务标识不符，不能释放引用', 409)
            self._plans.pop(ident)
            self._recoveries = {key:item for key,item in self._recoveries.items() if item.plan_id != ident}
            return {'plan_id':ident,'retired':True,'terminal_phase':current['phase'],
                    'journal_preserved':True,'program_modified':False,
                    'notice':'仅释放本次内存句柄；持久日志保留，之后可用固定离线CLI核对'}

    def cancel(self, ident):
        with self._lock:
            plan = self._get(ident)
            if plan.attempted:
                _fail('already_attempted', '已尝试的事务不能当作普通预览取消；请核对状态', 409)
            plan.cancelled = True
            return {'plan_id':ident, 'cancelled':True, 'program_modified':False,
                    'journal_preserved':True, 'notice':'仅撤销内存批准，不删除程序、数据或事务日志'}

    def preview_recovery(self, ident):
        with self._lock:
            plan = self._get(ident)
            if not plan.attempted:
                _fail('not_attempted', '未尝试的预览不是恢复授权', 409)
            if len(self._recoveries) >= self._maximum:
                _fail('plan_limit', '恢复预览过多', 429)
            status = self.status(ident)
            snapshot = {'action':'recover', 'plan_id':ident, 'kind':plan.kind,
                        'transaction_fingerprint':plan.fingerprint, 'status':status}
            digest = _fingerprint(snapshot)
            recovery_id = secrets.token_urlsafe(32)
            self._recoveries[recovery_id] = _Recovery(ident, digest, self._clock()+self._ttl, _fingerprint(status))
            return {'recovery_id':recovery_id, 'snapshot':snapshot, 'fingerprint':digest,
                    'approval_required':True, 'automatic_start':False}

    def recover(self, recovery_id, fingerprint, user_confirmed):
        with self._lock:
            self._purge()
            approval = self._recoveries.pop(recovery_id, None)
            if (approval is None or user_confirmed is not True or fingerprint != approval.fingerprint
                    or self._clock() >= approval.expires):
                _fail('confirmation_invalid', '恢复批准失效或不匹配；不会自动重试', 409)
            plan = self._get(approval.plan_id)
            if _fingerprint(self.status(approval.plan_id)) != approval.status_fingerprint:
                _fail('recovery_status_changed', '恢复现场状态已改变，请重新核对并生成恢复预览', 409)
            installer, uninstaller = self._tools()
            try:
                return (uninstaller if plan.kind == 'uninstall' else installer).recover(plan.path, plan.fingerprint)
            except Exception:
                _fail('recovery_refused', '恢复被拒绝或结果未知；请保留现场并核对，不自动删除未知文件', 409)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class InstallPreview(Strict):
    source: str = Field(min_length=1, max_length=4000)
    target: str = Field(min_length=1, max_length=4000)
    archive_record: str | None = Field(default=None, max_length=4000)
    legacy_manifest: str | None = Field(default=None, max_length=4000)


class PlanId(Strict):
    plan_id: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')


class Confirm(PlanId):
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    user_confirmed: bool

    @field_validator('user_confirmed')
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError('需要独立明确确认')
        return value


class UninstallPreview(Strict):
    target: str = Field(min_length=1, max_length=4000)
    expected_manifest_sha: str = Field(pattern=r'^[0-9a-f]{64}$')


class AdoptTransaction(Strict):
    discovery_id: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    user_confirmed: bool

    @field_validator('user_confirmed')
    @classmethod
    def explicit(cls, value):
        return Confirm.explicit(value)


class Recover(Strict):
    recovery_id: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    user_confirmed: bool

    @field_validator('user_confirmed')
    @classmethod
    def explicit(cls, value):
        return Confirm.explicit(value)


def create_router(service=None):
    router = APIRouter(prefix='/v11/program-manager', tags=['program-manager'])

    def invoke(method, *args, **kwargs):
        if service is None:
            raise HTTPException(503, {'code':'program_manager_not_configured',
                                     'message':'宿主尚未显式配置程序管理目录；本接口不使用默认安装或数据路径'})
        try:
            return getattr(service, method)(*args, **kwargs)
        except ManagerApiError as error:
            raise HTTPException(error.status, {'code':error.code, 'message':str(error)}) from error
        except (ValueError, OSError, TypeError):
            raise HTTPException(422, {'code':'program_manager_request_invalid',
                                     'message':'程序管理请求无效；未自动启动、结束程序或切换数据'}) from None

    @router.get('')
    def metadata():
        return invoke('metadata')

    @router.get('/transactions')
    def transactions():
        return invoke('transactions')

    @router.post('/transactions/adopt')
    def adopt_transaction(body:AdoptTransaction):
        return invoke('adopt_transaction', body.discovery_id, body.fingerprint, body.user_confirmed)

    @router.post('/install/preview')
    def install(body:InstallPreview):
        return invoke('preview_install', body.source, body.target,
                      archive_record=body.archive_record, legacy_manifest=body.legacy_manifest)

    @router.post('/rollback/preview')
    def rollback(body:PlanId):
        return invoke('preview_rollback', body.plan_id)

    @router.post('/uninstall/preview')
    def uninstall(body:UninstallPreview):
        return invoke('preview_uninstall', body.target, body.expected_manifest_sha)

    @router.post('/commit')
    def commit(body:Confirm):
        return invoke('commit', body.plan_id, body.fingerprint, body.user_confirmed)

    @router.post('/uninstall/commit')
    def uninstall_commit(body:Confirm):
        return invoke('commit', body.plan_id, body.fingerprint, body.user_confirmed, uninstall=True)

    @router.get('/plans/{plan_id}')
    def status(plan_id:str):
        return invoke('status', plan_id)

    @router.post('/cancel')
    def cancel(body:PlanId):
        return invoke('cancel', body.plan_id)

    @router.post('/retire')
    def retire(body:PlanId):
        return invoke('retire', body.plan_id)

    @router.post('/recover/preview')
    def recover_preview(body:PlanId):
        return invoke('preview_recovery', body.plan_id)

    @router.post('/recover')
    def recover(body:Recover):
        return invoke('recover', body.recovery_id, body.fingerprint, body.user_confirmed)

    return router
