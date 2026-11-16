"""Read-only bridge from installed packages to skills and Agent discovery.

Package metadata is projected from its registry, never copied into the legacy
catalog. Tool workers cannot take the manager's exclusive lease: reads use SQLite
mode=ro and verified immutable package files. Requests return an UNAUTHORIZED
intent; only the host UI can preview/approve/execute via PluginManager.
"""
from __future__ import annotations

import copy
import hashlib
import json
import importlib.metadata
from pathlib import Path
import re
import sqlite3
import sys
from contextlib import closing

from fastapi import HTTPException

from app.plugin_archive import scan_package
from app.plugin_manifest import ManifestError, satisfies, validate_id
from app.plugin_trust import TrustError, TrustStore

MAX_SKILL_BYTES = 200_000
PREFIX = 'pkgskill_'


def skill_id(plugin_id, name):
    return PREFIX + hashlib.sha256((plugin_id + '\0' + name).encode('utf-8')).hexdigest()


def storage_root():
    # DB_PATH is the actual selected authority and is patched by isolated tests;
    # do not accidentally consult an unpatched global APPDATA in a tool fixture.
    from app import db
    return Path(db.DB_PATH).resolve().parent / 'plugin-packages'


def _rows(root=None):
    root = Path(root) if root is not None else storage_root()
    registry = root / 'registry.sqlite3'
    if not registry.exists():
        return root, []
    if any(p.is_symlink() or p.is_junction() for p in (root, registry)):
        raise HTTPException(409, '插件存储不能是链接')
    try:
        with closing(sqlite3.connect(registry.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return root, [dict(r) for r in conn.execute(
                'SELECT p.*, v.signature_status AS _recorded_signature_status '
                'FROM plugins p LEFT JOIN versions v ON v.plugin_id=p.id AND v.digest=p.digest '
                'ORDER BY p.id')]
    except sqlite3.Error as exc:
        raise HTTPException(503, '插件注册表不可读，请在插件管理中检查') from exc


def _trust_checks(rows, root):
    """Read only the explicit registry sibling trust store; never acquire a manager.

    A missing trust root must not downgrade an installed package previously known
    as verified/revoked to a newly unknown publisher (including cross-machine copies).
    """
    try:
        store = TrustStore(Path(root).absolute().parent / 'plugin-trust.json')
        before = store.snapshot()
        remembered = any(item['installed'] and item.get('_recorded_signature_status') in
                         {'verified_trusted', 'revoked'} for item in rows)
        if not before['exists'] and remembered:
            return {'fingerprint': None, 'revision': None, 'signatures': {}, 'reasons': ['trust_store_missing']}
        signatures = {}
        for item in rows:
            result = store.verify(json.loads(item['manifest']))
            if (result['trust_revision'], result['trust_fingerprint']) != (before['revision'], before['fingerprint']):
                return {'fingerprint': None, 'revision': None, 'signatures': {}, 'reasons': ['trust_changed']}
            signatures[item['id']] = result['status']
        after = store.snapshot()
        if (after['revision'], after['fingerprint']) != (before['revision'], before['fingerprint']):
            return {'fingerprint': None, 'revision': None, 'signatures': {}, 'reasons': ['trust_changed']}
        return {'fingerprint': before['fingerprint'], 'revision': before['revision'],
                'signatures': signatures, 'reasons': []}
    except (TrustError, OSError, ValueError, TypeError, KeyError):
        return {'fingerprint': None, 'revision': None, 'signatures': {}, 'reasons': ['trust_store_invalid']}


def _availability(row, rows, root, trust=None):
    trust = _trust_checks(rows, root) if trust is None else trust
    reasons = list(trust['reasons'])
    signature = trust['signatures'].get(row['id'])
    if signature in {'invalid', 'revoked'}:
        reasons.append('signature_' + signature)
    if (row['installed'] and row.get('_recorded_signature_status') in {'verified_trusted', 'revoked'}
            and signature == 'present_unverified'):
        reasons.append('signature_trust_missing')
    if row['installed'] and row.get('_recorded_signature_status') is None:
        reasons.append('signature_metadata_missing')
    if not row['installed']:
        reasons.append('not_installed')
    if not row['enabled']:
        reasons.append('disabled')
    try:
        manifest = json.loads(row['manifest'])
        if row['installed']:
            path = root / 'code' / row['id'] / row['digest']
            actual = scan_package(path)
            if actual.digest != row['digest'] or actual.manifest != manifest:
                reasons.append('files_mismatch')
        if not satisfies('1.0.0', manifest['host_api']):
            reasons.append('host_api')
        indexed = {item['id']: item for item in rows}
        visiting, visited = set(), set()
        def check(current):
            pid = current['id']
            if pid in visiting:
                reasons.append('dependency_cycle')
                return
            if pid in visited:
                return
            visiting.add(pid)
            for dependency in json.loads(current['manifest']).get('dependencies', []):
                other = indexed.get(dependency['id'])
                if not other or not other['installed'] or not other['enabled']:
                    reasons.append('dependency_unavailable:' + dependency['id'])
                    continue
                if other.get('_recorded_signature_status') is None:
                    reasons.append('dependency_signature_metadata_missing:' + other['id'])
                dependency_signature = trust['signatures'].get(other['id'])
                if dependency_signature in {'invalid', 'revoked'}:
                    reasons.append('dependency_signature_' + dependency_signature + ':' + other['id'])
                if (other.get('_recorded_signature_status') in {'verified_trusted', 'revoked'}
                        and dependency_signature == 'present_unverified'):
                    reasons.append('dependency_signature_trust_missing:' + other['id'])
                if not satisfies(json.loads(other['manifest'])['version'], dependency['version']):
                    reasons.append('dependency_version:' + dependency['id'])
                path = root / 'code' / other['id'] / other['digest']
                package = scan_package(path)
                if package.digest != other['digest']:
                    reasons.append('dependency_files_mismatch:' + other['id'])
                check(other)
            visiting.remove(pid)
            visited.add(pid)
        check(row)
        for requirement in manifest.get('python_dependencies', []):
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)(.*)', requirement)
            if not match:
                reasons.append('python_dependency_invalid')
                continue
            if getattr(sys, 'frozen', False):
                reasons.append('python_dependency_worker_unverified')
                continue
            try:
                actual = importlib.metadata.version(match[1])
                version_range = match[2].strip().replace(',', ' ')
                if version_range and not satisfies(actual, version_range):
                    reasons.append('python_dependency_version:' + match[1])
            except (importlib.metadata.PackageNotFoundError, ManifestError):
                reasons.append('python_dependency_missing:' + match[1])
    except (OSError, ValueError, KeyError, TypeError, ManifestError):
        reasons.append('files_or_manifest_unavailable')
    return sorted(set(reasons))


def package_metadata(root=None):
    root, rows = _rows(root)
    trust = _trust_checks(rows, root)
    result = []
    for row in rows:
        try:
            manifest = json.loads(row['manifest'])
            reasons = _availability(row, rows, root, trust)
            result.append({'id': row['id'], 'name': manifest['name'], 'version': manifest['version'],
                'revision': row['revision'], 'digest': row['digest'], 'installed': bool(row['installed']),
                'enabled': bool(row['enabled']), 'available': not reasons, 'unavailable_reasons': reasons,
                'namespace': 'pkg:' + row['id'],
                'signature_status': trust['signatures'].get(row['id'], 'unavailable'),
                'trust_revision': trust['revision'], 'trust_fingerprint': trust['fingerprint'],
                'tools': [{'name': t['name'], 'description': t.get('description', '')} for t in manifest.get('tools', [])],
                'skills': [{'id': skill_id(row['id'], s['name']), 'name': s['name'],
                            'description': s.get('description', '')} for s in manifest.get('skills', [])],
                'mcp': [{'name': m['name'], 'transport': m['transport']} for m in manifest.get('mcp', [])],
                'read_only': True, 'authorization': 'explicit-host-preview-required'})
        except (ValueError, KeyError, TypeError):
            result.append({'id': row['id'], 'available': False, 'unavailable_reasons': ['invalid_manifest'],
                           'skills': [], 'tools': [], 'mcp': [], 'read_only': True})
    after = _trust_checks(rows, root)
    if after['reasons'] or after['fingerprint'] != trust['fingerprint']:
        for package in result:
            package['available'] = False
            package['unavailable_reasons'] = sorted(set(package['unavailable_reasons'] + ['trust_changed']))
            package['signature_status'] = 'unavailable'
            package['trust_revision'] = None
            package['trust_fingerprint'] = None
    return result


def dependency_available(dependency, root=None):
    if not isinstance(dependency, str) or not dependency.startswith('pkg:'):
        return False
    pid = dependency[4:]
    try:
        validate_id(pid)
    except ManifestError:
        return False
    return any(p['id'] == pid and p['available'] for p in package_metadata(root))


def skill_metadata(tags=None, root=None):
    if tags is not None and '插件包' not in tags:
        return []
    result = []
    for package in package_metadata(root):
        for skill in package['skills']:
            result.append({**skill, 'plugin_id': package['id'], 'skill_name': skill['name'],
                'digest': package.get('digest'), 'package_revision': package.get('revision'),
                'revision': package.get('revision'), 'version': package.get('version'),
                'source': 'package:' + package['id'], 'tags': ['插件包'],
                'dependencies': ['pkg:' + package['id']], 'missing_dependencies': [] if package['available'] else ['pkg:' + package['id']],
                'enabled': package.get('enabled', False), 'available': package['available'],
                'unavailable_reasons': package['unavailable_reasons'],
                'signature_status': package.get('signature_status', 'unavailable'),
                'trust_revision': package.get('trust_revision'),
                'trust_fingerprint': package.get('trust_fingerprint'), 'read_only': True})
    return result


def _live(plugin_id, root=None, expected_digest=None, expected_revision=None, expected_trust=None):
    root, rows = _rows(root)
    row = next((r for r in rows if r['id'] == plugin_id), None)
    if row is None:
        raise HTTPException(404, '插件包不存在')
    if expected_digest is not None and expected_digest != row['digest'] or expected_revision is not None and expected_revision != row['revision']:
        raise HTTPException(409, '插件包版本已变化，请重新读取能力目录')
    trust = _trust_checks(rows, root)
    if expected_trust is not None and expected_trust != trust['fingerprint']:
        raise HTTPException(409, '可信公钥状态在读取期间已变化，请重新加载')
    reasons = _availability(row, rows, root, trust)
    if reasons:
        raise HTTPException(409, '插件包不可用：' + '、'.join(reasons))
    fresh_trust = _trust_checks(rows, root)
    if fresh_trust['reasons'] or fresh_trust['fingerprint'] != trust['fingerprint']:
        raise HTTPException(409, '可信公钥状态在读取期间已变化，请重新加载')
    row['_trust_fingerprint'] = trust['fingerprint']
    row['_trust_revision'] = trust['revision']
    row['_signature_status'] = trust['signatures'].get(row['id'], 'unavailable')
    return root, row, json.loads(row['manifest'])


def assigned_skill(item_id, *, expected_digest=None, expected_revision=None, root=None):
    root, rows = _rows(root)
    for row in rows:
        manifest = json.loads(row['manifest'])
        for declared in manifest.get('skills', []):
            if skill_id(row['id'], declared['name']) != item_id:
                continue
            root, current, manifest = _live(row['id'], root, expected_digest, expected_revision)
            target = root / 'code' / current['id'] / current['digest'] / declared['path']
            raw = target.read_bytes()
            if len(raw) > MAX_SKILL_BYTES or hashlib.sha256(raw).hexdigest() != manifest['files'][declared['path']]:
                raise HTTPException(409, '技能正文超限或已变化，请重新安装可信插件包')
            try:
                body = raw.decode('utf-8-sig')
            except UnicodeError as exc:
                raise HTTPException(422, '插件技能必须为 UTF-8 Markdown') from exc
            # Recheck registry + full package after file read, avoiding stale loaded
            # text when a disable/update/uninstall races a slow read.
            _live(current['id'], root, current['digest'], current['revision'], current['_trust_fingerprint'])
            metadata = next((s for s in skill_metadata(root=root) if s['id'] == item_id), None)
            if (not metadata or not metadata['available'] or metadata['digest'] != current['digest']
                    or metadata['package_revision'] != current['revision']
                    or metadata['trust_fingerprint'] != current['_trust_fingerprint']):
                raise HTTPException(409, '技能在读取期间已停用或更新，请重新加载')
            return {**metadata, 'body': body}
    raise HTTPException(404, '插件技能不存在或已被新版移除')


def tools_for(plugin_id, *, expected_digest=None, expected_revision=None, root=None):
    root, row, manifest = _live(plugin_id, root, expected_digest, expected_revision)
    _live(plugin_id, root, row['digest'], row['revision'], row['_trust_fingerprint'])
    return {'plugin_id': plugin_id, 'revision': row['revision'], 'digest': row['digest'],
            'signature_status': row['_signature_status'], 'trust_revision': row['_trust_revision'],
            'trust_fingerprint': row['_trust_fingerprint'],
            'tools': copy.deepcopy(manifest['tools']), 'mcp': copy.deepcopy(manifest.get('mcp', [])),
            'read_only': True, 'executed': False, 'authorization': 'explicit-host-preview-required'}


def request_call(plugin_id, tool_name, arguments, *, expected_digest=None, expected_revision=None, root=None):
    """Return an intent, not a token, approval, execution, connection or retry."""
    from app.plugin_store import _schema_args, PluginError
    root, row, manifest = _live(plugin_id, root, expected_digest, expected_revision)
    tool = next((t for t in manifest['tools'] if t['name'] == tool_name), None)
    if tool is None:
        raise HTTPException(404, '插件包没有此工具')
    if not isinstance(arguments, dict):
        raise HTTPException(422, '工具参数必须是 JSON 对象')
    try:
        _schema_args(arguments, tool.get('input_schema', {'type': 'object', 'properties': {}, 'additionalProperties': False}))
    except PluginError as exc:
        raise HTTPException(422, str(exc)) from exc
    _live(plugin_id, root, row['digest'], row['revision'], row['_trust_fingerprint'])
    return {'status': 'awaiting_user_preview', 'executed': False, 'authorized': False,
        'plugin_id': plugin_id, 'revision': row['revision'], 'digest': row['digest'], 'tool': tool_name,
        'args': copy.deepcopy(arguments), 'permissions': sorted(set(['python.execute'] + manifest.get('permissions', []) + tool.get('permissions', []))),
        'next_action': {'method': 'POST', 'path': f'/v1/plugin-packages/{plugin_id}/calls/preview',
                        'body': {'tool': tool_name, 'args': copy.deepcopy(arguments), 'expected_revision': row['revision']}},
        'note': '这只是未授权请求。必须在插件管理中预览准确目标并由用户批准；Agent 不得自动提交或消费授权。'}
