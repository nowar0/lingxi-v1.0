"""User-approved standalone terminal: preview exact command, then explicit commit.

The user explicitly authorized this capability on 2026-10-27 after being told
PowerShell commands may affect files outside the selected project directory.
"""
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog
from app.db import connect
from app.workspace_ui import binding
from app.tools.executor import execute

router = APIRouter(prefix='/v9/terminal')
running = set()


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    command: str = Field(min_length=1, max_length=4000)


@router.post('/preview')
def preview(body: Preview):
    workspace_id = binding(body.session_id)
    selected = catalog.get('workspace', workspace_id)
    if not body.command.strip():
        raise HTTPException(422, '请输入具体命令')
    if os.name != 'nt':
        raise HTTPException(409, '独立 PowerShell 终端仅支持 Windows')
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    argv = [str(shell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', body.command]
    token = uuid4().hex
    document = {'session_id': body.session_id, 'command': body.command, 'cwd': selected['path'],
                'workspace_id': workspace_id, 'workspace_revision': selected['revision'],
                'argv': argv, 'expires': time.time() + 600, 'status': 'prepared'}
    catalog.put('ui-command', token, document, 0)
    return {'token': token, **document, 'note': '命令在所选目录运行，最多 25 秒；工作目录不是系统沙箱，命令仍可影响目录外文件。'}


class Commit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(pattern=r'^[a-f0-9]{32}$')


@router.post('/commit')
async def commit(body: Commit):
    saved = catalog.get('ui-command', body.token)
    if saved['status'] == 'complete':
        return saved['result']
    key = binding(saved['session_id'])
    selected = catalog.get('workspace', key)
    if saved['expires'] < time.time() or selected['revision'] != saved['workspace_revision']:
        raise HTTPException(409, '预览已过期或工作目录已变化，请重新预览')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        from app.git_transition import assert_unclaimed
        assert_unclaimed(db,saved['session_id'])
        if db.execute("UPDATE capability_documents SET document=json_set(document,'$.status','running') WHERE kind='ui-command' AND id=? AND json_extract(document,'$.status')='prepared'", (body.token,)).rowcount != 1:
            raise HTTPException(409, '该命令已提交或结果未确认，不能自动重放')
    task = asyncio.current_task()
    running.add(task)
    state, result = 'interrupted', None
    try:
        raw = await execute('workspace_command', {'workspace_id': key, 'argv_json': json.dumps(saved['argv'])},
                            {'mode': 'work', 'session_id': saved['session_id'],
                             'workspace_revision': saved['workspace_revision']}, 35)
        result = json.loads(raw)
        state = 'complete'
        return result
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(502, '命令结果未确认，请先核对项目状态：' + str(error)) from error
    finally:
        document = {key: value for key, value in saved.items() if key not in ('id', 'revision')}
        document.update(status=state, result=result)
        catalog.put('ui-command', body.token, document, saved['revision'])
        running.discard(task)


@router.get('/history')
def history(session_id: int):
    binding(session_id)
    return {'items': [item for item in catalog.listing('ui-command') if item['session_id'] == session_id][-100:]}


async def shutdown():
    tasks = list(running)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
