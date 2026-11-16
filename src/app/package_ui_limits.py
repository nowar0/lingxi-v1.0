"""Bound declarative UI bodies before JSON/Pydantic parsing; no other routes affected."""
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

MAX_UI_HTTP_BYTES = 65536


class PackageUiBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        protected = (scope['type'] == 'http' and scope.get('method') == 'POST'
                     and ((path.startswith('/v1/plugin-packages/')
                           and (path.endswith('/ui/open') or path.endswith('/ui/rpc')))
                          or path in ('/v11/model-documents/preview', '/v11/model-documents/run',
                                      '/v11/model-documents/cancel')))
        if not protected:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        length = headers.get('content-length')
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                declared = -1
            if declared < 0:
                await JSONResponse({'detail':'UI 请求长度无效'},status_code=400)(scope,receive,send)
                return
            if declared > MAX_UI_HTTP_BYTES:
                await JSONResponse({'detail':'UI 请求超过64 KiB限制'},status_code=413)(scope,receive,send)
                return
        chunks=[]; total=0
        while True:
            event=await receive()
            if event['type']=='http.disconnect':
                return
            if event['type']!='http.request':
                continue
            body=event.get('body',b'');total+=len(body)
            if total>MAX_UI_HTTP_BYTES:
                await JSONResponse({'detail':'UI 请求超过64 KiB限制'},status_code=413)(scope,receive,send)
                return
            chunks.append(body)
            if not event.get('more_body',False):
                break
        replayed=False
        async def bounded_receive():
            nonlocal replayed
            if not replayed:
                replayed=True
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        await self.app(scope,bounded_receive,send)
