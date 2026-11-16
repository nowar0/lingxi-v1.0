"""Windows ConPTY workbench, with a human preview for every input write."""
import asyncio
import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
from threading import Event, RLock, Thread
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import advanced_approval
from app.terminal_process import ProcessJob

router = APIRouter(prefix='/v9/terminal/pty')
_sessions = {}
_lock = RLock()
_MAX_OUTPUT = 500_000
_KEYS = {'Enter': '\r', 'Tab': '\t', 'Escape': '\x1b', 'Up': '\x1b[A',
         'Down': '\x1b[B', 'Right': '\x1b[C', 'Left': '\x1b[D'}


def is_available():
    if os.name != 'nt':
        return False
    try:
        from winpty import PtyProcess
        return bool(PtyProcess and ctypes.windll.kernel32.CreatePseudoConsole)
    except (ImportError, AttributeError, OSError):
        return False


def attach_job(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ProcessJob(SimpleNamespace(_handle=handle))
    finally:
        kernel.CloseHandle(handle)


class Terminal:
    def __init__(self, workspace, rows, columns):
        from winpty import PtyProcess
        self.workspace = workspace
        self.generation = uuid4().hex
        self.sequence = 0
        self.cwd = workspace['path']
        self.rows, self.columns = rows, columns
        self.offset = 0
        self.output = ''
        self.closed = False
        self.lock = RLock()
        self.ready = Event()
        marker = '__LINGXI_PTY_' + self.generation + '__'
        self.marker = re.compile(re.escape(marker) + r'([A-Za-z0-9+/=]{1,8000})__END__')
        initialize = (
            "$ProgressPreference='SilentlyContinue'; "
            "function global:prompt { $lx=(Get-Location).Path; "
            "[Console]::Write('" + marker + "' + [Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($lx)) + '__END__'); 'PS ' + $lx + '> ' }"
        )
        encoded = base64.b64encode(initialize.encode('utf-16-le')).decode('ascii')
        exe = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        safe_env = {key: value for key, value in os.environ.items()
                    if key not in ('LINGXI_UI_BOOTSTRAP', 'LINGXI_DATA_DIR')}
        self.process = PtyProcess.spawn([str(exe), '-NoLogo', '-NoProfile', '-NoExit', '-EncodedCommand', encoded],
            cwd=self.cwd, env=safe_env, dimensions=(rows, columns), backend='0')
        self.job = None
        try:
            self.job = attach_job(self.process.pid)
            Thread(target=self.read, name='lingxi-conpty-output', daemon=True).start()
            if not self.ready.wait(10) or not self.sequence:
                raise RuntimeError('交互式 PowerShell 没有返回首个提示符')
        except BaseException:
            self.close()
            raise

    def append(self, text):
        self.output += text
        if len(self.output) > _MAX_OUTPUT:
            excess = len(self.output) - _MAX_OUTPUT
            self.output = self.output[excess:]
            self.offset += excess

    def read(self):
        pending = ''
        marker_start = '__LINGXI_PTY_' + self.generation + '__'
        try:
            while not self.closed:
                pending += self.process.read(2048)
                with self.lock:
                    for match in list(self.marker.finditer(pending)):
                        try:
                            self.cwd = base64.b64decode(match[1], validate=True).decode('utf-8')
                            self.sequence += 1
                            self.ready.set()
                        except (ValueError, UnicodeError):
                            pass
                    pending = self.marker.sub('', pending)
                    # Retain a partial marker across reads without delaying
                    # normal terminal output, including prompts without a LF.
                    index = pending.rfind(marker_start)
                    if index < 0:
                        length = min(len(pending), len(marker_start)-1)
                        index = len(pending)
                        for suffix in range(length, 0, -1):
                            if pending.endswith(marker_start[:suffix]):
                                index = len(pending)-suffix
                                break
                    if len(pending)-index > 9000:
                        index = len(pending)
                    self.append(pending[:index])
                    pending = pending[index:]
        except (EOFError, OSError, RuntimeError):
            pass
        finally:
            self.ready.set()

    def alive(self):
        return not self.closed and self.process.isalive()

    def snapshot(self, offset=0):
        with self.lock:
            position = self.offset + len(self.output)
            reset = offset < self.offset or offset > position
            return {'alive': self.alive(), 'generation': self.generation, 'cwd': self.cwd,
                    'sequence': self.sequence, 'rows': self.rows, 'columns': self.columns,
                    'cursor': position, 'reset': reset,
                    'output': self.output[0 if reset else offset-self.offset:]}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        if self.job:
            self.job.close()
        self.process.close(force=True)


class Session(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)


class Size(Session):
    rows: int = Field(default=24, ge=5, le=100)
    columns: int = Field(default=80, ge=20, le=260)


class Input(Session):
    text: str = Field(min_length=1, max_length=8000)
    enter: bool = True
    interrupt: bool = False
    key: Literal['', 'Enter', 'Tab', 'Escape', 'Up', 'Down', 'Left', 'Right'] = ''


def terminal(session_id, workspace):
    value = _sessions.get(session_id)
    if value and value.workspace != workspace:
        raise HTTPException(409, '工作目录已变化，请关闭旧终端后重新连接')
    return value


def state(value, workspace):
    return (workspace['id'], workspace['revision'], value.generation, value.sequence)


@router.get('/status')
def status(session_id: int, cursor: int = Query(default=0, ge=0)):
    workspace = advanced_approval.workspace(session_id)
    with _lock:
        value = terminal(session_id, workspace)
        return {'available': is_available(), **(value.snapshot(cursor) if value else
                {'alive': False, 'cwd': workspace['path'], 'cursor': 0, 'output': ''})}


@router.post('/open')
def open_terminal(body: Size):
    workspace = advanced_approval.workspace(body.session_id)
    if not is_available():
        raise HTTPException(409, '交互终端需要 Windows ConPTY')
    with _lock:
        value = terminal(body.session_id, workspace)
        if value and value.alive():
            return value.snapshot()
        if value:
            value.close()
            _sessions.pop(body.session_id, None)
        if len(_sessions) >= 8:
            raise HTTPException(409, '最多同时保留 8 个交互终端')
        try:
            value = _sessions[body.session_id] = Terminal(workspace, body.rows, body.columns)
            return value.snapshot()
        except (RuntimeError, OSError) as exc:
            raise HTTPException(502, '交互终端启动失败：' + str(exc)[:160]) from exc


@router.post('/resize')
def resize(body: Size):
    workspace = advanced_approval.workspace(body.session_id)
    with _lock:
        value = terminal(body.session_id, workspace)
        if not value or not value.alive():
            raise HTTPException(409, '请先连接交互终端')
        value.process.setwinsize(body.rows, body.columns)
        value.rows, value.columns = body.rows, body.columns
    return {'rows': body.rows, 'columns': body.columns}


@router.post('/preview')
def preview(body: Input):
    workspace = advanced_approval.workspace(body.session_id)
    if body.interrupt and (body.text != 'Ctrl+C' or body.enter):
        raise HTTPException(422, '中断需单独预览 Ctrl+C')
    if body.key and (body.text != body.key or body.enter or body.interrupt):
        raise HTTPException(422, '单键需单独预览')
    if not body.interrupt and any(ord(char) < 32 and char not in '\t\r\n' for char in body.text):
        raise HTTPException(422, '输入含有不可见控制字符')
    with _lock:
        value = terminal(body.session_id, workspace)
        if not value or not value.alive():
            raise HTTPException(409, '请先连接交互终端')
        with value.lock:
            return advanced_approval.prepare('pty', body.session_id, state(value, workspace), body,
                {'cwd': value.cwd, 'text': body.text, 'enter': body.enter, 'interrupt': body.interrupt, 'key': body.key})


@router.post('/commit')
def commit(body: advanced_approval.Commit):
    from app import catalog
    from app.git_transition import assert_unclaimed
    workspace = advanced_approval.workspace(body.session_id)
    with _lock:
        value = terminal(body.session_id, workspace)
        if not value or not value.alive():
            raise HTTPException(409, '终端已断开，请重新连接')
        with value.lock:
            action = advanced_approval.take('pty', body, state(value, workspace))
            with catalog.read_connection() as db:
                assert_unclaimed(db, body.session_id)
            if action.interrupt:
                text = '\x03'
            elif action.key:
                text = _KEYS[action.key]
            else:
                text = action.text.replace('\r\n', '\n').replace('\n', '\r')
            value.sequence += 1
        try:
            value.process.write(text + ('\r' if action.enter else ''))
        except (OSError, RuntimeError) as exc:
            raise HTTPException(502, '输入结果未知，请查看终端；确认不会自动重放') from exc
    return {'sent': True, 'generation': value.generation}


@router.post('/close')
def close(body: Session):
    advanced_approval.require_work(body.session_id)
    close_session(body.session_id)
    return {'closed': True}


def is_running(sid):
    value = _sessions.get(sid)
    return bool(value and value.alive())


def close_session(sid):
    with _lock:
        value = _sessions.pop(sid, None)
        if value:
            value.close()


async def shutdown():
    for sid in list(_sessions):
        await asyncio.to_thread(close_session, sid)
