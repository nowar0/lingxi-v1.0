"""Authenticate sensitive V0.5 host APIs and bind request-local token ownership."""
from starlette.requests import Request
from starlette.responses import JSONResponse
from fastapi import HTTPException
from app import ui_auth

_PREFIXES=('/v6/manage','/v11/local-models','/v1/plugin-packages','/v11/plugin-data','/v11/plugin-stage',
           '/v11/plugin-host/security','/v11/model-host','/v11/plugin-model')


class HostUiAuthMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or not scope.get('path','').startswith(_PREFIXES):
            await self.app(scope,receive,send);return
        try:
            principal=ui_auth.sessions.principal(Request(scope))
        except HTTPException as error:
            await JSONResponse({'detail':error.detail},status_code=error.status_code)(scope,receive,send);return
        token=ui_auth.current_principal.set(principal)
        try:await self.app(scope,receive,send)
        finally:ui_auth.current_principal.reset(token)
