"""Loopback-host and browser-origin checks without changing streaming behavior."""
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
UNSAFE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


class LocalAccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get('host', '')
        try:
            hostname = urlsplit('//' + host).hostname
        except ValueError:
            hostname = None
        if hostname not in LOOPBACK_HOSTS:
            await JSONResponse({'detail': '只接受本机地址访问'}, status_code=400)(scope, receive, send)
            return
        if scope['method'] in UNSAFE_METHODS:
            expected = scope.get('scheme', 'http') + '://' + host
            origin = headers.get('origin')
            referer = headers.get('referer')
            if referer and not origin:
                try:
                    parsed = urlsplit(referer)
                    origin = parsed.scheme + '://' + parsed.netloc
                except ValueError:
                    origin = 'invalid'
            if ((origin is not None and origin != expected)
                    or headers.get('sec-fetch-site') == 'cross-site'):
                await JSONResponse({'detail': '拒绝跨站修改请求'}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
