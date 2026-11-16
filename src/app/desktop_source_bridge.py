"""Launcher-only source selection registration; not a browser path permission.

Only Electron main sends its fresh per-boot secret. Renderer obtains a source ID,
never chooses a backend path through HTTP. This secret is separate from the UI
bootstrap fragment and absent in the renderer/preload/context bridge.
"""
import hmac
import os
import re
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
from app import package_host,ui_auth

_SECRET=None


def require_launcher(request: Request):
    offered=request.headers.get('x-lingxi-source-bridge','')
    if _SECRET is None or not hmac.compare_digest(offered,_SECRET):
        raise HTTPException(403,'仅桌面程序可请求此操作')


def configure_launcher():
    global _SECRET
    raw=os.environ.pop('LINGXI_SOURCE_BRIDGE_SECRET',None)
    if raw is None:return
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',raw):raise ValueError('invalid host selector boot secret')
    _SECRET=raw


class SelectedSource(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    path:str=Field(min_length=4,max_length=4000)
    kind:str=Field(pattern=r'^(candidate|compatibility_evidence)$')


router=APIRouter(prefix='/v11/desktop-source',tags=['desktop-private'])

@router.post('/restore-ready')
def restore_ready(body:dict,request:Request):
    from pathlib import Path
    from app import config,data_management,release,migrations
    offered=request.headers.get('x-lingxi-source-bridge','')
    if _SECRET is None or not hmac.compare_digest(offered,_SECRET):raise HTTPException(403,'需要桌面程序确认')
    data_management.assert_idle(config.DATA_DIR/'lingxi.db')
    value=Path(str(body.get('path','')))
    if not value.is_absolute():raise HTTPException(422,'恢复目录无效')
    try:
        value=data_management.assert_no_links(value).resolve()
        imports=data_management.controlled_import_directory().resolve()
        if not body.get('revert') and value.parent!=imports:raise ValueError('仅启用本次受控恢复目录')
        if body.get('revert') and str(value)!=body.get('previous'):raise ValueError('仅可撤回已记录的切换')
        release.check_database(value/'lingxi.db');migrations.guard_schema(value/'lingxi.db')
    except (ValueError,OSError):raise HTTPException(422,'恢复目录校核失败，当前目录保持不变') from None
    return {'path':str(value)}


@router.post('/register')
def register(body:SelectedSource,request:Request):
    offered=request.headers.get('x-lingxi-source-bridge','')
    if _SECRET is None or not hmac.compare_digest(offered,_SECRET):
        raise HTTPException(403,{'code':'native_source_selector_required'})
    principal=ui_auth.sessions.principal(request)
    from app.plugin_stage_api import get_stage_api
    return get_stage_api(package_host.initialize()).sources.register_selected_source(body.path,principal,kind=body.kind)
