"""User-selected workspace access and content-hash guarded edits."""
import hashlib
import os
from pathlib import Path
from uuid import uuid4
from threading import RLock

_file_locks = {}
_file_locks_guard = RLock()

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import catalog, sessions


class Workspace(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    path: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(default=0, ge=0)


@catalog.router.get('/workspaces')
def listing(session_id:int|None=None):
    return {'items':[item for item in catalog.listing('workspace') if session_id is None or item['session_id']==session_id]}


@catalog.router.put('/workspaces/{workspace_id}')
def register(workspace_id: str, body: Workspace):
    session = sessions.get_session(body.session_id)
    if not session or session['mode'] != 'work':
        raise HTTPException(409, '工作目录必须绑定工作会话')
    root = Path(body.path)
    if not root.is_absolute() or not root.is_dir():
        raise HTTPException(422, '请选择存在的绝对目录')
    return catalog.put('workspace', workspace_id,
                       {'session_id': body.session_id, 'path': str(root.resolve())}, body.expected_revision)


def resolve(workspace_id, sid, relative):
    item = catalog.get('workspace', workspace_id)
    if item.get('deleted'):
        raise ValueError('该工作目录已解除绑定')
    if item['session_id'] != sid:
        raise ValueError('工作目录不属于当前会话')
    root = Path(item['path']).resolve(strict=True)
    rel = Path(relative)
    if rel.is_absolute() or rel.drive or '..' in rel.parts or ':' in relative:
        raise ValueError('文件路径必须位于所选目录内')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError('不允许通过链接访问其他位置')
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise ValueError('文件超出所选目录')
    return root, target


def read_file(workspace_id, sid, relative):
    _, target = resolve(workspace_id, sid, relative)
    if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('文件不存在或超过 2 MiB 文本读取限制')
    data = target.read_bytes()
    return {'path': relative, 'text': data.decode('utf-8'), 'sha256': hashlib.sha256(data).hexdigest()}


def write_file(workspace_id, sid, relative, content, expected_hash):
    _, path = resolve(workspace_id, sid, relative)
    with _file_locks_guard:
        lock = _file_locks.setdefault(os.path.normcase(str(path)), RLock())
    with lock:
        return _write_file(workspace_id, sid, relative, content, expected_hash)


def _write_file(workspace_id, sid, relative, content, expected_hash):
    _, target = resolve(workspace_id, sid, relative)
    if not relative or target.is_dir() or len(content.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('目标或内容不符合文本写入限制')
    actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else 'new'
    if actual != expected_hash:
        raise ValueError('文件已改变，请重新读取后确认修改')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep the exact prior content next to the target before replacing it.
    backup = None
    if target.exists():
        backup = target.with_name(target.name + '.lingxi-backup-' + uuid4().hex)
        backup.write_bytes(target.read_bytes())
    temporary = target.with_name('.' + target.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8', newline='')
        resolve(workspace_id, sid, relative)
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else 'new'
        if actual != expected_hash:
            raise ValueError('提交前文件已改变，取消写入')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {'path': relative, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'backup': str(backup) if backup else None}


def list_files(workspace_id, sid, relative='.'):
    root, directory = resolve(workspace_id, sid, relative)
    if not directory.is_dir():
        raise ValueError('不是目录')
    items = []
    scanned = 0
    truncated = False
    from app.workspace_search import ignored
    for p in directory.iterdir():
        scanned += 1
        if scanned > 5000:
            truncated = True
            break
        if p.is_symlink() or p.is_junction() or ignored(p.relative_to(root), []):
            continue
        items.append({'path': p.relative_to(root).as_posix(), 'directory': p.is_dir()})
        if len(items) >= 500:
            truncated = True
            break
    return {'items': items, 'limit': 500, 'truncated': truncated}


def replace_file(workspace_id,sid,relative,old_text,new_text,expected_hash):
    if not old_text:raise ValueError('旧片段不能为空；请先读取文件并选择唯一片段')
    current=read_file(workspace_id,sid,relative)
    if current['sha256']!=expected_hash:raise ValueError('文件已改变，请重新读取后确认修改')
    count=current['text'].count(old_text)
    if count!=1:raise ValueError(f'旧片段匹配{count}处，必须恰好匹配1处；未修改文件')
    if chr(0) in new_text or chr(0) in current['text']:raise ValueError('二进制内容不能按文字替换')
    updated=current['text'].replace(old_text,new_text,1)
    result=write_file(workspace_id,sid,relative,updated,expected_hash)
    return {**result,'replacements':1,'previous_sha256':expected_hash}
