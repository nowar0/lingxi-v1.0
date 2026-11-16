"""阶段8：消息只读归档、会话版本栅栏和同事务的记忆回退。"""
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from app import chat, migrations, stage5
from app.db import connect

router = APIRouter(prefix='/v5')
LIMIT = 10


def ensure_backup():
    marker = stage5.DATA_DIR / 'backups/stage8-schema-backup.json'
    if not marker.exists():
        backup = migrations.backup(stage5.DATA_DIR)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(stage5.dump({'backup': backup}), encoding='utf-8')


def init_table():
    ensure_backup()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS timeline8_checkpoints(message_id INTEGER PRIMARY KEY REFERENCES messages(id), session_id INTEGER NOT NULL, document TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS timeline8_restores(id TEXT PRIMARY KEY, session_id INTEGER NOT NULL, target_id INTEGER NOT NULL, version INTEGER NOT NULL, document TEXT NOT NULL, at TEXT NOT NULL)')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(stage6_batches)')}
        if 'session_version' not in columns:
            db.execute('ALTER TABLE stage6_batches ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')


def version(db, sid):
    row = db.execute('SELECT timeline_version FROM sessions WHERE id=?', (sid,)).fetchone()
    return row[0] if row else None


def effective_rows(db, sid):
    # 与队列聊天的显示顺序相同；先排队的下一批输入ID可能小于上一批回答ID。
    return db.execute('''SELECT m.* FROM messages m WHERE session_id=? AND archived=0
        ORDER BY COALESCE((SELECT MIN(message_id) FROM stage6_inputs WHERE batch_id=m.batch_id),m.id),m.id''', (sid,)).fetchall()


def checkpoint(db, sid, mid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='timeline8_checkpoints'").fetchone():
        return
    summary = db.execute("SELECT id FROM memory7_summaries WHERE session_id=? AND status='active' ORDER BY boundary DESC,at DESC LIMIT 1", (sid,)).fetchone()
    work = db.execute('SELECT document,revision FROM stage6_work_records WHERE session_id=?', (sid,)).fetchone()
    doc = {'session_version': version(db, sid), 'message_id': mid,
           'summary_id': summary[0] if summary else None,
           # 审计只保存版本边界；实际恢复按来源链重算，不按编号覆盖全库。
           'memory_version_boundary': db.execute('SELECT COALESCE(MAX(id),0) FROM fact_versions').fetchone()[0],
           'visible_count': len(effective_rows(db, sid)),
           'tasks': [dict(r) for r in db.execute('SELECT id,status,assistant_id FROM stage6_batches WHERE session_id=?', (sid,))],
           'work_record': dict(work) if work else None}
    db.execute('INSERT OR IGNORE INTO timeline8_checkpoints VALUES(?,?,?)', (mid, sid, stage5.dump(doc)))


def check_session(db, sid, mode):
    row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
    if not row or mode not in ('companion', 'work') or row['mode'] != mode:
        raise HTTPException(409, '会话不存在或模式不匹配')
    return row


def info(db, sid, target, mode):
    session = check_session(db, sid, mode)
    rows = effective_rows(db, sid)
    index = next((i for i, r in enumerate(rows) if r['id'] == target), -1)
    if index < 0:
        raise HTTPException(404, '目标不在当前有效对话中')
    later = rows[index + 1:]
    generating = db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'", (sid,)).fetchone()
    legacy = db.execute("SELECT 1 FROM stage5_attempts WHERE session_id=? AND status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)", (sid,)).fetchone()
    reason = ('先结束或停止当前回复，并处理旧的未确认请求' if generating or legacy or ('session', sid) in chat.active else
              '当前进度' if not later else '超出最近 10 条消息，只能查看和搜索' if len(later) > LIMIT else '')
    return {'session_id': sid, 'mode': mode, 'target_id': target, 'distance': len(later),
            'session_version': session['timeline_version'], 'latest_message_id': rows[-1]['id'],
            'allowed': not reason, 'reason': reason, 'archived_ids': [r['id'] for r in later],
            'external_effects_note': '回退只改变对话、摘要和记忆；已执行的文件、浏览器、发送及付费行为不会撤销，也不会自动重做。'}


@router.get('/sessions/{sid}/restore-preview')
def preview(sid: int, target_id: int, mode: str):
    with connect() as db:
        db.execute('BEGIN')
        return info(db, sid, target_id, mode)


def source_archived(db, source, seen=None):
    """只追踪已撤回来源，其他会话的新版本不因时间晚于目标而失效。"""
    if source.get('batch_id'):
        r = db.execute('SELECT status FROM stage6_batches WHERE id=?', (source['batch_id'],)).fetchone()
        if r and r[0] == 'withdrawn':
            return True
    for mid in source.get('message_ids', []):
        r = db.execute('SELECT archived FROM messages WHERE id=?', (mid,)).fetchone()
        if r and r[0]:
            return True
    if source.get('request_id'):
        r = db.execute('SELECT result FROM completed_requests WHERE request_id=?', (source['request_id'],)).fetchone()
        if r:
            mid = json.loads(r[0]).get('last_message_id')
            answer = db.execute('SELECT archived,session_id,batch_id FROM messages WHERE id=?', (mid,)).fetchone()
            if answer and answer['archived']:
                return True
            if answer and not answer['batch_id'] and not source.get('batch_id'):
                user = db.execute("SELECT archived FROM messages WHERE session_id=? AND role='user' AND batch_id IS NULL AND id<? ORDER BY id DESC LIMIT 1",
                                  (answer['session_id'], mid)).fetchone()
                if user and user['archived']:
                    return True
    linked = source.get('source_memory_version_id')
    if linked:
        seen = set(seen or ())
        if linked in seen:
            return True
        seen.add(linked)
        r = db.execute('SELECT status,source FROM fact_versions WHERE id=?', (linked,)).fetchone()
        return bool(r and (r['status'] == 'withdrawn' or source_archived(db, json.loads(r['source']), seen)))
    return False


def recompute_memories(db):
    changed = set()
    for r in db.execute("SELECT * FROM fact_versions WHERE status!='withdrawn'").fetchall():
        if source_archived(db, json.loads(r['source'])):
            db.execute("UPDATE fact_versions SET status='withdrawn' WHERE id=?", (r['id'],))
            changed.add((r['scope'], r['owner_id'], r['key']))
    for scope, owner, key in changed:
        r = db.execute("SELECT * FROM fact_versions WHERE scope=? AND owner_id=? AND key=? AND status!='withdrawn' ORDER BY id DESC LIMIT 1", (scope, owner, key)).fetchone()
        latest = db.execute('SELECT status FROM fact_versions WHERE scope=? AND owner_id=? AND key=? ORDER BY id DESC LIMIT 1', (scope, owner, key)).fetchone()
        if scope == 'work':
            db.execute('DELETE FROM work_facts WHERE session_id=? AND key=?', (owner, key))
            if r and r['status'] == 'active':
                db.execute('INSERT INTO work_facts VALUES(?,?,?,?)', (owner, key, r['value'], r['at']))
        else:
            db.execute('DELETE FROM facts WHERE mode=? AND key=?', (scope, key))
            if r and r['status'] == 'active':
                db.execute('INSERT INTO facts VALUES(?,?,?,?)', (key, r['value'], scope, r['at']))
        if latest and latest['status'] == 'withdrawn':
            # 回退也是当前值的变更：新增版本保持CAS单调递增，旧编辑窗口不能覆盖恢复后的值。
            source = {**json.loads(r['source']), 'restored_from_version_id': r['id']} if r else {'origin':'阶段8撤回来源；没有更早有效值'}
            db.execute('INSERT INTO fact_versions(scope,owner_id,key,value,source,at,status,category,occurred_at,confirmed_at,pinned) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (scope, owner, key, r['value'] if r else '', stage5.dump(source), stage5.now(),
                        r['status'] if r else 'deleted', r['category'] if r else 'legacy',
                        r['occurred_at'] if r else None, r['confirmed_at'] if r else None, r['pinned'] if r else 0))
    # 旧事件也保留版本，撤回来源不能继续通过检索读到。
    for r in db.execute('SELECT * FROM memory7_event_versions ORDER BY event_id,revision').fetchall():
        doc = json.loads(r['document'])
        if doc.get('status') != 'withdrawn' and source_archived(db, doc.get('source', {})):
            doc['status'] = 'withdrawn'
            db.execute('UPDATE memory7_event_versions SET document=? WHERE event_id=? AND revision=?', (stage5.dump(doc), r['event_id'], r['revision']))
    for eid in [r[0] for r in db.execute('SELECT DISTINCT event_id FROM memory7_event_versions')]:
        r = db.execute("SELECT document FROM memory7_event_versions WHERE event_id=? AND json_extract(document,'$.status')!='withdrawn' ORDER BY revision DESC LIMIT 1", (eid,)).fetchone()
        doc = json.loads(r[0]) if r else None
        if doc and doc.get('status') == 'active':
            db.execute('UPDATE events SET content=? WHERE id=?', (doc.get('value', ''), eid))
        else:
            db.execute('DELETE FROM events WHERE id=?', (eid,))
    return [{'scope': s, 'owner_id': o, 'key': k} for s, o, k in sorted(changed)]


@router.post('/sessions/{sid}/restore')
def restore(sid: int, body: dict):
    if type(body.get('target_id')) is not int:
        raise HTTPException(422, 'target_id必须是整数')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        p = info(db, sid, body['target_id'], body.get('mode'))
        if type(body.get('expected_version')) is not int or body['expected_version'] != p['session_version'] or body.get('expected_latest_message_id') != p['latest_message_id']:
            raise HTTPException(409, '会话已变化，请刷新回退预览')
        if not p['allowed']:
            raise HTTPException(409, p['reason'])
        has_checkpoint = db.execute('SELECT 1 FROM timeline8_checkpoints WHERE message_id=?', (body['target_id'],)).fetchone()
        archived = p['archived_ids']
        for mid in archived:
            db.execute('UPDATE messages SET archived=1 WHERE id=?', (mid,))
        bids = [r[0] for r in db.execute("SELECT id FROM stage6_batches WHERE session_id=? AND assistant_id IN (SELECT id FROM messages WHERE archived=1)", (sid,))]
        for bid in bids:
            db.execute("UPDATE stage6_batches SET status='withdrawn',error='阶段8回退；不会自动重新执行' WHERE id=?", (bid,))
            db.execute("UPDATE memory7_candidates SET status='discarded',reason='来源已撤回' WHERE batch_id=?", (bid,))
            db.execute("UPDATE messages SET delivery_status='retained' WHERE batch_id=? AND role='user' AND archived=0", (bid,))
        # 停止尚未启动的队列输入；页面原来的自动启动计时器不能在回退后付费执行。
        db.execute("UPDATE messages SET delivery_status='retained' WHERE session_id=? AND role='user' AND archived=0 AND delivery_status='queued' AND id IN (SELECT message_id FROM stage6_inputs WHERE batch_id IS NULL)", (sid,))
        for r in db.execute("SELECT id FROM messages WHERE session_id=? AND archived=1 AND role='assistant' AND batch_id IS NULL", (sid,)).fetchall():
            mid = db.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND role='user' AND id<? AND batch_id IS NULL", (sid,r['id'])).fetchone()[0]
            db.execute("UPDATE messages SET delivery_status='retained' WHERE id=? AND archived=0", (mid,))
        memories = recompute_memories(db)
        from app.memory import summarizer
        for r in db.execute("SELECT id,source FROM memory7_summaries WHERE session_id=? AND status='active'", (sid,)).fetchall():
            if not summarizer.source_valid(db, sid, json.loads(r['source'])):
                db.execute("UPDATE memory7_summaries SET status='withdrawn' WHERE id=?", (r['id'],))
        db.execute("UPDATE memory7_jobs SET status='stale',error='会话已回退；旧结果不得提交' WHERE session_id=? AND status IN ('pending','running')", (sid,))
        db.execute('UPDATE sessions SET timeline_version=timeline_version+1,updated_at=? WHERE id=?', (stage5.now(), sid))
        if not has_checkpoint:
            # 升级前的消息没有工作记录快照：按来源重建记忆，保留独立手工工作记录。
            checkpoint(db, sid, body['target_id'])
        point = json.loads(db.execute('SELECT document FROM timeline8_checkpoints WHERE message_id=?', (body['target_id'],)).fetchone()[0])
        if not has_checkpoint:
            point['legacy_reconstructed'] = True
        elif p['mode'] == 'work':
            record = point.get('work_record')
            document = record['document'] if record else stage5.dump({'goal':'','materials':'','constraints':'','tasks':[],'status':'待开始'})
            db.execute('INSERT INTO stage6_work_records VALUES(?,?,1) ON CONFLICT(session_id) DO UPDATE SET document=excluded.document,revision=stage6_work_records.revision+1', (sid, document))
        event = {**p, 'changed_memories': memories, 'checkpoint': point, 'batch_ids': bids, 'new_version': p['session_version'] + 1}
        db.execute('INSERT INTO timeline8_restores VALUES(?,?,?,?,?,?)', (str(uuid4()), sid, body['target_id'], event['new_version'], stage5.dump(event), stage5.now()))
    from app import queue6
    return {**event, 'view': queue6.view(sid, p['mode'])}


@router.get('/sessions/{sid}/history')
def history(sid: int, mode: str, q: str = '', offset: int = 0, limit: int = 100, kind: str = 'all'):
    if len(q) > 500 or offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, '历史查询或分页范围错误')
    with connect() as db:
        db.execute('BEGIN')
        check_session(db, sid, mode)
        rows = effective_rows(db, sid)
        pending = bool(db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'", (sid,)).fetchone()) or ('session', sid) in chat.active or bool(db.execute("SELECT 1 FROM stage5_attempts WHERE session_id=? AND status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)", (sid,)).fetchone())
        items = []
        if kind not in ('all','images','audio','files','links'):raise HTTPException(422,'历史类型无效')
        import re
        for i, r in enumerate(rows):
            text=r['content'] or ''
            links=re.findall(r'\[([^\]]+)\]\(([^)]+)\)',text)
            matches={'all':True,'images':any(label=='生成图片' or re.search(r'\.(png|jpe?g|webp|gif)$',label,re.I) for label,url in links),'audio':any(re.search(r'\.(wav|mp3|ogg|m4a|webm|aac)$',label,re.I) for label,url in links),'files':any(url.startswith('/v6/media/') for label,url in links),'links':bool(links) or bool(re.search(r'https?://\S+',text))}
            if not matches[kind]:continue
            data = dict(r)
            data['speaker'] = json.loads(data.pop('speaker_snapshot')) if r['speaker_snapshot'] else None
            role_label = '你 用户 user' if data['role']=='user' else '助手 工作 Agent assistant'
            if q.casefold() not in ((data['content'] or '') + ' ' + (data['at'] or '') + ' ' + stage5.dump(data['speaker']) + ' ' + role_label).casefold():
                continue
            distance = len(rows) - i - 1
            data.update(distance=distance, restore_allowed=0 < distance <= LIMIT and not pending,
                        restore_reason='先停止当前回复' if pending else '当前进度' if distance == 0 else '超出最近 10 条消息' if distance > LIMIT else '')
            items.append(data)
        return {'items': items[offset:offset+limit], 'total': len(items), 'visible_count': len(rows), 'session_version': version(db, sid), 'offset': offset, 'limit': limit}
