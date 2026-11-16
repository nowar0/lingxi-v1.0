"""Ephemeral launcher-owned UI session, never a user Key or portable backup item.

Electron/standalone launcher chooses one random boot capability via child environment
and appends it once in the loopback URL fragment. The browser exchanges it for an
HttpOnly same-origin cookie, then removes the fragment; the capability is never in
HTTP query/access logs or persistent business storage. A bootstrap capability is a
local desktop trust boundary, not a multi-user or hostile-local-process security claim.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import contextvars

current_principal=contextvars.ContextVar('lingxi_ui_principal',default=None)

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

_COOKIE='lingxi_ui_session'
_TOKEN=re.compile(r'^[A-Za-z0-9_-]{43,128}$')


class HostUiSessions:
    def __init__(self, *, clock=time.monotonic):
        self.clock=clock
        self._boot=None
        self._sessions={}
        self._lock=threading.RLock()
        self._authority=secrets.token_hex(32)

    def configure_launcher(self):
        # Host composition only. Never read this environment value during import.
        with self._lock:
            if self._boot is None:
                value=os.environ.pop('LINGXI_UI_BOOTSTRAP',None)
                if value is None:
                    return False  # Direct ASGI embedding is unauthenticated, not auto-trusted.
                if type(value) is not str or not _TOKEN.fullmatch(value):
                    raise ValueError('launcher-owned random UI bootstrap is required')
                self._boot=hashlib.sha256(value.encode('ascii')).digest()
                return True

    def exchange(self, token, current_cookie='', *, renew=False):
        with self._lock:
            if self._boot is None or not _TOKEN.fullmatch(token):
                raise HTTPException(403,{'code':'ui_bootstrap_unavailable'})
            if not hmac.compare_digest(self._boot,hashlib.sha256(token.encode('ascii')).digest()):
                raise HTTPException(403,{'code':'ui_bootstrap_invalid'})
            now=self.clock()
            # A fresh launcher proof may renew the same identity. Existing plugin
            # approvals remain owned by their UI rather than changing on renewal.
            if renew and isinstance(current_cookie,str) and _TOKEN.fullmatch(current_cookie):
                key=hashlib.sha256(current_cookie.encode('ascii')).digest()
                previous=self._sessions.get(key)
                if previous is not None:
                    self._sessions[key]=(previous[0],now+12*3600)
                    return current_cookie
            self._sessions={key:entry for key,entry in self._sessions.items() if entry[1]>now}
            if len(self._sessions)>=32:
                raise HTTPException(429,{'code':'ui_session_capacity'})
            raw=secrets.token_urlsafe(32)
            identity=secrets.token_hex(24)
            self._sessions[hashlib.sha256(raw.encode('ascii')).digest()]=(identity,now+12*3600)
            return raw

    def principal(self, request):
        raw=request.cookies.get(_COOKIE,'')
        if not _TOKEN.fullmatch(raw):
            raise HTTPException(401,{'code':'ui_auth_required'})
        with self._lock:
            found=self._sessions.get(hashlib.sha256(raw.encode('ascii')).digest())
            if found is None or found[1]<=self.clock():
                raise HTTPException(401,{'code':'ui_auth_expired'})
            from app.model_host import HostUiPrincipal
            return HostUiPrincipal(found[0],self._authority)

    def live(self, principal):
        with self._lock:
            now=self.clock()
            if (getattr(principal,'authority_fingerprint',None)!=self._authority
                    or not any(entry[0]==getattr(principal,'owner_session_id',None) and entry[1]>now
                               for entry in self._sessions.values())):
                return None
            return principal

    def close(self):
        with self._lock:
            self._sessions.clear()
            self._boot=None
            self._authority=secrets.token_hex(32)


sessions=HostUiSessions()
router=APIRouter(prefix='/v11/ui-auth',tags=['ui-auth'])


class Bootstrap(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    token:str=Field(min_length=43,max_length=128,pattern=r'^[A-Za-z0-9_-]+$')
    renew:bool=False


@router.post('/exchange')
def exchange(body:Bootstrap,request:Request):
    # LocalAccessMiddleware also gates Host/Origin; absent origin is not accepted
    # on this cookie-mint endpoint (unlike legacy CLI-compatible operations).
    origin=request.headers.get('origin')
    expected=str(request.base_url).rstrip('/')
    if origin!=expected or request.headers.get('sec-fetch-site')=='cross-site':
        raise HTTPException(403,{'code':'ui_bootstrap_origin_required'})
    cookie=sessions.exchange(body.token,request.cookies.get(_COOKIE,''),renew=body.renew)
    response=JSONResponse({'authenticated':True,'expires_in':43200})
    response.set_cookie(_COOKIE,cookie,httponly=True,samesite='strict',secure=request.url.scheme=='https',
                        max_age=43200,path='/')
    response.headers['Cache-Control']='no-store'
    return response


@router.get('/status')
def status(request:Request):
    sessions.principal(request)
    return {'authenticated':True}
