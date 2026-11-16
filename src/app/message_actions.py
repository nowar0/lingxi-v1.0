"""Message edits and reversible removal, fenced by the timeline version."""
import json
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import chat, queue6, stage5, timeline
from app.db import connect
from app.memory import summarizer

router = APIRouter(prefix='/v8')


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS message_actions(
            id TEXT PRIMARY KEY, session_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            action TEXT NOT NULL, before_content TEXT NOT NULL, new_version INTEGER NOT NULL,
            undone INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS message_action_memory_undo(
            action_id TEXT PRIMARY KEY REFERENCES message_actions(id) ON DELETE CASCADE, document TEXT NOT NULL)''')


class Version(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)


class Edit(Version):
    content: str = Field(min_length=1, max_length=8000)


def check(db, sid, expected):
    row = db.execute('SELECT mode FROM sessions WHERE id=?', (sid,)).fetchone()
    if not row or row['mode'] not in ('companion', 'work'):
        raise HTTPException(404, '聊天不存在')
    session = timeline.check_session(db, sid, row['mode'])
    if session['archived'] or session['timeline_version'] != expected:
        raise HTTPException(409, '聊天已变化，请刷新后再操作')
    legacy = db.execute("SELECT 1 FROM stage5_attempts WHERE session_id=? AND status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)", (sid,)).fetchone()
    generating = db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'", (sid,)).fetchone()
    working = db.execute("SELECT 1 FROM work_jobs WHERE session_id=? AND status IN ('running','pausing')", (sid,)).fetchone()
    if generating or working or legacy or ('session', sid) in chat.active:
        raise HTTPException(409, '请等当前回复完成，或先停止回复')
    return session['mode']


def target(db, sid, mid):
    message = db.execute('SELECT * FROM messages WHERE session_id=? AND id=? AND archived=0', (sid, mid)).fetchone()
    if not message:
        raise HTTPException(404, '消息不存在或已删除')
    return message


def contains_image(db, text):
    for label, fid in re.findall(r'\[([^\]\n]+)\]\(/v6/media/([a-f0-9]{32})\?session_id=\d+\)', text):
        row = db.execute('SELECT mime FROM media_files WHERE id=?', (fid,)).fetchone()
        if (row and row['mime'].startswith('image/')) or label == '生成图片' or re.search(r'\.(png|jpe?g|webp|gif)$', label, re.I):
            return True
    return False


def invalidate(db, sid, mid, reversible=False):
    # Temporarily exclude an edited source while withdrawing its derived memory.
    # The message itself is retained; no later replies are removed or re-executed.
    from app.memory import undo
    before = undo.capture(db, sid) if reversible else None
    db.execute('UPDATE messages SET archived=1 WHERE id=?', (mid,))
    timeline.recompute_memories(db)
    for row in db.execute("SELECT id,source FROM memory7_summaries WHERE session_id=? AND status='active'", (sid,)).fetchall():
        if not summarizer.source_valid(db, sid, json.loads(row['source'])):
            db.execute("UPDATE memory7_summaries SET status='withdrawn' WHERE id=?", (row['id'],))
    db.execute("UPDATE memory7_jobs SET status='stale',error='消息已修改；旧结果不再提交' WHERE session_id=? AND status IN ('pending','running')", (sid,))
    return undo.changed(db, before) if before is not None else None


def record(db, sid, message, action):
    key = str(uuid4())
    db.execute('UPDATE sessions SET timeline_version=timeline_version+1,updated_at=? WHERE id=?', (stage5.now(), sid))
    version = timeline.version(db, sid)
    db.execute('INSERT INTO message_actions VALUES(?,?,?,?,?,?,0,?)', (key, sid, message['id'], action, message['content'], version, stage5.now()))
    return key


@router.put('/sessions/{sid}/messages/{mid}')
def edit_message(sid: int, mid: int, body: Edit):
    if not body.content.strip():
        raise HTTPException(422, '消息不能为空')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        mode = check(db, sid, body.expected_version)
        message = target(db, sid, mid)
        if message['role'] != 'user' or contains_image(db, message['content']):
            raise HTTPException(409, '只能编辑自己发出的文字消息，图片不支持编辑')
        if body.content != message['content']:
            invalidate(db, sid, mid)
            db.execute('UPDATE messages SET content=?,archived=0 WHERE id=?', (body.content, mid))
            record(db, sid, message, 'edit')
    return {'view': queue6.view(sid, mode)}


@router.delete('/sessions/{sid}/messages/{mid}')
def remove_message(sid: int, mid: int, body: Version):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        mode = check(db, sid, body.expected_version)
        message = target(db, sid, mid)
        memory_changes = invalidate(db, sid, mid, reversible=True)
        undo_id = record(db, sid, message, 'delete')
        db.execute('INSERT INTO message_action_memory_undo VALUES(?,?)', (undo_id, stage5.dump(memory_changes)))
    return {'view': queue6.view(sid, mode), 'undo_id': undo_id}


@router.post('/sessions/{sid}/message-actions/{key}/undo')
def undo_remove(sid: int, key: str, body: Version):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        mode = check(db, sid, body.expected_version)
        action = db.execute("SELECT * FROM message_actions WHERE id=? AND session_id=? AND action='delete' AND undone=0", (key, sid)).fetchone()
        if not action or action['new_version'] != body.expected_version:
            raise HTTPException(409, '删除后聊天已变化，无法撤销这次操作')
        message = db.execute('SELECT * FROM messages WHERE id=? AND session_id=? AND archived=1', (action['message_id'], sid)).fetchone()
        if not message:
            raise HTTPException(409, '这条消息当前无法恢复')
        db.execute('UPDATE messages SET archived=0 WHERE id=?', (message['id'],))
        journal = db.execute('SELECT document FROM message_action_memory_undo WHERE action_id=?', (key,)).fetchone()
        from app.memory import undo
        recovery = undo.restore(db, sid, json.loads(journal['document']), key) if journal else {'legacy_without_journal': True}
        db.execute('UPDATE message_actions SET undone=1 WHERE id=?', (key,))
        db.execute('UPDATE sessions SET timeline_version=timeline_version+1,updated_at=? WHERE id=?', (stage5.now(), sid))
    return {'view': queue6.view(sid, mode), 'memory_recovery': recovery}
