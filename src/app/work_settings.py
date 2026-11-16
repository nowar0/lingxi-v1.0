"""Workspace and acceptance metadata added to the existing work-record dialog."""
from pydantic import BaseModel, ConfigDict, Field
from fastapi import HTTPException, Request
from app import catalog, sessions, workspace
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(default='', max_length=2000)
    acceptance: str = Field(default='', max_length=8000)
    expected_revision: int = Field(default=0, ge=0)


class FolderPath(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(default='', max_length=2000)


def _local_picker_request(request: Request):
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc.lower() != request.headers.get('host', '').lower():
        raise HTTPException(403, '请从零汐本地页面选择工作目录')


def _validated_folder(path: str):
    root = Path(path.strip())
    if not path.strip() or not root.is_absolute() or not root.is_dir():
        raise HTTPException(422, '请选择存在的本地绝对目录')
    root = root.resolve()
    return {'path': str(root), 'name': root.name or str(root)}


_picker_lock = threading.Lock()
_PICKER_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择要添加到零汐的工作区文件夹'
$dialog.ShowNewFolderButton = $false
if ($env:LINGXI_PICKER_INITIAL_PATH) { $dialog.SelectedPath = $env:LINGXI_PICKER_INITIAL_PATH }
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        @{cancelled=$false;path=$dialog.SelectedPath} | ConvertTo-Json -Compress
    } else { @{cancelled=$true} | ConvertTo-Json -Compress }
} finally { $dialog.Dispose() }
'''


def _choose_folder(initial: str):
    if sys.platform != 'win32':
        raise HTTPException(409, '当前系统无法打开文件夹窗口，请直接填写完整目录')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = os.environ.copy()
    environment['LINGXI_PICKER_INITIAL_PATH'] = initial
    try:
        result = subprocess.run(
            [str(powershell), '-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand',
             base64.b64encode(_PICKER_SCRIPT.encode('utf-16-le')).decode('ascii')],
            capture_output=True, timeout=180, check=True, env=environment,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return json.loads(result.stdout.decode('utf-8-sig'))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(503, '文件夹窗口未能完成，请重试或填写完整目录') from exc


@catalog.router.post('/workspace-folders/validate')
def validate_folder(body: FolderPath, request: Request):
    _local_picker_request(request)
    return _validated_folder(body.path)


@catalog.router.post('/workspace-folders/select')
def select_folder(body: FolderPath, request: Request):
    _local_picker_request(request)
    if not _picker_lock.acquire(blocking=False):
        raise HTTPException(409, '已有文件夹选择窗口，请先完成或取消')
    try:
        try:
            initial = _validated_folder(body.path)['path'] if body.path.strip() else ''
        except HTTPException:
            initial = ''  # A stale hint must not prevent choosing a different folder.
        chosen = _choose_folder(initial)
        if chosen.get('cancelled'):
            return {'cancelled': True}
        return {'cancelled': False, **_validated_folder(chosen.get('path', ''))}
    finally:
        _picker_lock.release()


@catalog.router.get('/work-settings/{sid}')
def get(sid: int):
    row = sessions.get_session(sid)
    if not row or row['mode'] != 'work':
        raise HTTPException(404, '工作会话不存在')
    try:
        return catalog.get('work-settings', str(sid))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return {'path': '', 'acceptance': '', 'revision': 0}


@catalog.router.put('/work-settings/{sid}')
def put(sid: int, body: Settings):
    from app.db import connect
    normalized = _validated_folder(body.path)['path'] if body.path else ''
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        session = db.execute('SELECT mode FROM sessions WHERE id=?', (sid,)).fetchone()
        if not session or session['mode'] != 'work':
            raise HTTPException(404, '工作会话不存在')
        from app import persistent_terminal, enhanced_terminal, git_commits
        from app.git_transition import assert_unclaimed
        assert_unclaimed(db,sid,normalized or None)
        previous = db.execute("SELECT document FROM capability_documents WHERE kind='work-settings' AND id=?", (str(sid),)).fetchone()
        if previous and json.loads(previous['document']).get('path') != normalized and (
                persistent_terminal.is_running(sid) or enhanced_terminal.is_running(sid) or git_commits.is_running(sid)):
            raise HTTPException(409, '终端或Git操作仍在运行，请先停止再切换目录')
        row = db.execute("SELECT revision FROM capability_documents WHERE kind='work-settings' AND id=?", (str(sid),)).fetchone()
        if (row['revision'] if row else 0) != body.expected_revision:
            raise HTTPException(409, '工作配置已改变，请刷新')
        workspace_id = 'session_' + str(sid)
        current = db.execute("SELECT revision,document FROM capability_documents WHERE kind='workspace' AND id=?", (workspace_id,)).fetchone()
        if normalized:
            catalog.put_in_transaction(db, 'workspace', workspace_id,
                {'session_id': sid, 'path': normalized}, current['revision'] if current else 0)
        elif current:
            old = json.loads(current['document'])
            if not old.get('deleted'):
                catalog.put_in_transaction(db, 'workspace', workspace_id,
                    {'session_id': sid, 'path': old['path'], 'deleted': True}, current['revision'])
        return catalog.put_in_transaction(db, 'work-settings', str(sid),
            {'path': normalized, 'acceptance': body.acceptance}, body.expected_revision)
