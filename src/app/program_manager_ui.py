"""Independent, loopback-only portable manager host; no chat/config import.

Import/help do not inspect files or initialize user data. CLI starts only after an
explicit root is supplied. This session protects local web requests, not a hostile
same-user process. Each program transaction still requires its separate approval.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sys
import threading
import time
import webbrowser

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from app.program_manager_api import ManagerService, create_router

MAX_REQUEST_BYTES = 16384
ABSOLUTE_TTL = 3600
IDLE_TTL = 900
_HEX = re.compile(r'^[0-9a-f]{64}$')
_SECURITY = {
    'cache-control': 'no-store', 'referrer-policy': 'no-referrer',
    'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY',
    'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def _hash(value):
    return hashlib.sha256(value.encode('ascii')).digest()


def _request_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


class ManagerSession:
    """One bootstrap exchange, then a memory-only token bound to the host lifetime."""
    def __init__(self, bootstrap, *, clock=time.monotonic):
        if not isinstance(bootstrap, str) or not _HEX.fullmatch(bootstrap):
            raise ValueError('exact random bootstrap required')
        self.bootstrap_hash = _hash(bootstrap)
        self.token_hash = None
        self.clock = clock
        self.created = self.last_seen = clock()
        self._lock = threading.Lock()

    def exchange(self, supplied):
        with self._lock:
            if (self.clock() - self.created > 120 or self.bootstrap_hash is None
                    or not isinstance(supplied, str) or not _HEX.fullmatch(supplied)):
                return None
            if not hmac.compare_digest(self.bootstrap_hash, _hash(supplied)):
                return None
            self.bootstrap_hash = None
            token = secrets.token_hex(32)
            self.token_hash = _hash(token)
            self.last_seen = self.clock()
            return token

    def authorize(self, supplied):
        with self._lock:
            now = self.clock()
            if (self.token_hash is None or now-self.created >= ABSOLUTE_TTL
                    or now-self.last_seen >= IDLE_TTL or not isinstance(supplied, str)
                    or not _HEX.fullmatch(supplied)
                    or not hmac.compare_digest(self.token_hash, _hash(supplied))):
                return False
            self.last_seen = now
            return True


class ManagerBoundary:
    def __init__(self, app, *, origin, session):
        self.app, self.origin, self.session = app, origin, session
        self.host = origin.removeprefix('http://')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            await self.app(scope, receive, send)
            return
        async def secured(message):
            if message['type'] == 'http.response.start':
                original = [(key, value) for key, value in message.get('headers', [])
                            if key.decode('latin1').lower() not in _SECURITY]
                message = {**message, 'headers': original+[(key.encode(), value.encode()) for key, value in _SECURITY.items()]}
            await send(message)
        async def deny(status, code):
            await JSONResponse({'detail': {'code': code, 'message': '管理器请求未授权或已过期；没有执行操作'}}, status_code=status)(scope, receive, secured)
        raw_headers = scope.get('headers', [])
        headers = {}
        for key, value in raw_headers:
            name = key.decode('latin1').lower()
            if name in headers:
                await deny(400, 'duplicate_header')
                return
            headers[name] = value.decode('latin1')
        if headers.get('host') != self.host or scope.get('query_string', b''):
            await deny(403, 'host_or_query_refused')
            return
        path, method = scope['path'], scope['method']
        is_api = path == '/v11/program-manager' or path.startswith('/v11/program-manager/')
        exchange = path == '/manager/session'
        if method not in ('GET', 'HEAD', 'POST'):
            await deny(405, 'method_refused')
            return
        if headers.get('origin') not in (None, self.origin):
            await deny(403, 'origin_refused')
            return
        if method == 'POST':
            if headers.get('origin') != self.origin or headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                await deny(403, 'origin_or_content_type_required')
                return
            if not (is_api or exchange):
                await deny(404, 'route_refused')
                return
        if is_api and not self.session.authorize(headers.get('x-lingxi-manager-session')):
            await deny(401, 'session_required')
            return
        if exchange and method != 'POST':
            await deny(405, 'exchange_requires_post')
            return
        payload = b''
        if method == 'POST':
            declared = headers.get('content-length')
            if declared is not None and (not declared.isascii() or not declared.isdecimal() or int(declared) > MAX_REQUEST_BYTES):
                await deny(413, 'request_too_large')
                return
            try:
                async with asyncio.timeout(10):
                    while True:
                        part = await receive()
                        if part['type'] == 'http.disconnect':
                            return
                        payload += part.get('body', b'')
                        if len(payload) > MAX_REQUEST_BYTES:
                            await deny(413, 'request_too_large')
                            return
                        if not part.get('more_body', False):
                            break
            except TimeoutError:
                await deny(408, 'request_body_timeout')
                return
        if method == 'POST' and not exchange:
            try:
                if type(_request_json(payload)) is not dict:
                    raise ValueError('object required')
            except (ValueError, UnicodeError, RecursionError):
                await deny(400, 'strict_json_required')
                return
        if exchange:
            try:
                value = _request_json(payload)
                if type(value) is not dict or set(value) != {'bootstrap'}:
                    raise ValueError('shape')
                token = self.session.exchange(value['bootstrap'])
            except (ValueError, UnicodeError, RecursionError):
                token = None
            if token is None:
                await deny(401, 'bootstrap_refused')
            else:
                await JSONResponse({'session_token': token})(scope, receive, secured)
            return
        used = False
        async def replay():
            nonlocal used
            if not used:
                used = True
                return {'type': 'http.request', 'body': payload, 'more_body': False}
            return await receive()
        await self.app(scope, replay if method == 'POST' else receive, secured)


def create_manager_app(service, static_root, *, origin, bootstrap_secret=None, clock=time.monotonic):
    """Return (ASGI app, one-use bootstrap); explicit roots are host-owned capabilities."""
    if type(service) is not ManagerService or not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', origin):
        raise ValueError('explicit manager service and loopback origin required')
    port = int(origin.rsplit(':', 1)[1])
    if port > 65535:
        raise ValueError('invalid port')
    root = Path(static_root)
    if not root.is_absolute() or not root.is_dir() or any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        raise ValueError('explicit ordinary static root required')
    files = tuple(root.rglob('*'))
    if len(files) > 1000 or not (root/'manager.html').is_file():
        raise ValueError('independent manager page missing or excessive')
    for item in files:
        if item.is_symlink() or item.is_junction() or item.is_file() and item.stat().st_nlink != 1:
            raise ValueError('unsafe manager asset')
    bootstrap = bootstrap_secret or secrets.token_hex(32)
    session = ManagerSession(bootstrap, clock=clock)
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(create_router(service))
    application.mount('/', StaticFiles(directory=root), name='manager-static')
    return ManagerBoundary(application, origin=origin, session=session), bootstrap


@contextmanager
def _manager_signals(server):
    """Own UI shutdown signals without replaying them to the console defaults."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = (signal.SIGINT, signal.SIGTERM)
    if hasattr(signal, 'SIGBREAK'):
        signals += (signal.SIGBREAK,)
    original_handlers = {}
    try:
        for sig in signals:
            original_handlers[sig] = signal.signal(sig, server.handle_exit)
        yield
    finally:
        # Uvicorn's handle_exit retains graceful/second-SIGINT force-exit behavior.
        # Replaying SIGBREAK after restoration instead triggers Windows CRT abort.
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立便携程序管理界面；不启动聊天或读取默认用户数据', allow_abbrev=False)
    parser.add_argument('--allow-root', type=Path, required=True)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    if args.no_browser:
        parser.error('--no-browser 尚无可信启动能力交付渠道，拒绝启动不可访问的管理服务；请使用固定离线CLI')
    root = args.allow_root
    if (not root.is_absolute() or '..' in root.parts or str(root).startswith(('\\\\', '//'))
            or not root.is_dir() or root.parent == root
            or any(p.is_symlink() or p.is_junction() for p in (root, *root.parents))):
        parser.error('必须明确选择已存在的普通便携根目录；不接受盘根、链接、网络路径或自动扫描')
    state, data = root/'transactions', root/'data'
    for name in ('APPDATA', 'LOCALAPPDATA'):
        value = os.environ.get(name)
        if value:
            protected = Path(value).absolute()
            if root == protected or root.is_relative_to(protected) or protected.is_relative_to(root):
                parser.error('便携根不能包含或位于默认用户数据目录')
    own_root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    service = ManagerService(root, state, data, protected_program_roots=(root/'manager', own_root))
    if getattr(sys, 'frozen', False):
        static = Path(sys._MEIPASS)/'static/program-manager'
    else:
        static = Path(__file__).resolve().parents[1]/'frontend/dist-manager'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(('127.0.0.1', 0))
        reserved.listen(32)
        origin = f'http://127.0.0.1:{reserved.getsockname()[1]}'
        application, bootstrap = create_manager_app(service, static, origin=origin)
        url = origin+'/manager.html#bootstrap='+bootstrap
        # Never print bootstrap/session secrets, even if default browser launch fails.
        import uvicorn
        class ManagerServer(uvicorn.Server):
            def capture_signals(self):
                return _manager_signals(self)
        server = ManagerServer(uvicorn.Config(application, host='127.0.0.1', access_log=False,
                                               log_level='warning', proxy_headers=False))
        browser_failed = threading.Event()
        def open_manager():
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            if not opened:
                browser_failed.set()
                server.should_exit = True
                print('无法打开默认浏览器，管理服务正在退出；未显示或记录启动能力。可使用固定离线CLI。', file=sys.stderr)
        timer = threading.Timer(0.7, open_manager)
        timer.daemon = True
        timer.start()
        print('便携管理器已启动；目录仅由本次宿主选择，Ctrl+C退出。')
        try:
            server.run(sockets=[reserved])
        finally:
            timer.cancel()
        if browser_failed.is_set():
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
