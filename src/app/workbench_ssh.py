"""Same-origin AsyncSSH adapter for the upstream DSH SSH React panel."""
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, quote
from uuid import uuid4

import asyncssh
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from app import catalog, config

class SshRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def guarded(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                return JSONResponse({'error': str(exc.detail), 'detail': exc.detail}, status_code=exc.status_code)
        return guarded


router = APIRouter(prefix='/api/dsh-ssh', route_class=SshRoute)
_connections = {}
_connecting = {}
_connection_epochs = {}
_cluster_previews = {}
_terminals = {}
_tunnels = {}
_scans = {}
MAX_FILE = 512 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024


def error(message, code=409):
    return JSONResponse({'error': message}, status_code=code)


def key(alias):
    return hashlib.sha256(alias.encode()).hexdigest()


def hosts():
    return catalog.listing('ssh-workbench-host')


def host(alias):
    value = next((h for h in hosts() if h['alias'] == alias), None)
    if not value:
        raise HTTPException(404, 'SSH 主机不存在')
    return value


def public(h):
    return {k: h.get(k, default) for k, default in {
        'alias': '', 'host': '', 'port': 22, 'user': '', 'auth': 'agent',
        'proxyJump': [], 'proxyCommand': '', 'description': '', 'environment': '',
        'tags': [], 'location': '', 'forwardAgent': False, 'createdAt': 0, 'updatedAt': 0,
    }.items()} | {'keyReady': h.get('auth') != 'key' or Path(h.get('keyPath', '')).is_file()}


def trust_key(h):
    return key(h['host'].lower() + ':' + str(h['port']))


def trusted(h):
    try:
        return catalog.get('ssh-workbench-trust', trust_key(h))
    except HTTPException:
        return None


async def scan(alias):
    h = host(alias)
    server_key = await asyncio.wait_for(asyncssh.get_server_host_key(h['host'], port=h['port']), 15)
    if server_key is None:
        raise HTTPException(502, '服务器没有提供主机密钥')
    token = uuid4().hex
    record = {'host': h['host'], 'port': h['port'], 'fingerprint': server_key.get_fingerprint(),
              'key': server_key.export_public_key('openssh').decode().strip()}
    _scans[token] = {'record': record, 'alias': alias, 'revision': h['revision'], 'at': time.monotonic()}
    previous = trusted(h)
    return {'token': token, 'host': h['host'], 'port': h['port'], 'fingerprint': record['fingerprint'],
            'trusted': bool(previous and previous.get('key') == record['key'])}


async def close_owned(saved):
    connections = saved.get('owned', [saved['connection']])
    for conn in reversed(connections):
        conn.close()
    await asyncio.gather(*(conn.wait_closed() for conn in connections), return_exceptions=True)


async def invalidate(alias):
    _connection_epochs[alias] = _connection_epochs.get(alias, 0) + 1
    affected = {alias} | {name for name, value in _connections.items()
                          if alias in value.get('aliases', (name,))}
    for ident in [i for i, t in _terminals.items() if t['alias'] in affected]:
        await close_terminal(ident)
    for ident in [i for i, t in _tunnels.items() if t['alias'] in affected]:
        await stop_tunnel(ident)
    for name in affected:
        saved = _connections.pop(name, None)
        if saved:
            await close_owned(saved)


@router.get('/hosts')
def list_hosts():
    return {'hosts': [public(h) for h in hosts()]}


async def save_host(payload, alias=None):
    if not isinstance(payload, dict):
        raise HTTPException(422, '主机配置必须是对象')
    alias = alias or payload.get('alias', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', alias):
        raise HTTPException(422, '别名限英文、数字、_、.、-，最多 64 字')
    previous = next((h for h in hosts() if h['alias'] == alias), {})
    hostname, username = payload.get('host', previous.get('host', '')), payload.get('user', previous.get('user', ''))
    if not isinstance(hostname, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,252}', hostname):
        raise HTTPException(422, '主机地址无效')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,100}', username):
        raise HTTPException(422, 'SSH 用户名无效')
    port = payload.get('port', previous.get('port', 22))
    if type(port) is not int or not 1 <= port <= 65535:
        raise HTTPException(422, '端口应为 1～65535')
    auth = payload.get('auth')
    record = {k: v for k, v in previous.items() if k not in ('id', 'revision')}
    record.update(alias=alias, host=hostname, port=port, user=username,
                  createdAt=previous.get('createdAt', int(time.time() * 1000)), updatedAt=int(time.time() * 1000))
    if auth is not None:
        if not isinstance(auth, dict) or auth.get('kind') not in ('key', 'password', 'agent'):
            raise HTTPException(422, '请选择密钥、密码或 Agent 认证')
        record['auth'] = auth['kind']
        record['keyPath'] = str(auth.get('keyPath', ''))[:3000]
        record['agentPath'] = str(auth.get('agentPath', ''))[:3000]
        for source, dest in [('password', 'password_ref'), ('passphrase', 'passphrase_ref')]:
            if auth.get(source):
                ref = previous.get(dest) or 'ssh-workbench-' + uuid4().hex
                config.save_key(ref, str(auth[source]))
                record[dest] = ref
    elif not previous:
        raise HTTPException(422, '新主机需要配置认证方式')
    for field, default in [('description', ''), ('environment', ''), ('location', ''), ('proxyCommand', ''),
                           ('proxyJump', []), ('tags', []), ('forwardAgent', False)]:
        value = payload.get(field, previous.get(field, default))
        if isinstance(default, list):
            if not isinstance(value, list) or len(value) > 30 or not all(isinstance(v, str) and len(v) <= 100 for v in value):
                raise HTTPException(422, field + '格式无效')
        elif isinstance(default, bool):
            if type(value) is not bool:
                raise HTTPException(422, field + '需要布尔值')
        elif not isinstance(value, str) or len(value) > 4000:
            raise HTTPException(422, field + '过长或格式错误')
        record[field] = value
    if record['proxyJump'] and record['proxyCommand'] and record['proxyCommand'].lower() != 'none':
        raise HTTPException(422, '跳板机与 ProxyCommand 不能同时配置')
    if alias in record['proxyJump']:
        raise HTTPException(422, '跳板机不能引用自身')
    saved = catalog.put('ssh-workbench-host', key(alias), record, previous.get('revision', 0))
    await invalidate(alias)
    return {'host': public(saved)}


@router.post('/hosts')
async def create_host(body: dict):
    if any(h['alias'] == body.get('alias') for h in hosts()):
        return error('主机别名已存在')
    return await save_host(body)


@router.post('/hosts/import-ssh-config')
async def import_ssh_config():
    # This file is read only after the user presses the upstream import control.
    filename = Path.home() / '.ssh/config'
    if not filename.is_file():
        return error('没有找到 ~/.ssh/config', 404)
    if filename.stat().st_size > 1024 * 1024:
        return error('ssh/config 超过 1 MB，请先缩小配置', 413)
    blocks, current = [], None
    for line in filename.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            fields = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not fields:
            continue
        command, values = fields[0].lower(), fields[1:]
        if command in ('host', 'match'):
            current = {'names': values, 'match': command == 'match', 'fields': {}}
            blocks.append(current)
        elif current and values:
            current['fields'].setdefault(command, ' '.join(values))
    added, skipped, parsed = 0, [], 0
    existing = {h['alias'] for h in hosts()}
    for block in blocks:
        for alias in block['names']:
            parsed += 1
            reason = 'match' if block['match'] else 'wildcard' if any(c in alias for c in '*?!') else 'existing' if alias in existing else None
            if reason:
                skipped.append({'name': alias, 'reason': reason})
                continue
            f = block['fields']
            try:
                auth = {'kind': 'key', 'keyPath': str(Path(f['identityfile']).expanduser())} if f.get('identityfile') else {'kind': 'agent'}
                await save_host({'alias': alias, 'host': f.get('hostname', alias), 'port': int(f.get('port', 22)),
                                 'user': f.get('user', os.environ.get('USERNAME', '')), 'auth': auth,
                                 'proxyJump': f.get('proxyjump', '').split(',') if f.get('proxyjump') else [],
                                 'proxyCommand': f.get('proxycommand', ''), 'forwardAgent': f.get('forwardagent', '').lower() in ('yes', 'true', 'on')})
                existing.add(alias)
                added += 1
            except (ValueError, HTTPException):
                skipped.append({'name': alias, 'reason': 'invalid'})
    return {'result': {'parsed': parsed, 'added': added, 'skipped': len(skipped), 'skippedBlocks': skipped}}


@router.patch('/hosts')
async def update_host(body: dict, alias: str):
    host(alias)
    return await save_host(body, alias)


@router.delete('/hosts')
async def delete_host(alias: str):
    h = host(alias)
    await invalidate(alias)
    catalog.put('ssh-workbench-host', h['id'], {'deleted': True}, h['revision'])
    return {'ok': True}


@router.post('/fingerprint')
async def fingerprint(body: dict):
    try:
        return await scan(str(body.get('alias', '')))
    except (OSError, asyncssh.Error, asyncio.TimeoutError):
        return error('无法读取服务器主机密钥，请检查地址与 SSH 端口', 502)


@router.post('/trust')
def trust(body: dict):
    item = _scans.pop(str(body.get('token', '')), None)
    if not item or time.monotonic() - item['at'] > 300:
        raise HTTPException(409, '扫描已过期，请重新核对指纹')
    h = host(item['alias'])
    if h['revision'] != item['revision'] or body.get('fingerprint') != item['record']['fingerprint']:
        raise HTTPException(409, '主机配置或指纹已变化')
    old = trusted(h)
    catalog.put('ssh-workbench-trust', trust_key(h), item['record'], old['revision'] if old else 0)
    return {'ok': True}


def connection_route(alias, chain=(), require_trust=True):
    """Resolve the complete graph before locking or opening any socket."""
    configured = {h['alias']: dict(h) for h in hosts()}
    route = []
    def visit(name, ancestors):
        if name in ancestors or len(ancestors) > 5:
            raise HTTPException(409, '跳板机引用循环或超过五层')
        h = configured.get(name)
        if not h:
            raise HTTPException(404, 'SSH 主机不存在：' + name)
        for jump in h.get('proxyJump', []):
            visit(jump, (*ancestors, name))
        known = trusted(h) or {}
        if not known and require_trust:
            raise HTTPException(409, '先核对并信任主机指纹：' + name)
        if route and h.get('proxyCommand') and h['proxyCommand'].lower() != 'none':
            raise HTTPException(409, '同一跳板路径不能同时使用 ProxyJump 和 ProxyCommand：' + name)
        route.append((h, known))
        if len(route) > 6:
            raise HTTPException(409, '跳板机路径超过五层')
    visit(alias, chain)
    signature = tuple((h['alias'], h['revision'], hashlib.sha256(k.get('key', '').encode()).hexdigest(),
                       _connection_epochs.get(h['alias'], 0)) for h, k in route)
    return route, signature


async def connect_hop(h, known, tunnel):
    kwargs = {'host': h['host'], 'port': h['port'], 'username': h['user'],
              'known_hosts': ([asyncssh.import_public_key(known['key'])], [], []),
              'client_keys': [], 'config': None, 'connect_timeout': 15, 'login_timeout': 30,
              'agent_forwarding': h.get('forwardAgent', False), 'keepalive_interval': 15,
              'keepalive_count_max': 2}
    if h['auth'] == 'key':
        if not h.get('keyPath') or not Path(h['keyPath']).is_file():
            raise HTTPException(409, '密钥文件不存在，请编辑主机配置')
        kwargs['client_keys'] = [h['keyPath']]
        kwargs['agent_path'] = None
        kwargs['passphrase'] = config.load_key(h['passphrase_ref']) if h.get('passphrase_ref') else None
    elif h['auth'] == 'password':
        password = config.load_key(h['password_ref']) if h.get('password_ref') else None
        if not password:
            raise HTTPException(409, '请在主机配置中重新保存密码')
        kwargs.update(password=password, agent_path=None)
    else:
        kwargs['agent_path'] = h.get('agentPath') or os.environ.get('SSH_AUTH_SOCK') or (r'\\.\pipe\openssh-ssh-agent' if os.name == 'nt' else None)
    if tunnel is not None:
        kwargs['tunnel'] = tunnel
    elif h.get('proxyCommand') and h['proxyCommand'].lower() != 'none':
        command = h['proxyCommand'].replace('%%', '\0')
        for token, value in [('%h', h['host']), ('%p', str(h['port'])), ('%r', h['user']), ('%n', h['alias'])]:
            command = command.replace(token, value)
        kwargs['proxy_command'] = shlex.split(command.replace('\0', '%'), posix=os.name != 'nt')
    return await asyncssh.connect(**kwargs)


async def connection(alias, chain=()):
    connection_route(alias, chain)
    # A root owns its entire route: no nested alias locks and no tunnel cache
    # shared with another route. Closing/replacing one route cannot orphan hops.
    async with _connecting.setdefault(alias, asyncio.Lock()):
        route, signature = connection_route(alias, chain)
        cached = _connections.get(alias)
        if cached and cached.get('signature') == signature and all(
                not c.is_closed() for c in cached.get('owned', [cached['connection']])):
            cached['last'] = time.monotonic()
            return cached['connection']
        if cached:
            _connections.pop(alias, None)
            await close_owned(cached)
        owned = []
        try:
            for h, known in route:
                conn = await connect_hop(h, known, owned[-1] if owned else None)
                owned.append(conn)
            if connection_route(alias, chain)[1] != signature:
                raise HTTPException(409, '连接期间主机配置已变化，请重新连接')
            _connections[alias] = {'connection': owned[-1], 'owned': owned,
                                  'revision': route[-1][0]['revision'], 'signature': signature,
                                  'aliases': tuple(h['alias'] for h, _ in route), 'last': time.monotonic()}
            return owned[-1]
        except BaseException:
            if owned:
                await asyncio.shield(close_owned({'connection': owned[-1], 'owned': owned}))
            raise


def exception_text(exc):
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return '主机密钥与已信任指纹不符；请独立核对后重新扫描，不会自动放行'
    if isinstance(exc, asyncssh.PermissionDenied):
        return 'SSH 认证失败，请检查用户名、密钥/密码或 Agent'
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return '连接或操作超时'
    return 'SSH 操作未完成（' + type(exc).__name__ + '）'


@router.post('/test')
async def test_host(body: dict):
    at = time.monotonic()
    try:
        await connection(str(body.get('alias', '')))
        return {'result': {'ok': True, 'latencyMs': round((time.monotonic() - at) * 1000)}}
    except Exception as exc:
        return {'result': {'ok': False, 'error': exception_text(exc)}}


async def execute(alias, command, timeout=60000, expected_route=None):
    at = time.monotonic()
    if not isinstance(command, str) or not command.strip() or len(command) > 100000:
        raise HTTPException(422, '命令不能为空且不能超过 100000 字')
    timeout = max(1000, min(int(timeout or 60000), 600000))
    proc = None
    try:
        conn = await connection(alias)
        if expected_route is not None and connection_route(alias)[1] != expected_route:
            raise HTTPException(409, '主机或跳板配置已变化，未执行此主机命令')
        proc = await conn.create_process(command, encoding='utf-8', errors='replace')
        async def read(stream):
            chunks, size = [], 0
            while True:
                value = await stream.read(32768)
                if not value:
                    break
                size += len(value.encode())
                if size > MAX_OUTPUT:
                    raise ValueError('输出超过上限')
                chunks.append(value)
            return ''.join(chunks)
        async def wait():
            stdout, stderr = await asyncio.gather(read(proc.stdout), read(proc.stderr))
            await proc.wait_closed()
            return stdout, stderr
        stdout, stderr = await asyncio.wait_for(wait(), timeout / 1000)
        return {'success': proc.exit_status == 0, 'exitCode': proc.exit_status, 'timedOut': False,
                'stdout': stdout, 'stderr': stderr, 'durationMs': round((time.monotonic() - at) * 1000)}
    except Exception as exc:
        if proc:
            proc.close()
        return {'success': False, 'exitCode': None, 'timedOut': isinstance(exc, TimeoutError),
                'stdout': '', 'stderr': '', 'error': exception_text(exc),
                'durationMs': round((time.monotonic() - at) * 1000)}


@router.post('/exec')
async def exec_command(body: dict):
    return {'result': await execute(str(body.get('alias', '')), body.get('command'), body.get('timeoutMs'))}


def cluster_selection(body):
    aliases = body.get('aliases') or []
    environment, tags = body.get('environment'), body.get('tags') or []
    if (not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases)
            or not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags)
            or (environment is not None and not isinstance(environment, str))
            or not (aliases or tags or (environment and environment.strip()))):
        raise HTTPException(422, '批量执行必须明确选择主机、环境或标签')
    selected = [h['alias'] for h in hosts() if (not aliases or h['alias'] in aliases)
                and (not environment or h.get('environment') == environment)
                and all(t in h.get('tags', []) for t in tags)]
    if not selected or len(selected) > 100:
        raise HTTPException(422, '请选择 1～100 台已配置的主机')
    return selected


@router.post('/cluster/preview')
def preview_cluster(body: dict):
    selected = cluster_selection(body)
    command = body.get('command')
    if not isinstance(command, str) or not command.strip() or len(command) > 100000:
        raise HTTPException(422, '命令不能为空且不能超过 100000 字')
    try:
        workers, timeout = int(body.get('maxWorkers') or 8), int(body.get('timeoutMs') or 60000)
    except (ValueError, TypeError):
        raise HTTPException(422, '并发数和超时必须是整数') from None
    if not 1 <= workers <= 16 or not 1000 <= timeout <= 600000:
        raise HTTPException(422, '并发数限1～16，超时限1000～600000毫秒')
    now = time.monotonic()
    for key, item in list(_cluster_previews.items()):
        if now - item['created'] > 1800:
            _cluster_previews.pop(key, None)
    if len(_cluster_previews) >= 128:
        raise HTTPException(429, '批量操作预览过多，请稍后重试')
    targets, routes, untrusted = [], {}, set()
    for alias in selected:
        route, signature = connection_route(alias, require_trust=False)
        h = route[-1][0]
        targets.append({name: h[name] for name in ('alias', 'host', 'port', 'user')})
        routes[alias] = signature
        untrusted.update(hop['alias'] for hop, known in route if not known)
    token = uuid4().hex
    _cluster_previews[token] = {'created': now, 'expires': now + 120, 'status': 'prepared',
        'targets': targets, 'routes': routes, 'command': command, 'workers': workers, 'timeout': timeout}
    return {'token': token, 'targets': targets, 'command': command, 'expires_in': 120,
            'untrusted_aliases': sorted(untrusted)}


@router.post('/cluster')
async def cluster(body: dict):
    token = body.get('token')
    if not token:
        try:
            cluster_selection(body)
        except HTTPException as exc:
            return error(exc.detail, exc.status_code)
        return error('请先预览并确认最终主机清单与具体命令', 409)
    if set(body) != {'token'} or not isinstance(token, str):
        return error('执行只接受本次确认的预览编号', 422)
    item = _cluster_previews.get(token)
    if not item:
        return error('预览不存在或服务已重启，请重新预览', 409)
    if item['status'] == 'complete':
        return {'results': item['results'], 'replayed': True, 'note': '返回原运行结果节选，未重复执行'}
    if item['status'] != 'prepared':
        return error('该批次已提交；执行中或结果未确认，不会重复执行', 409)
    if time.monotonic() > item['expires']:
        return error('确认已过期，请重新预览', 409)
    for alias, signature in item['routes'].items():
        if connection_route(alias)[1] != signature:
            return error('主机或跳板配置已变化，请重新预览最终目标', 409)
    item['status'] = 'running'
    try:
        record = catalog.put('ssh-cluster-run', token, {'status': 'running', 'targets': item['targets'],
            'command': item['command'], 'note': '服务重启后不会重放此审批'}, 0)
    except BaseException:
        item['status'] = 'prepared'
        raise
    gate = asyncio.Semaphore(item['workers'])
    async def one(target):
        async with gate:
            result = await execute(target['alias'], item['command'], item['timeout'], expected_route=item['routes'][target['alias']])
            return {'alias': target['alias'], 'ok': result['success'], **result}
    try:
        results = await asyncio.gather(*(one(target) for target in item['targets']))
        bounded = [{**r, 'stdout': r.get('stdout', '')[:4096], 'stderr': r.get('stderr', '')[:4096],
                    'cached_excerpt': len(r.get('stdout', '')) > 4096 or len(r.get('stderr', '')) > 4096} for r in results]
        item.update(status='complete', results=bounded)
        catalog.put('ssh-cluster-run', token, {'status': 'complete', 'targets': item['targets'],
                    'command': item['command'], 'results': bounded}, record['revision'])
        return {'results': results, 'targets': item['targets']}
    except BaseException:
        item['status'] = 'unknown'
        catalog.put('ssh-cluster-run', token, {'status': 'unknown', 'targets': item['targets'],
                    'command': item['command'], 'note': '请求中断，需人工核对已执行主机；不会重放'}, record['revision'])
        raise


async def stop_tunnel(ident):
    item = _tunnels.pop(ident, None)
    if item:
        item['listener'].close()
        await item['listener'].wait_closed()
    return bool(item)


def tunnel_public(item):
    return {k: v for k, v in item.items() if k not in ('listener', 'connection')}


@router.post('/tunnel')
async def tunnel(body: dict):
    action = body.get('action')
    if action == 'list':
        return {'tunnels': [tunnel_public(t) | {'state': 'failed' if t['connection'].is_closed() else 'forwarding'} for t in _tunnels.values()]}
    if action == 'stop':
        return {'ok': await stop_tunnel(body.get('tunnelId'))}
    if action == 'stop-all':
        ids = [i for i, t in _tunnels.items() if not body.get('alias') or t['alias'] == body['alias']]
        for ident in ids:
            await stop_tunnel(ident)
        return {'stopped': len(ids)}
    if action != 'start':
        return error('隧道动作无效', 422)
    if len(_tunnels) >= 20:
        return error('最多同时保持 20 条隧道')
    alias, remote = str(body.get('alias', '')), body.get('remoteHost') or '127.0.0.1'
    local, remote_port = body.get('localPort') or 0, body.get('remotePort')
    if type(local) is not int or not 0 <= local <= 65535 or type(remote_port) is not int or not 1 <= remote_port <= 65535:
        return error('端口范围无效', 422)
    if not isinstance(remote, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,252}', remote):
        return error('远程地址无效', 422)
    try:
        conn = await connection(alias)
        listener = await conn.forward_local_port('127.0.0.1', local, remote, remote_port)
        ident = uuid4().hex
        item = {'id': ident, 'alias': alias, 'localPort': listener.get_port(), 'remoteHost': remote,
                'remotePort': remote_port, 'state': 'forwarding', 'startedAt': int(time.time() * 1000),
                'listener': listener, 'connection': conn}
        _tunnels[ident] = item
        return {'tunnel': tunnel_public(item)}
    except Exception as exc:
        return error(exception_text(exc), 502)


@router.get('/ls')
async def ls(alias: str, path: str = '.'):
    try:
        conn = await connection(alias)
        async with conn.start_sftp_client() as sftp:
            entries = []
            async for item in sftp.scandir(path):
                if item.filename in ('.', '..'):
                    continue
                mode = item.attrs.permissions or 0
                entries.append({'name': item.filename, 'type': 'dir' if mode & 0o170000 == 0o040000 else 'file' if mode & 0o170000 == 0o100000 else 'other',
                                'size': item.attrs.size or 0, 'mtimeMs': (item.attrs.mtime or 0) * 1000, 'mode': mode})
                if len(entries) >= 10000:
                    break
            return {'entries': entries}
    except Exception as exc:
        return error(exception_text(exc), 502)


@router.post('/upload')
async def upload(request: Request, alias: str, remotePath: str):
    temp_root = config.DATA_DIR / 'ssh-upload-tmp'
    temp_root.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=temp_root, suffix='.upload')
    total = 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_FILE:
                    raise HTTPException(413, '单次上传上限 512 MB')
                stream.write(chunk)
    except BaseException:
        Path(filename).unlink(missing_ok=True)
        raise
    async def transfer():
        queue = asyncio.Queue()
        def progress(src, dst, done, size):
            queue.put_nowait({'type': 'progress', 'progress': {'phase': 'transferring', 'file': remotePath,
                              'transferred': done, 'total': size, 'percent': round(done / size * 100, 1) if size else 100}})
        async def put():
            try:
                conn = await connection(alias)
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(filename, remotePath, progress_handler=progress)
                queue.put_nowait({'type': 'result', 'ok': True, 'transferredBytes': total})
            except Exception as exc:
                queue.put_nowait({'type': 'result', 'ok': False, 'error': exception_text(exc)})
        task = asyncio.create_task(put())
        try:
            while True:
                frame = await queue.get()
                yield json.dumps(frame, ensure_ascii=False) + '\n'
                if frame['type'] == 'result':
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            Path(filename).unlink(missing_ok=True)
    return StreamingResponse(transfer(), media_type='application/x-ndjson')


@router.get('/download')
async def download(alias: str, remotePath: str):
    try:
        conn = await connection(alias)
        sftp = await conn.start_sftp_client()
        attrs = await sftp.stat(remotePath)
        if attrs.size is None or attrs.size > MAX_FILE:
            sftp.exit()
            return error('单次下载上限 512 MB', 413)
        stream = await sftp.open(remotePath, 'rb')
    except Exception as exc:
        return error(exception_text(exc), 502)
    async def chunks():
        try:
            while True:
                block = await stream.read(65536)
                if not block:
                    break
                yield block
        finally:
            await stream.close()
            sftp.exit()
            await sftp.wait_closed()
    name = remotePath.replace('\\', '/').split('/')[-1]
    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', name) or 'download'
    return StreamingResponse(chunks(), media_type='application/octet-stream', headers={
        'Content-Length': str(attrs.size), 'Content-Disposition': 'attachment; filename="' + safe_name + '"; filename*=UTF-8\'\'' + quote(name)})


def socket_allowed(ws):
    host_header = ws.headers.get('host', '')
    try:
        hostname = urlsplit('//' + host_header).hostname
    except ValueError:
        return False
    expected = ('https' if ws.url.scheme == 'wss' else 'http') + '://' + host_header
    return (hostname in ('127.0.0.1', 'localhost', '::1') and
            ws.client and ws.client.host in ('127.0.0.1', '::1', 'testclient') and ws.headers.get('origin') == expected)


async def close_terminal(ident):
    item = _terminals.pop(ident, None)
    if item:
        item['process'].close()
        item['reader'].cancel()
        await asyncio.gather(item['reader'], return_exceptions=True)


@router.websocket('/terminal')
async def terminal(ws: WebSocket):
    if not socket_allowed(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    item = None
    try:
        cols = max(2, min(int(ws.query_params.get('cols', 80)), 500))
        rows = max(1, min(int(ws.query_params.get('rows', 24)), 300))
        ident = ws.query_params.get('session')
        if ident:
            item = _terminals.get(ident)
            if not item or item.get('attached'):
                raise HTTPException(409, '终端会话不存在或已由另一窗口连接')
        else:
            alias = ws.query_params.get('alias', '')
            h = host(alias)
            if not trusted(h):
                fingerprint = await scan(alias)
                await ws.send_json({'type': 'auth_prompt', 'name': '核对 SSH 主机指纹',
                    'instructions': h['host'] + ':' + str(h['port']) + '\n' + fingerprint['fingerprint'] + '\n请通过可信渠道核对。确认后输入：我已核对指纹',
                    'prompts': [{'prompt': '确认', 'echo': True}]})
                response = await asyncio.wait_for(ws.receive_json(), 120)
                if response.get('type') != 'auth_response' or response.get('responses') != ['我已核对指纹']:
                    raise HTTPException(409, '未确认主机指纹，未连接')
                trust({'token': fingerprint['token'], 'fingerprint': fingerprint['fingerprint']})
            if len(_terminals) >= 8:
                raise HTTPException(409, '最多同时保留八个终端，请先断开已有终端')
            conn = await connection(alias)
            proc = await conn.create_process(term_type='xterm-256color', term_size=(cols, rows),
                                             encoding='utf-8', errors='replace')
            ident = uuid4().hex
            item = {'id': ident, 'alias': alias, 'process': proc, 'buffer': '', 'attached': None,
                    'last': time.monotonic(), 'exit': None, 'queue': asyncio.Queue(maxsize=256)}
            _terminals[ident] = item
            async def read():
                try:
                    while True:
                        data = await proc.stdout.read(32768)
                        if not data:
                            break
                        item['buffer'] = (item['buffer'] + data)[-500000:]
                        if item.get('attached'):
                            await item['queue'].put({'type': 'output', 'data': data})
                    await proc.wait_closed()
                    item['exit'] = {'type': 'exit', 'code': proc.exit_status}
                except Exception as exc:
                    item['exit'] = {'type': 'exit', 'code': None, 'error': exception_text(exc)}
                if item.get('attached'):
                    await item['queue'].put(item['exit'])
            item['reader'] = asyncio.create_task(read())
        while not item['queue'].empty():
            item['queue'].get_nowait()
        # Snapshot scrollback before the first await. Later output belongs only
        # to the live queue, avoiding a replay/live duplication race.
        scrollback = item['buffer']
        item['attached'] = ws
        item['last'] = time.monotonic()
        item['process'].change_terminal_size(cols, rows)
        await ws.send_json({'type': 'ready', 'alias': item['alias'], 'sessionId': ident})
        if scrollback:
            await ws.send_json({'type': 'output', 'data': scrollback})
        if item['exit']:
            await ws.send_json(item['exit'])
            return
        async def send():
            while True:
                frame = await item['queue'].get()
                await ws.send_json(frame)
                if frame['type'] == 'exit':
                    break
        sender = asyncio.create_task(send())
        try:
            while True:
                frame = await ws.receive_json()
                item['last'] = time.monotonic()
                if frame.get('type') == 'input':
                    value = frame.get('data', '')
                    if not isinstance(value, str) or len(value) > 65536:
                        raise HTTPException(422, '终端输入过长')
                    item['process'].stdin.write(value)
                    await item['process'].stdin.drain()
                elif frame.get('type') == 'resize':
                    item['process'].change_terminal_size(max(2, min(int(frame['cols']), 500)), max(1, min(int(frame['rows']), 300)))
                elif frame.get('type') == 'close':
                    await close_terminal(ident)
                    break
                elif frame.get('type') == 'detach':
                    break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with contextlib.suppress(Exception):
            await ws.send_json({'type': 'exit', 'code': None, 'error': exception_text(exc)})
    finally:
        if item and item.get('attached') is ws:
            item['attached'] = None
            item['last'] = time.monotonic()
        with contextlib.suppress(Exception):
            await ws.close()


async def reap():
    now = time.monotonic()
    for ident, item in list(_terminals.items()):
        if not item.get('attached') and now - item['last'] > 1800:
            await close_terminal(ident)
    used = {i['alias'] for i in _terminals.values()} | {i['alias'] for i in _tunnels.values()}
    for alias, item in list(_connections.items()):
        if alias not in used and now - item['last'] > 1800:
            await invalidate(alias)
    for token, item in list(_scans.items()):
        if now - item['at'] > 300:
            _scans.pop(token, None)


async def shutdown():
    for alias in list(_connections):
        await invalidate(alias)
