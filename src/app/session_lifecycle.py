"""Shared guarded deletion and inert history forks."""
import json
import re
from uuid import uuid4
from fastapi import HTTPException
from app import catalog, stage5, timeline


def guard_idle(db, sid):
    from app.extensions import busy
    legacy = db.execute("SELECT 1 FROM stage5_attempts WHERE session_id=? AND status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)", (sid,)).fetchone()
    if busy(db, sid) or legacy:
        raise HTTPException(409, '会话正在运行或有待确认输入，请先停止并处理输入')


def remove(db, sid):
    guard_idle(db, sid)
    tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ('work_jobs', 'work_facts', 'media_files', 'memory7_summaries', 'memory7_jobs'):
        if name in tables and db.execute(f'SELECT 1 FROM {name} WHERE session_id=?', (sid,)).fetchone():
            raise HTTPException(409, '包含任务、媒体或记忆来源，请先归档保留关联资料')
    # Even shared companion memory can have evidence in this session. Retain
    # the evidence instead of letting removal silently make it unverifiable.
    message_ids = {r['id'] for r in db.execute('SELECT id FROM messages WHERE session_id=?', (sid,))}
    batches = {r['id'] for r in db.execute('SELECT id FROM stage6_batches WHERE session_id=?', (sid,))}
    requests = {r['request_id'] for r in db.execute('SELECT request_id FROM stage5_attempts WHERE session_id=?', (sid,))}
    sources = [json.loads(r['source']) for r in db.execute('SELECT source FROM fact_versions')]
    if 'memory7_event_versions' in tables:
        sources += [json.loads(r['document']).get('source', {}) for r in db.execute('SELECT document FROM memory7_event_versions')]
    if 'memory_v2_sources' in tables:
        for row in db.execute('SELECT document,session_id FROM memory_v2_sources'):
            if row['session_id'] is not None and str(row['session_id']) == str(sid):
                raise HTTPException(409, '此会话仍是记忆的来源，请先归档保留证据')
            sources.append(json.loads(row['document']))
    if 'memory_v2_versions' in tables:
        sources += [json.loads(row['document']).get('source', {}) for row in db.execute('SELECT document FROM memory_v2_versions')]
    if 'memory_v2_entities' in tables and db.execute(
            "SELECT 1 FROM memory_v2_entities WHERE scope='work' AND owner_id=?", (str(sid),)).fetchone():
        raise HTTPException(409, '此会话仍有工作记忆，请先归档保留关联资料')
    for source in sources:
        if not isinstance(source, dict):
            continue
        linked_messages = source.get('message_ids', [])
        linked_sessions = source.get('message_sessions', [])
        if not isinstance(linked_messages, (list, tuple)): linked_messages = []
        if not isinstance(linked_sessions, (list, tuple)): linked_sessions = []
        if (str(source.get('session_id')) == str(sid) or str(sid) in map(str, linked_sessions)
                or source.get('batch_id') in batches or source.get('request_id') in requests
                or {str(mid) for mid in message_ids}.intersection(map(str, linked_messages))):
            raise HTTPException(409, '此会话仍是记忆的来源，请先归档保留证据')
    for name in ('memory7_candidates', 'memory7_attempts', 'stage6_batch_records'):
        if name in tables:
            db.execute(f'DELETE FROM {name} WHERE batch_id IN (SELECT id FROM stage6_batches WHERE session_id=?)', (sid,))
    db.execute("DELETE FROM completed_requests WHERE json_extract(result,'$.session_id')=?", (sid,))
    for name in ('message_actions', 'timeline8_checkpoints', 'timeline8_restores', 'stage6_inputs',
                 'stage6_batches', 'stage6_input_cancellations', 'stage6_work_records', 'session_drafts', 'session_creations', 'stage5_attempts', 'messages'):
        if name in tables:
            db.execute(f'DELETE FROM {name} WHERE session_id=?', (sid,))
    # No dangling file authority, model preset binding, or dormant schedule.
    for row in db.execute('SELECT kind,id,revision,document FROM capability_documents').fetchall():
        doc = json.loads(row['document'])
        own = doc.get('session_id') == sid or row['kind'] in ('work-settings', 'agent-binding') and row['id'] == str(sid)
        if own and not doc.get('deleted'):
            catalog.put_in_transaction(db, row['kind'], row['id'], {**doc, 'deleted': True, 'enabled': False}, row['revision'])
    if 'extension_forks' in tables:
        db.execute('DELETE FROM extension_forks WHERE session_id=?', (sid,))
    db.execute('DELETE FROM sessions WHERE id=?', (sid,))
    stage5._clear_active_session(db, sid)


def clone_media(db, text, sid, target, created, mapping):
    from app import config, media
    def replace(match):
        fid, owner = match.group(1), int(match.group(2))
        if owner != sid:
            raise HTTPException(409, '消息含其他会话的附件引用，无法自动分叉')
        if fid not in mapping:
            row = db.execute('SELECT * FROM media_files WHERE id=? AND session_id=?', (fid, sid)).fetchone()
            if not row:
                raise HTTPException(409, '消息附件记录缺失，无法分叉')
            source = config.DATA_DIR / 'attachments' / (fid + media.TYPES[row['mime']])
            if not source.is_file() or source.is_symlink():
                raise HTTPException(409, '消息附件文件缺失或为链接，无法分叉')
            raw = source.read_bytes()
            import hashlib
            if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise HTTPException(409, '消息附件已变化，无法分叉')
            fresh = uuid4().hex
            destination = source.with_name(fresh + media.TYPES[row['mime']])
            with destination.open('xb') as stream:
                stream.write(raw)
            created.append(destination)
            db.execute('INSERT INTO media_files VALUES(?,?,?,?,?,?)', (fresh, target, row['mime'], row['size'], row['sha256'], row['name']))
            mapping[fid] = fresh
        return f'/v6/media/{mapping[fid]}?session_id={target}'
    return re.sub(r'/v6/media/([a-f0-9]{32})\?session_id=(\d+)', replace, text or '')


def fork(db, sid, created, through_message_id=None, copy_draft=True, copy_record=True):
    guard_idle(db, sid)
    session = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
    if not session or session['mode'] not in ('companion', 'work'):
        raise HTTPException(409, '未归类会话不能分叉')
    at = stage5.now()
    target = db.execute('INSERT INTO sessions(title,mode,created_at,updated_at) VALUES(?,?,?,?)',
                        ((session['title'] or '会话')[:72]+'（分叉）', session['mode'], at, at)).lastrowid
    rows = timeline.effective_rows(db, sid)
    valid_batches = {r['id']: r for r in db.execute("SELECT * FROM stage6_batches WHERE session_id=? AND status='complete'", (sid,))}
    if through_message_id is not None:
        cut = next((i for i,r in enumerate(rows) if r['id'] == through_message_id and r['role'] == 'assistant' and r['delivery_status'] == 'complete'), None)
        if cut is None:
            raise HTTPException(409, '只能从仍有效的已完成回答处分出侧聊')
        rows = rows[:cut+1]
    available = {r['id'] for r in rows if r['delivery_status'] == 'complete'}
    for ident, batch in list(valid_batches.items()):
        users = [r['message_id'] for r in db.execute('SELECT message_id FROM stage6_inputs WHERE batch_id=?', (ident,))]
        if batch['assistant_id'] not in available or not users or any(mid not in available for mid in users):
            valid_batches.pop(ident)
    batch_map, message_map, media_map = {}, {}, {}
    skipped = 0
    for r in rows:
        if r['delivery_status'] not in ('complete', 'retained') or (r['batch_id'] and r['batch_id'] not in valid_batches and r['delivery_status'] != 'retained'):
            skipped += 1
            continue
        old_batch = r['batch_id'] if r['delivery_status'] == 'complete' else None
        new_batch = batch_map.setdefault(old_batch, str(uuid4())) if old_batch else None
        text = clone_media(db, r['content'], sid, target, created, media_map)
        mid = db.execute('INSERT INTO messages(session_id,role,content,at,speaker_snapshot,delivery_status,batch_id) VALUES(?,?,?,?,?,?,?)',
                         (target, r['role'], text, r['at'], r['speaker_snapshot'], r['delivery_status'], new_batch)).lastrowid
        message_map[r['id']] = mid
        if new_batch and r['role'] == 'user':
            body = {'mode': session['mode'], 'content': text, 'fork_source_session_id': sid}
            db.execute('INSERT INTO stage6_inputs VALUES(?,?,?,?,?)', (str(uuid4()), target, mid, json.dumps(body), new_batch))
    for old, fresh in batch_map.items():
        original = valid_batches[old]
        answer = message_map.get(original['assistant_id'])
        if not answer:
            raise HTTPException(409, '已完成批次的回答不完整，请先处理原会话')
        # No old execution snapshot, approval, credential value, or retry token.
        snapshot = {'fork_source_session_id': sid, 'fork_source_batch_id': old}
        db.execute("INSERT INTO stage6_batches(id,session_id,mode,status,snapshot,assistant_id,error,updated_at,session_version) VALUES(?,?,?,'complete',?,?,'',?,0)",
                   (fresh, target, session['mode'], json.dumps(snapshot), answer, at))
    draft = db.execute('SELECT content FROM session_drafts WHERE session_id=?', (sid,)).fetchone()
    if draft and copy_draft:
        db.execute('INSERT INTO session_drafts VALUES(?,?,1)', (target, draft['content']))
    record = db.execute('SELECT document FROM stage6_work_records WHERE session_id=?', (sid,)).fetchone()
    if record and copy_record:
        db.execute('INSERT INTO stage6_work_records VALUES(?,?,1)', (target, record['document']))
    clone_work_configuration(db, sid, target)
    return target, skipped


def clone_work_configuration(db, sid, target):
    session = db.execute('SELECT mode FROM sessions WHERE id=?', (sid,)).fetchone()
    for kind in ('work-settings', 'agent-binding', 'session-options', 'composer-controls'):
        source_key = 'session_' + str(sid) if kind == 'session-options' else str(sid)
        target_key = 'session_' + str(target) if kind == 'session-options' else str(target)
        row = db.execute('SELECT document FROM capability_documents WHERE kind=? AND id=?', (kind, source_key)).fetchone()
        if row:
            doc = json.loads(row['document'])
            if not doc.get('deleted'):
                catalog.put_in_transaction(db, kind, target_key, doc, 0)
    if session['mode'] == 'work':
        for row in db.execute("SELECT id,document FROM capability_documents WHERE kind='workspace'").fetchall():
            doc = json.loads(row['document'])
            if doc.get('session_id') == sid and not doc.get('deleted'):
                catalog.put_in_transaction(db, 'workspace', ('session_' + str(target)) if row['id'] == 'session_' + str(sid) else 'fork_' + uuid4().hex,
                                           {**doc, 'session_id': target}, 0)
