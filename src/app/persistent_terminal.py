"""Explicit per-command approvals for a persistent, noninteractive shell."""
import asyncio
import os
from threading import RLock
import time
from uuid import uuid4
from fastapi import APIRouter,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from app import catalog
from app.workspace_ui import binding
from app.terminal_process import Shell

router=APIRouter(prefix='/v9/terminal/persistent')
_shells={};_lock=RLock();_reservations=set()


def _get(sid,selected):
    shell=_shells.get(sid)
    if shell and (not shell.snapshot()['alive'] or shell.root!=selected['path']):
        if shell.snapshot()['active'] or shell.snapshot()['background']:raise HTTPException(409,'请先停止当前命令及后台进程，再切换项目目录')
        shell.close();_shells.pop(sid,None);shell=None
    if shell:shell.workspace_revision=selected['revision']
    return shell


def _finish(token,status,result):
    try:
        with catalog.transaction():
            item=catalog.get('persistent-command',token)
            if item['status']!='running':return
            catalog.put('persistent-command',token,{**{k:v for k,v in item.items() if k not in ('id','revision')},'status':status,'result':result},item['revision'])
    except BaseException:
        # A missing final ledger must remain unknown, never trigger replay.
        # History reconciles a nonrunning shell against any remaining row.
        pass


def _selection(sid):
    selected=catalog.get('workspace',binding(sid))
    if os.name!='nt':raise HTTPException(409,'持续PowerShell终端仅支持Windows')
    return selected


class Preview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)
    command:str=Field(min_length=1,max_length=4000)


@router.post('/preview')
def preview(body:Preview):
    from app import sessions
    selected=_selection(body.session_id)
    if not body.command.strip():raise HTTPException(422,'请输入具体命令')
    with _lock:
        for sid,shell in list(_shells.items()):
            state=shell.snapshot();session=sessions.get_session(sid)
            if not state['active'] and not state['background'] and (time.monotonic()-shell.last_used>1800 or not session or session['archived']):
                shell.close();_shells.pop(sid,None)
        shell=_get(body.session_id,selected)
        state=shell.snapshot() if shell else None
        if state and state['active']:raise HTTPException(409,'此终端仍有命令执行中，请先停止或等待')
        token=uuid4().hex
        doc={'session_id':body.session_id,'command':body.command,'cwd':state['cwd'] if state else selected['path'],
             'workspace_revision':selected['revision'],'generation':state['generation'] if state else None,
             'sequence':state['sequence'] if state else 0,'expires':time.time()+120,'status':'prepared','result':None}
        catalog.put('persistent-command',token,doc,0)
    return {'token':token,**doc,'note':'命令逐条确认；保留当前目录和变量。此终端不接受交互输入，不是系统沙箱；输出最多保留末尾100万字符。停止会结束本终端及其子进程。'}


class Token(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token:str=Field(pattern=r'^[a-f0-9]{32}$')


@router.post('/commit')
async def commit(body:Token):
    # Even a lost HTTP response cannot submit this approval twice. The process
    # lifetime is owned by the terminal and is independent of this HTTP request.
    return await asyncio.to_thread(_commit,body.token)


def _commit(token):
    with _lock:
        reserved_sid=None
        try:
            with catalog.transaction():
                item=catalog.get('persistent-command',token)
                if item['status']!='prepared':return _public(item)
                from app.git_transition import assert_unclaimed
                with catalog.read_connection() as db:assert_unclaimed(db,item['session_id'])
                selected=_selection(item['session_id']);shell=_get(item['session_id'],selected)
                state=shell.snapshot() if shell else None
                if (item['expires']<time.time() or item['workspace_revision']!=selected['revision']
                        or (state['generation'] if state else None)!=item['generation']
                        or (state['sequence'] if state else 0)!=item['sequence'] or state and state['active']):
                    raise HTTPException(409,'预览已过期，或终端目录/状态已改变，请重新预览')
                if shell is None and len(_shells)>=8:raise HTTPException(409,'最多保留8个终端，请先停止不用的终端')
                doc={k:v for k,v in item.items() if k not in ('id','revision')}
                doc['status']='running'
                item=catalog.put('persistent-command',token,doc,item['revision'])
                reserved_sid=item['session_id']
                _reservations.add(reserved_sid)
        except BaseException:
            if reserved_sid is not None:_reservations.discard(reserved_sid)
            raise
        try:
            if shell is None:
                shell=Shell(selected['path'],selected['revision'],_finish);_shells[item['session_id']]=shell
            shell.run(token,item['command'])
            _reservations.discard(item['session_id'])
        except BaseException as error:
            _reservations.discard(item['session_id'])
            _finish(token,'unknown',{'cwd':item['cwd'],'exit_code':None,'output':str(error),'limited':False})
            if shell:shell.close();_shells.pop(item['session_id'],None)
            raise HTTPException(502,'终端执行未确认，请先查看记录，不会自动重放') from error
    # A short wait preserves a useful immediate response for small commands.
    # Long jobs continue with live output and an explicit stop action.
    shell.finished.wait(.25)
    return _public(catalog.get('persistent-command',token))


def _public(item):
    result={**item}
    shell=_shells.get(item['session_id'])
    state=shell.snapshot() if shell else None
    if item['status']=='running':
        if state and state['active']==item['id']:
            result['result']={'cwd':state['cwd'],'exit_code':None,'output':state['output'],'limited':state['limited']}
        else:
            # Restart or final-write failure: expose unknown, never relaunch.
            result['status']='unknown'
    return result


@router.get('/history')
def history(session_id:int=Query(gt=0)):
    selected=_selection(session_id)
    with _lock:
        shell=_get(session_id,selected);state=shell.snapshot() if shell else None
        return {'items':[_public(item) for item in catalog.listing('persistent-command') if item['session_id']==session_id][-100:],
                'cwd':state['cwd'] if state else selected['path'],'running':bool(state and state['active']),
                'persistent':bool(state),'background':bool(state and state['background']),'interactive':False}


class Stop(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)


@router.post('/stop')
async def stop(body:Stop):
    _selection(body.session_id)
    await asyncio.to_thread(close_session,body.session_id)
    return {'stopped':True,'session_id':body.session_id}


def close_session(sid):
    with _lock:
        shell=_shells.pop(sid,None)
        if shell:shell.close()


def is_running(sid):
    # Called while session mutations hold the SQLite write transaction. Do
    # not acquire the manager lock here (commit acquires it before SQLite).
    if sid in _reservations:return True
    shell=_shells.get(sid)
    return bool(shell and (shell.snapshot()['active'] or shell.snapshot()['background']))


async def shutdown():
    for sid in list(_shells):await asyncio.to_thread(close_session,sid)
