"""One-use, persisted approvals for UI-originated MCP connections.

This guards intentional launches, not the operating-system privileges of an MCP
server. Agent-originated calls retain their separate exact-tool approvals.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time
from urllib.parse import urlsplit

from fastapi import HTTPException

from app import catalog
from app.db import connect

TTL_SECONDS = 120


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS mcp_connection_approvals(
            token TEXT PRIMARY KEY, plugin_id TEXT NOT NULL, snapshot TEXT NOT NULL,
            fingerprint TEXT NOT NULL, expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TABLE IF NOT EXISTS mcp_connection_audit(
            id INTEGER PRIMARY KEY, plugin_id TEXT NOT NULL, action TEXT NOT NULL,
            fingerprint TEXT NOT NULL, status TEXT NOT NULL, at REAL NOT NULL)''')
        db.execute('DELETE FROM mcp_connection_approvals WHERE expires<? OR consumed=1', (time.time(),))


def snapshot(plugin):
    if not plugin.get('enabled') or plugin.get('deleted'):
        raise HTTPException(409, 'MCP 已停用或删除')
    value = {key: plugin.get(key) for key in (
        'id', 'revision', 'name', 'transport', 'command', 'args', 'url', 'credential_ref')}
    if plugin['transport'] == 'stdio':
        executable = shutil.which(plugin['command'])
        if not executable:
            raise HTTPException(409, '找不到 MCP 可执行程序，请检查完整路径')
        resolved = Path(executable).resolve(strict=True)
        if not resolved.is_file():
            raise HTTPException(409, 'MCP 程序不是普通文件')
        with resolved.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        value.update(executable=str(resolved), executable_sha256=digest,
                     target={'type': 'local_process', 'executable': str(resolved),
                             'args': plugin.get('args', [])})
    else:
        address = urlsplit(plugin['url'])
        value['target'] = {'type': 'network', 'scheme': address.scheme,
                           'host': address.hostname, 'port': address.port or (443 if address.scheme == 'https' else 80),
                           'url': plugin['url'], 'redirects': False}
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def preview(plugin_id):
    value = snapshot(catalog.get('plugin', plugin_id))
    digest = fingerprint(value)
    token = secrets.token_urlsafe(32)
    expires = time.time() + TTL_SECONDS
    with connect() as db:
        db.execute('DELETE FROM mcp_connection_approvals WHERE expires<? OR consumed=1', (time.time(),))
        if db.execute('SELECT COUNT(*) FROM mcp_connection_approvals').fetchone()[0] >= 64:
            raise HTTPException(429, '待确认连接过多，请取消或等待过期')
        db.execute('INSERT INTO mcp_connection_approvals VALUES(?,?,?,?,?,0)',
                   (token, plugin_id, json.dumps(value, ensure_ascii=False), digest, expires))
    return {'token': token, 'fingerprint': digest, 'expires_at': expires, 'snapshot': value,
            'action': 'discover', 'note': '本次将启动所示本地程序或连接所示地址。进程隔离不等于操作系统沙箱；工具执行仍需另行批准。'}


def consume(plugin_id, token, expected_fingerprint, allowed, *, frozen=False):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        record = db.execute("SELECT * FROM capability_documents WHERE kind='plugin' AND id=?", (plugin_id,)).fetchone()
        if not record:
            raise HTTPException(404, 'MCP 配置不存在')
        plugin = {**json.loads(record['document']), 'id': record['id'], 'revision': record['revision']}
        current = snapshot(plugin)
        live_digest = fingerprint(current)
        row = db.execute('SELECT * FROM mcp_connection_approvals WHERE token=? AND plugin_id=?',
                         (token, plugin_id)).fetchone()
        if not row or row['consumed'] or row['expires'] < time.time():
            raise HTTPException(409, '连接确认已过期、取消或使用，请重新预览')
        if row['fingerprint'] != expected_fingerprint or live_digest != expected_fingerprint:
            raise HTTPException(409, 'MCP 配置或程序已变化，请重新预览；未执行')
        db.execute('UPDATE mcp_connection_approvals SET consumed=1 WHERE token=?', (token,))
        db.execute('INSERT INTO mcp_connection_audit(plugin_id,action,fingerprint,status,at) VALUES(?,?,?,?,?)',
                   (plugin_id, 'discover', live_digest, 'approved' if allowed else 'denied', time.time()))
    if not allowed:
        return False
    if frozen:
        # The actual connector consumes this exact approved configuration, not a
        # second catalog read that might select a concurrently edited target.
        return {**plugin, '_approved_snapshot': current}
    return True


def audit_result(plugin_id, digest, status):
    with connect() as db:
        db.execute('INSERT INTO mcp_connection_audit(plugin_id,action,fingerprint,status,at) VALUES(?,?,?,?,?)',
                   (plugin_id, 'discover', digest, status, time.time()))


def process_environment():
    """Never inherit API tokens or user model credentials into a child server."""
    # MCP stdio is an older connection adapter, not the package worker. Keep only
    # loader/runtime variables; never inherit profile, data-root or user temp paths.
    allowed = ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL')
    return {key: os.environ[key] for key in allowed if key in os.environ}
