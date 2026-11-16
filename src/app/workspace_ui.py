"""Read-only UI access to the existing session-bound workspace file helpers."""
from fastapi import APIRouter, HTTPException, Query
from app import catalog, work_settings, workspace
from pathlib import Path
import os
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix='/v9/workspace')


def binding(sid):
    if not work_settings.get(sid)['path']:
        raise HTTPException(409, '请先为当前工作会话选择项目目录')
    key = 'session_' + str(sid)
    item = catalog.get('workspace', key)
    if item.get('deleted') or item['session_id'] != sid:
        raise HTTPException(409, '项目目录绑定已失效，请重新选择')
    return key


@router.get('/files')
def files(session_id: int = Query(gt=0), path: str = Query('.', max_length=2000)):
    try:
        return workspace.list_files(binding(session_id), session_id, path)
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


@router.get('/file')
def file(session_id: int = Query(gt=0), path: str = Query(max_length=2000), expected_project_path: str|None = Query(None,max_length=2000)):
    try:
        key = binding(session_id)
        root, target = workspace.resolve(key, session_id, path)
        if expected_project_path and os.path.normcase(str(root))!=os.path.normcase(str(Path(expected_project_path).resolve())):
            raise HTTPException(409,'项目目录已改变，未将新目录的同名文件替换到旧标签；请刷新工作记录后重新打开')
        from app.artifacts import EXTENSIONS,kind,file_mime
        import hashlib
        if not target.is_file():raise ValueError('文件不存在或不是普通文件')
        size=target.stat().st_size;mime=EXTENSIONS.get(target.suffix.lower(),'application/octet-stream')
        metadata={'path':path,'size':size,'mime':mime,'preview_kind':kind(mime)}
        if size>15*1024*1024:
            return {**metadata,'text':'','sha256':'','read_only':True,
                    'reason':'文件超过15MiB，未载入预览；可在原项目目录使用对应应用打开。'}
        with target.open('rb') as stream:raw=stream.read(15*1024*1024+1)
        if len(raw)>15*1024*1024:raise HTTPException(409,'文件在读取时增大，请重新打开')
        mime=file_mime(target,raw)
        metadata.update(size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),mime=mime,preview_kind=kind(mime))
        if len(raw)>2*1024*1024:
            return {**metadata,'text':'','read_only':True,'reason':'此文件超过2MiB编辑限制，可打开原件预览或下载。'}
        try:
            text=raw.decode('utf-8')
            if chr(0) in text:raise UnicodeError()
            # Known non-text formats retain their original viewer even when
            # their short headers happen to decode as UTF-8.
            if mime in ('application/pdf','application/zip') or mime.startswith(('image/','audio/')):raise UnicodeError()
            return {**metadata,'text':text}
        except UnicodeError:
            return {**metadata,'text':'','read_only':True,'reason':'原件按对应格式预览；二进制和非UTF-8文件不能在文本编辑器修改。'}
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


class FileSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(max_length=2 * 1024 * 1024)
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_project_path: str = Field(min_length=1, max_length=2000)


@router.put('/file')
def save_file(body: FileSave, session_id: int = Query(gt=0), path: str = Query(max_length=2000)):
    # Hold directory binding stable through the synchronous save. Filesystem
    # writes still use their own atomic replacement and content guard.
    with catalog.transaction():
        try:
            key = binding(session_id)
            root, _ = workspace.resolve(key, session_id, path)
            if os.path.normcase(str(root)) != os.path.normcase(str(Path(body.expected_project_path).resolve())):
                raise HTTPException(409, '项目目录已经变化；未保存到新目录，请重新选择文件')
            from app.git_transition import assert_unclaimed
            with catalog.read_connection() as db:assert_unclaimed(db,session_id)
            current = workspace.read_file(key, session_id, path)
            if '\0' in current['text'] or '\0' in body.content:
                raise ValueError('二进制文件不能作为文本编辑')
            return workspace.write_file(key, session_id, path, body.content, body.expected_hash)
        except (ValueError, OSError) as error:
            raise HTTPException(409 if '已改变' in str(error) else 422, str(error)) from error


class SideChatCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    parent_session_id: int = Field(gt=0)
    through_message_id: int | None = Field(default=None, gt=0)
    expected_version: int = Field(ge=0)
    inherit_record: bool = True
    kind: str = Field(default='side', pattern=r'^(side|fork)$')


@router.post('/side-chats')
def create_side_chat(body: SideChatCreate):
    import json
    from app import sessions, session_lifecycle, stage5, config
    created = []
    try:
        with catalog.transaction() as db:
            old = db.execute("SELECT document FROM capability_documents WHERE kind='side-chat' AND id=?", (body.request_id,)).fetchone()
            signature = body.model_dump(exclude={'request_id'})
            if old:
                origin = json.loads(old['document'])
                if origin['request'] != signature:
                    raise HTTPException(409, '该创建编号已用于另一组侧聊内容')
                target = origin['session_id']
                row = db.execute('SELECT * FROM sessions WHERE id=? AND archived=0', (target,)).fetchone()
                if not row:
                    raise HTTPException(409, '此前创建的侧聊已归档或删除，请重新创建')
                return {'session': dict(row), 'origin': origin}
            parent = db.execute("SELECT * FROM sessions WHERE id=? AND mode='work' AND archived=0", (body.parent_session_id,)).fetchone()
            if not parent:
                raise HTTPException(404, '来源工作会话不可用')
            if parent['timeline_version'] != body.expected_version:
                raise HTTPException(409, '来源会话已修改，请重新选择上下文切点')
            target, skipped = session_lifecycle.fork(db, body.parent_session_id, created,
                through_message_id=body.through_message_id, copy_draft=False, copy_record=body.inherit_record)
            title = ('侧聊 · ' + (parent['title'] or '工作会话')[:60]) if body.kind == 'side' else (parent['title'] or '工作会话')[:65] + '（分叉）'
            db.execute('UPDATE sessions SET title=? WHERE id=?', (title, target))
            origin = {'session_id': target, 'parent_session_id': body.parent_session_id,
                      'through_message_id': body.through_message_id, 'regular': body.kind == 'fork', 'request': signature, 'kind': body.kind}
            catalog.put_in_transaction(db, 'side-chat', body.request_id, origin, 0)
        return {'session': sessions.get_session(target), 'origin': origin}
    except BaseException:
        for path in created:
            if path.resolve().parent == (config.DATA_DIR / 'attachments').resolve():
                path.unlink(missing_ok=True)
        raise


@router.get('/side-chats/{sid}')
def side_chat_origin(sid: int):
    from app import sessions
    row = sessions.get_session(sid)
    if not row or row['mode'] != 'work' or row['archived']:
        raise HTTPException(404, '侧聊已归档或删除；可在会话管理中查看')
    origin = next((d for d in catalog.listing('side-chat') if d['session_id'] == sid), None)
    if not origin:
        raise HTTPException(404, '独立侧聊来源记录不存在')
    return {'session': row, 'origin': origin}


class SideChatSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(gt=0)


@router.post('/side-chats/{sid}/save')
def save_side_chat(sid: int, body: SideChatSave):
    with catalog.transaction():
        origin = side_chat_origin(sid)['origin']
        result = catalog.put('side-chat', origin['id'], {k:v for k,v in {**origin,'regular':True}.items() if k not in ('id','revision')}, body.expected_revision)
    return result
