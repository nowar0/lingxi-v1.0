"""Opt-in app controls, bounded diagnostics and reasoning-free conversation export."""
import json
import time
from uuid import uuid4
from fastapi import HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, sessions
from app.db import connect


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    domain_tools: bool = False
    expected_revision: int = Field(default=0, ge=0)


def settings():
    try:
        return {**Settings().model_dump(exclude={'expected_revision'}), **catalog.get('evolution-settings', 'settings')}
    except HTTPException as error:
        if error.status_code != 404:
            raise
    return {'domain_tools': False, 'revision': 0}


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS model_requests(
            id TEXT PRIMARY KEY,role TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,reason TEXT,usage TEXT,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        db.execute('CREATE INDEX IF NOT EXISTS model_requests_at ON model_requests(at)')
        columns={r['name'] for r in db.execute('PRAGMA table_info(model_requests)')}
        for name,kind in [('session_id','INTEGER'),('first_token_ms','REAL'),('generation_ms','REAL')]:
            if name not in columns:db.execute(f'ALTER TABLE model_requests ADD COLUMN {name} {kind}')


@catalog.router.get('/evolution/settings')
def get_settings():
    return settings()


@catalog.router.put('/evolution/settings')
def save_settings(body: Settings):
    return catalog.put('evolution-settings', 'settings', body.model_dump(exclude={'expected_revision'}), body.expected_revision)


def record_model(role, model, status, started, usage, reason=None, first_token_ms=None, generation_ms=None):
    from app.usage import extract,_owner
    with connect() as db:
        db.execute('INSERT INTO model_requests(id,role,model,status,duration_ms,reason,usage) VALUES(?,?,?,?,?,?,?)',
            (identifier := uuid4().hex, str(role)[:500], str(model)[:500], status, round((time.monotonic()-started)*1000),
             reason, json.dumps(extract(usage), ensure_ascii=False)))
        db.execute('UPDATE model_requests SET session_id=?,first_token_ms=?,generation_ms=? WHERE id=?',(_owner.get().get('session_id'),first_token_ms,generation_ms,identifier))


@catalog.router.get('/diagnostics')
def diagnostics(limit: int = Query(default=30, ge=1, le=100)):
    from app import provider_failover, extension_jobs
    from app.speech.registry import engines
    with connect() as db:
        requests = [dict(row) for row in db.execute('SELECT * FROM model_requests ORDER BY rowid DESC LIMIT ?', (limit,))]
        errors = [dict(row) for row in db.execute("SELECT reason,COUNT(*) AS count FROM model_requests WHERE status!='complete' GROUP BY reason ORDER BY count DESC LIMIT 12")]
        tool_events = [row['event'] for row in db.execute("SELECT event FROM work_events WHERE event IN ('tool_started','tool_result','tool_interrupted','tool_rejected') ORDER BY seq DESC LIMIT 500")]
        batch_tools = []
        for row in db.execute('SELECT document FROM stage6_batch_records ORDER BY rowid DESC LIMIT 100'):
            batch_tools.extend(json.loads(row['document']).get('tools', []))
    finished = sum(value == 'tool_result' for value in tool_events)
    failed = sum(value == 'tool_interrupted' for value in tool_events)
    chat_finished = sum(value.get('ok') is True for value in batch_tools)
    chat_failed = sum(value.get('ok') is False for value in batch_tools)
    for row in requests:
        row['usage'] = json.loads(row['usage']) if row['usage'] else None
    return {'requests': requests, 'failures': errors,
        'tools': {'completed': finished + chat_finished, 'failed': failed + chat_failed,
                  'scope': '最近500条任务工具事件与100条聊天批次；拒绝不计作已执行'},
        'speech': engines()['items'], 'budget': extension_jobs.budget(),
        'switches': provider_failover.events(min(limit, 30))['items']}


@catalog.router.get('/sessions/{sid}/export')
def export_conversation(sid: int, start_id: int = 0, end_id: int | None = None, limit: int = 10000):
    session = sessions.get_session(sid)
    if not session:raise HTTPException(404, '会话不存在')
    if start_id<0 or end_id is not None and end_id<start_id or not 1<=limit<=10000:raise HTTPException(422,'导出范围无效')
    with connect() as db:
        rows = db.execute('SELECT id,role,content,at,batch_id,delivery_status FROM messages WHERE session_id=? AND archived=0 AND id>=? AND (? IS NULL OR id<=?) ORDER BY id LIMIT ?', (sid,start_id,end_id,end_id,limit)).fetchall()
        if len(rows) > 10000:
            raise HTTPException(422, '会话超过一万条消息，请先按范围整理后导出')
        lines = ['# ' + str(session['title']).replace('\n', ' '), '', f"模式：{session['mode']} · 会话：{sid}", '']
        for row in rows:
            lines.extend([f"## {'你' if row['role']=='user' else '助手'} · {row['at']}", '', row['content'] or '', ''])
            if row['batch_id'] and row['role'] == 'assistant':
                record = db.execute('SELECT document FROM stage6_batch_records WHERE batch_id=?', (row['batch_id'],)).fetchone()
                for tool in (json.loads(record['document']).get('tools', []) if record else []):
                    # Do not export hidden reasoning, credentials, raw tool arguments
                    # or tool outputs that may contain third-party secrets.
                    lines.append(f"- 工具：{tool.get('tool','未知')} · {tool.get('status','未知')}")
                lines.append('')
    body = '\n'.join(lines).encode('utf-8')
    if len(body) > 10 * 1024 * 1024:
        raise HTTPException(422, '导出超过10 MiB，请缩小会话范围')
    return Response(body, media_type='text/markdown; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="lingxi-session-{sid}.md"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
