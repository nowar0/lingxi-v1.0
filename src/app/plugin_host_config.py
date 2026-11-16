"""Explicit portable strict-plugin host setup; settings are not execution approval.

No directory is granted or AppContainer created here. A user selects an existing
ordinary parent; every future call still previews its unique root/profile/SID/ACLs.
The one-shot audit approval is never imported as permanent product configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import config, ui_auth
from app.plugin_sandbox_policy import canonical_local_path

_LOCK=threading.RLock()
_PENDING={}


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def settings_path():
    return config.DATA_DIR/'plugin-security'/'host-settings.json'


def _ordinary(path, *, missing=False):
    path=Path(os.path.abspath(path))
    for parent in (path,*path.parents):
        if parent.is_symlink() or parent.is_junction() or parent.exists() and getattr(parent.lstat(),'st_file_attributes',0)&0x400:
            raise ValueError('linked or reparse path refused')
    if not path.exists():
        if missing:return None
        raise ValueError('existing ordinary directory required')
    info=path.lstat()
    return {'volume':str(info.st_dev),'file_id':str(info.st_ino),'directory':path.is_dir()}


def read_settings():
    path=settings_path()
    with _LOCK:
        if not path.exists():return None
        identity=_ordinary(path)
        if identity['directory'] or path.stat().st_size>16384 or path.stat().st_nlink!=1:
            raise ValueError('invalid strict plugin settings file')
        value=json.loads(path.read_text(encoding='utf-8'))
        if (type(value) is not dict or set(value)!={'schema','parent','parent_identity','host_parent_approved','approval_semantics'}
                or value['schema']!='lingxi.strict-host-settings.v1' or value['host_parent_approved'] is not True
                or value['approval_semantics']!='parent_preparation_only_each_invocation_requires_exact_approval'):
            raise ValueError('unknown strict plugin settings')
        canonical_local_path(value['parent'])
        if _ordinary(value['parent'])!=value['parent_identity']:
            raise ValueError('approved plugin parent identity changed')
        return value


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class Setup(Strict):
    parent:str=Field(min_length=4,max_length=4000)


class Commit(Strict):
    token:str=Field(pattern=r'^[A-Za-z0-9_-]{43}$')
    fingerprint:str=Field(pattern=r'^[0-9a-f]{64}$')
    user_confirmed:bool


router=APIRouter(prefix='/v11/plugin-host/security',tags=['plugin-host'])


@router.get('')
def status(request:Request):
    ui_auth.sessions.principal(request)
    try:
        saved=read_settings()
        from app import package_host
        manager=package_host.initialize()
        return {'configured':saved is not None,'settings':saved,
                'ordinary':manager.strict_runtime_status('ordinary'),'mcp':manager.strict_runtime_status('mcp'),
                'execution_granted':False,'notice':'父目录许可不等于调用许可；每次仍须查看并批准完整专用身份和逐路径权限'}
    except (ValueError,OSError):
        raise HTTPException(409,{'code':'strict_host_settings_invalid'}) from None


@router.post('/preview')
def preview(body:Setup,request:Request):
    principal=ui_auth.sessions.principal(request)
    try:
        canonical_local_path(body.parent)
        if any(part.casefold() in {'appdata','credentials','vault'} for part in body.parent.split('\\')):
            raise ValueError('protected run parent refused')
        identity=_ordinary(body.parent)
        if not identity['directory']:raise ValueError('directory required')
        snapshot={'schema':'lingxi.strict-host-settings.v1','parent':body.parent,'parent_identity':identity,
            'host_parent_approved':True,
            'approval_semantics':'parent_preparation_only_each_invocation_requires_exact_approval'}
        fingerprint=_hash(snapshot)
        token=secrets.token_urlsafe(32)
        with _LOCK:
            now=time.monotonic()
            for key,item in list(_PENDING.items()):
                if item[2]<=now:_PENDING.pop(key)
            if len(_PENDING)>=32:raise HTTPException(429,{'code':'strict_setup_capacity'})
            _PENDING[token]=(principal,snapshot,now+120)
        return {'token':token,'fingerprint':fingerprint,'snapshot':snapshot,'expires_in':120,
            'native_actions_performed':False,'execution_granted':False,
            'notice':'保存配置仅允许未来在此父目录准备完整预览；不修改父ACL，不自动创建profile或执行插件'}
    except (ValueError,OSError):
        raise HTTPException(422,{'code':'strict_parent_not_ordinary'}) from None


@router.post('/commit')
def commit(body:Commit,request:Request):
    principal=ui_auth.sessions.principal(request)
    from app import package_host
    with package_host._lock, _LOCK:
        record=_PENDING.pop(body.token,None)
        if (record is None or record[0]!=principal or record[2]<=time.monotonic()
                or body.user_confirmed is not True or _hash(record[1])!=body.fingerprint):
            raise HTTPException(409,{'code':'strict_setup_confirmation_invalid'})
        snapshot=record[1]
        if _ordinary(snapshot['parent'])!=snapshot['parent_identity']:
            raise HTTPException(409,{'code':'strict_parent_identity_changed'})
        from app import package_host
        current=package_host.initialize()
        calls=current.execution_coordinator.snapshot()
        stage_service=getattr(current,'_host_plugin_stage_service',None)
        if (any(not item['finalized'] or item['resources_active'] for item in calls['calls'])
                or stage_service is not None and any(item['phase'] not in ('validated','completed_disabled','failed_cleaned')
                    for item in stage_service._executions.values())):
            raise HTTPException(409,{'code':'strict_runtime_busy'})
        path=settings_path()
        _ordinary(path.parent,missing=True)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name('.host-settings-'+secrets.token_hex(16)+'.tmp')
        raw=json.dumps(snapshot,ensure_ascii=False,indent=2).encode()+b'\n'
        with temporary.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    # Never acquire package-host lock underneath the settings lock. Both config
    # readers and host rotations use package-host -> settings ordering.
    package_host.shutdown()
    package_host.initialize()
    return {'saved':True,'execution_granted':False,'native_actions_performed':False}
