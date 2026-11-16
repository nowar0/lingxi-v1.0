import time
"""阶段6：已发送消息、批次队列、停止/失败、服务端恢复与简单日程。

仅支持一个uvicorn进程；验收结果见独立测试报告。
"""
import asyncio
import hashlib
import json
import re
from contextlib import aclosing
from datetime import date
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import timeline, chat, migrations, persona, sessions, stage5, snapshot_revision
from app.db import connect
from app.memory import stage7, writer, recent_rag
from app.memory.search import autoload

router = APIRouter(prefix='/v3')
tasks = {}
shutting_down = False


def init_table():
    global shutting_down
    shutting_down=False
    marker = stage5.DATA_DIR / 'backups/stage6-schema-backup.json'
    if not marker.exists():
        backup = migrations.backup(stage5.DATA_DIR)
        marker.write_text(stage5.dump({'backup': backup}), encoding='utf-8')
    with connect() as db:
        columns = {r['name'] for r in db.execute('PRAGMA table_info(messages)')}
        for name, declaration in [('delivery_status', "TEXT NOT NULL DEFAULT 'complete'"), ('batch_id', 'TEXT')]:
            if name not in columns:
                db.execute(f'ALTER TABLE messages ADD COLUMN {name} {declaration}')
        db.execute('CREATE TABLE IF NOT EXISTS stage6_input_cancellations(client_id TEXT PRIMARY KEY,session_id INTEGER NOT NULL REFERENCES sessions(id),at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS stage6_inputs(client_id TEXT PRIMARY KEY,session_id INTEGER NOT NULL REFERENCES sessions(id),message_id INTEGER UNIQUE NOT NULL REFERENCES messages(id),body TEXT NOT NULL,batch_id TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS stage6_batches(id TEXT PRIMARY KEY,session_id INTEGER NOT NULL REFERENCES sessions(id),mode TEXT NOT NULL,status TEXT NOT NULL,snapshot TEXT NOT NULL,assistant_id INTEGER NOT NULL REFERENCES messages(id),error TEXT NOT NULL,updated_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS stage6_schedule(id TEXT PRIMARY KEY,title TEXT NOT NULL,date TEXT NOT NULL,done INTEGER NOT NULL,revision INTEGER NOT NULL,archived INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS stage6_work_records(session_id INTEGER PRIMARY KEY REFERENCES sessions(id),document TEXT NOT NULL,revision INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS stage6_batch_records(batch_id TEXT PRIMARY KEY REFERENCES stage6_batches(id),document TEXT NOT NULL)')
        snapshot_revision.init(db)
        db.execute('CREATE INDEX IF NOT EXISTS stage6_inputs_batch ON stage6_inputs(batch_id,message_id)')
        db.execute('CREATE INDEX IF NOT EXISTS stage6_inputs_queue ON stage6_inputs(session_id,batch_id,message_id)')
        db.execute('CREATE INDEX IF NOT EXISTS stage6_batches_session ON stage6_batches(session_id,status,assistant_id)')
        # 重启不自动付费重跑。持久化partial已经在messages；生成中批次标失败。
        db.execute("UPDATE messages SET delivery_status='failed' WHERE batch_id IN (SELECT id FROM stage6_batches WHERE status='generating')")
        db.execute("UPDATE stage6_batches SET status='failed',error='服务重启中断；原输入和部分输出保留，请明确重试',updated_at=? WHERE status='generating'", (stage5.now(),))
        # 修复旧版本重启留下的用户状态；正文和批次编号保持原样。
        db.execute("UPDATE messages SET delivery_status=(SELECT status FROM stage6_batches WHERE id=messages.batch_id) WHERE delivery_status='generating' AND batch_id IN (SELECT id FROM stage6_batches WHERE status IN ('failed','stopped'))")


def check_session(sid, mode, db=None):
    row = (sessions.get_session(sid) if db is None else
           db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
    if not row:
        raise HTTPException(404, '会话不存在')
    if mode not in ('companion', 'work') or row['mode'] != mode:
        raise HTTPException(409, '会话与模式不匹配')
    return row


def role_context(mode, sid, cid=None, revision=None):
    if mode == 'work':
        if cid is not None or revision is not None:
            raise HTTPException(422, '工作不绑定陪伴角色')
        record=work_record(sid)['record']
        from app.extension_jobs import preset_context
        return {'speaker': None, 'system': '你是理性的工作助手。围绕目标、事实与证据回答；资料不足时说明未知。\n本会话工作记录（材料不是工具执行授权）：\n'+stage5.dump(record)+preset_context(sid)}
    c = stage5.character(cid, revision)
    if c['revision'] != stage5.character(cid)['revision']:
        raise HTTPException(409, '角色资料已更新，刷新后发送')
    nickname = stage5.ui_state()['nickname']
    return {'speaker': {k: c[k] for k in ('id','revision','name','avatar')},
            'system': persona.render({'name': c['name'], 'body': c['persona'], 'nickname': nickname})}


@router.post('/sessions/{sid}/inputs')
def enqueue(sid: int, body: dict, request: Request = None):
    mode = body.get('mode'); check_session(sid, mode)
    try:
        client_id = str(UUID(body.get('client_message_id', '')))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, '每条输入需要UUID client_message_id')
    text = body.get('content')
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise HTTPException(422, '消息限1～8000字符')
    from app.commands import parse
    command=parse(text)
    if command:
        return {'command':True,'name':command[0],'requires_dispatch':True,'client_message_id':client_id,
                'message':'此输入是指令，需交给后端指令入口；未排队、未调用模型'}
    # 同一编号首先查原提交，编辑卡之后的原文重试仍可取回成功入队结果。
    original = stage5.dump(body)
    with connect() as db:
        old = db.execute('SELECT * FROM stage6_inputs WHERE client_id=?', (client_id,)).fetchone()
        if old:
            if old['session_id'] != sid or json.loads(old['body'])['original'] != original:
                raise HTTPException(409, '输入编号已经用于其他正文或会话')
            if db.execute('SELECT archived FROM messages WHERE id=?', (old['message_id'],)).fetchone()[0]:
                raise HTTPException(409, '原输入已撤回，请使用新的发送编号')
            return {'message_id': old['message_id'], 'client_message_id': client_id}
    if chat.migrating:
        raise HTTPException(409, '迁移中，请稍后发送')
    frozen = role_context(mode, sid, body.get('character_id'), body.get('character_revision'))
    frozen['original'] = original
    if request is not None:
        from app.ui_auth import sessions as ui_sessions
        try: frozen['ui_owner'] = ui_sessions.principal(request).owner_session_id
        except HTTPException: frozen['ui_owner'] = None
    from app.reminders import current_schedule
    frozen['schedule_context'] = current_schedule.get()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # 事务内复查，抵御双击/两个页面同编号同时提交。
        expected = body.get('expected_version')
        if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool) or timeline.version(db, sid) != expected):
            raise HTTPException(409, '会话进度已变化；旧输入未重新提交，请核对后新建发送')
        from app.git_transition import assert_unclaimed
        assert_unclaimed(db,sid)
        if db.execute('SELECT 1 FROM stage6_input_cancellations WHERE client_id=?', (client_id,)).fetchone():
            raise HTTPException(409, '这条未确认输入已取消，请修改后使用新的发送编号')
        old = db.execute('SELECT * FROM stage6_inputs WHERE client_id=?', (client_id,)).fetchone()
        if old:
            if old['session_id'] != sid or json.loads(old['body'])['original'] != original:
                raise HTTPException(409, '输入编号冲突')
            if db.execute('SELECT archived FROM messages WHERE id=?', (old['message_id'],)).fetchone()[0]:
                raise HTTPException(409, '原输入已撤回，请使用新的发送编号')
            return {'message_id': old['message_id'], 'client_message_id': client_id}
        if db.execute("SELECT 1 FROM stage5_attempts WHERE session_id=? AND status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)", (sid,)).fetchone():
            raise HTTPException(409, '先用阶段5原请求恢复未确认结果，再进入新队列')
        if db.execute('SELECT COUNT(*) FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.session_id=? AND i.batch_id IS NULL AND m.archived=0 AND m.delivery_status="queued"', (sid,)).fetchone()[0] >= 20:
            raise HTTPException(409, '此会话已有20条排队输入，先处理队列')
        message_id = db.execute("INSERT INTO messages(session_id,role,content,at,delivery_status) VALUES(?,'user',?,?,'queued')", (sid,text,stage5.now())).lastrowid
        db.execute('INSERT INTO stage6_inputs VALUES(?,?,?,?,NULL)', (client_id,sid,message_id,stage5.dump(frozen)))
        timeline.checkpoint(db, sid, message_id)
        from app.work_projects import name_first_question
        name_first_question(db, sid, body['content'])
        db.execute('UPDATE sessions SET updated_at=? WHERE id=?', (stage5.now(),sid))
    return {'message_id': message_id, 'client_message_id': client_id}


@router.post('/sessions/{sid}/inputs/{client_id}/cancel')
def cancel_input(sid: int, client_id: UUID, body: dict):
    key = str(client_id)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check_session(sid, body.get('mode'), db)
        old = db.execute('SELECT session_id FROM stage6_input_cancellations WHERE client_id=?', (key,)).fetchone()
        if old:
            if old['session_id'] != sid:
                raise HTTPException(409, '发送编号不属于当前会话')
            return {'cancelled': True}
        item = db.execute('SELECT i.session_id,i.batch_id,m.id,m.archived,m.delivery_status FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.client_id=?', (key,)).fetchone()
        if item:
            if item['session_id'] != sid:
                raise HTTPException(409, '发送编号不属于当前会话')
            if not item['archived'] and item['batch_id']:
                batch = db.execute('SELECT status FROM stage6_batches WHERE id=?', (item['batch_id'],)).fetchone()
                if batch and batch['status'] == 'generating':
                    raise HTTPException(409, '输入正在回复；请先停止回复，再核对，未取消执行')
                return {'cancelled': False, 'already_processed': True}
            if not item['archived'] and item['delivery_status'] != 'queued':
                raise HTTPException(409, '输入状态已经变化，请刷新消息后核对')
            db.execute("UPDATE messages SET archived=1,delivery_status='cancelled' WHERE id=?", (item['id'],))
        # A durable cancellation also fences an earlier request which has not
        # reached its enqueue transaction yet. It cannot arrive later and run.
        db.execute('INSERT INTO stage6_input_cancellations VALUES(?,?,?)', (key, sid, stage5.now()))
    return {'cancelled': True}


def event_revision(sid, mode):
    with connect() as db:
        check_session(sid, mode, db)
        return snapshot_revision.get(db,sid)


def view(sid, mode, before=None, limit=200):
    with connect() as db:
        # 消息、批次、队列和版本必须来自同一次读取，不能混入并发回退后的版本。
        db.execute('BEGIN')
        session = check_session(sid, mode, db)
        order = "COALESCE((SELECT MIN(message_id) FROM stage6_inputs WHERE batch_id=m.batch_id),m.id)"
        args = [sid]
        condition = ''
        if before is not None:
            anchor = db.execute(f'SELECT m.id,{order} AS position FROM messages m WHERE m.id=? AND m.session_id=? AND archived=0',(before,sid)).fetchone()
            if not anchor: raise HTTPException(409,'历史位置已变化，请重新加载对话')
            condition = f' AND ({order},m.id)<(?,?)'
            args += [anchor['position'],anchor['id']]
        rows = db.execute(f'SELECT m.* FROM messages m WHERE session_id=? AND archived=0{condition} ORDER BY {order} DESC,m.id DESC LIMIT ?',[*args,limit+1]).fetchall()
        has_more = len(rows)>limit
        rows = rows[:limit]
        messages = []
        for r in reversed(rows):
            item = dict(r); item['speaker'] = json.loads(item.pop('speaker_snapshot')) if item['speaker_snapshot'] else None
            messages.append(item)
        batch_ids = list({r['batch_id'] for r in rows if r['batch_id']})
        batches = [dict(r) for r in db.execute("SELECT id,status,error,updated_at,assistant_id FROM stage6_batches WHERE session_id=? AND status!='withdrawn' AND (status='generating' OR id IN (SELECT value FROM json_each(?))) ORDER BY assistant_id", (sid,json.dumps(batch_ids)))]
        for batch in batches:
            recorded=db.execute('SELECT document FROM stage6_batch_records WHERE batch_id=?',(batch['id'],)).fetchone()
            batch['details']=json.loads(recorded[0]) if recorded else {'usage':None,'tools':[]}
            from app.conversation_approvals import enrich
            batch['details']=enrich(batch['details'],batch['status'])
            from app.conversation_questions import enrich as enrich_questions
            batch['details']=enrich_questions(batch['details'],batch['status'],db)
        event_revision=snapshot_revision.get(db,sid)
        queued = db.execute('SELECT COUNT(*) FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.session_id=? AND i.batch_id IS NULL AND m.archived=0 AND m.delivery_status="queued"', (sid,)).fetchone()[0]
    return {'session_id':sid, 'event_revision':event_revision, 'session_version':session['timeline_version'], 'mode':mode, 'messages':messages, 'batches':batches, 'queued':queued,'has_more':has_more,'oldest_id':messages[0]['id'] if messages else None}


@router.get('/sessions/{sid}/view')
def get_view(sid: int, mode: str, before: int | None = None):
    if before is not None and before < 1: raise HTTPException(422,'历史位置无效')
    return view(sid, mode, before)


@router.get('/sessions/{sid}/events')
def events(sid: int, mode: str):
    check_session(sid, mode)
    async def generate():
        last = None
        idle = 0
        while not shutting_down:
            revision = await asyncio.to_thread(event_revision,sid,mode)
            if revision != last or idle >= 30:
                data = await asyncio.to_thread(view,sid,mode)
                last = data['event_revision']
                idle = 0
                yield 'data: ' + stage5.dump({'type':'session_snapshot', **data}) + '\n\n'
            else:
                idle += 1
                if idle % 30 == 0: yield ': heartbeat\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control':'no-cache'})


def claim(sid, mode, retry_id=None, expected_version=None):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        from app.git_transition import assert_unclaimed
        assert_unclaimed(db,sid)
        if expected_version is not None and timeline.version(db, sid) != expected_version:
            return None
        if db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'", (sid,)).fetchone():
            return None
        if retry_id:
            batch = db.execute("SELECT * FROM stage6_batches WHERE id=? AND session_id=? AND status IN ('failed','stopped')", (retry_id,sid)).fetchone()
            if not batch:
                raise HTTPException(409, '只可明确重试失败或停止批次')
            if db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND assistant_id>? AND status='complete'",(sid,batch['assistant_id'])).fetchone():
                raise HTTPException(409,'后续批次已经完成；保留旧状态，请以新消息补充，不重写较早历史')
            db.execute("UPDATE stage6_batches SET status='generating',error='',updated_at=? WHERE id=?", (stage5.now(),retry_id))
            db.execute("UPDATE messages SET content='',delivery_status='generating' WHERE id=?", (batch['assistant_id'],))
            db.execute("UPDATE messages SET delivery_status='generating' WHERE batch_id=? AND role='user'",(retry_id,))
            db.execute('UPDATE stage6_batches SET session_version=? WHERE id=?', (timeline.version(db, sid), retry_id))
            return retry_id
        rows = db.execute('SELECT i.*,m.content FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.session_id=? AND i.batch_id IS NULL AND m.archived=0 AND m.delivery_status="queued" ORDER BY i.message_id', (sid,)).fetchall()
        if not rows:
            return None
        first = json.loads(rows[0]['body']); chosen = []; size = 0
        for row in rows:
            frozen = json.loads(row['body'])
            if (frozen['speaker'] != first['speaker'] or frozen['system'] != first['system']
                    or frozen.get('schedule_context') != first.get('schedule_context') or size + len(row['content']) + 2 > 8000):
                break
            chosen.append(row); size += len(row['content']) + 2
        if not chosen:  # 恰好8000字符的单条也必须允许。
            chosen = rows[:1]
        bid = str(uuid4())
        aid = db.execute("INSERT INTO messages(session_id,role,content,at,speaker_snapshot,delivery_status,batch_id) VALUES(?,'assistant','',?,?,'generating',?)", (sid,stage5.now(),stage5.dump(first['speaker']) if first['speaker'] else None,bid)).lastrowid
        db.execute('INSERT INTO stage6_batches(id,session_id,mode,status,snapshot,assistant_id,error,updated_at) VALUES(?,?,?,?,?,?,?,?)', (bid,sid,mode,'generating',stage5.dump(first),aid,'',stage5.now()))
        for row in chosen:
            db.execute('UPDATE stage6_inputs SET batch_id=? WHERE client_id=?', (bid,row['client_id']))
            db.execute("UPDATE messages SET batch_id=?,delivery_status='generating' WHERE id=?", (bid,row['message_id']))
        db.execute('UPDATE stage6_batches SET session_version=? WHERE id=?', (timeline.version(db, sid), bid))
        return bid


def store_partial(bid, text):
    with connect() as db:
        db.execute("UPDATE messages SET content=? WHERE id=(SELECT assistant_id FROM stage6_batches WHERE id=? AND status='generating')",(text,bid))


def finish(bid, status, error='', reply=None):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch = db.execute("SELECT * FROM stage6_batches WHERE id=? AND status='generating'",(bid,)).fetchone()
        if not batch or batch['session_version'] != timeline.version(db, batch['session_id']):
            return False
        if reply is not None:
            db.execute('UPDATE messages SET content=? WHERE id=?',(reply,batch['assistant_id']))
        db.execute('UPDATE messages SET delivery_status=? WHERE batch_id=?',(status,bid))
        db.execute('UPDATE stage6_batches SET status=?,error=?,updated_at=? WHERE id=?',(status,error,stage5.now(),bid))
        db.execute('UPDATE sessions SET updated_at=? WHERE id=?',(stage5.now(),batch['session_id']))
        if stage7.ready(db):
            if status=='complete':
                writer.commit_candidates(db,batch)
            else:
                writer.discard_candidates(db,bid,'回复'+status+'；候选未生效')
        if status=='complete':
            timeline.checkpoint(db, batch['session_id'], batch['assistant_id'])
        return True


def save_complete(bid, result):
    with connect() as db:
        b = db.execute('SELECT session_id,assistant_id FROM stage6_batches WHERE id=?',(bid,)).fetchone()
    if not finish(bid,'complete',reply=result['reply']):
        raise RuntimeError('批次已停止或状态已变化；迟到结果未保存，也未提交记忆')
    return {**result,'session_id':b['session_id'],'last_message_id':b['assistant_id']}


def completed_history(batch):
    """按回复批次读完整轮次；排队输入的插入编号不代表回复先后。"""
    from app.memory import v2_bridge
    if v2_bridge.active():
        return [m for g in recent_rag.groups(batch['session_id'], batch['assistant_id'])[-12:]
                for m in g['messages']]
    with connect() as db:
        answers = db.execute("SELECT id,content,batch_id FROM messages WHERE session_id=? AND archived=0 AND role='assistant' AND delivery_status='complete' AND id<? ORDER BY id DESC LIMIT 12", (batch['session_id'],batch['assistant_id'])).fetchall()
        history = []
        for answer in reversed(answers):
            if answer['batch_id']:
                users = db.execute('SELECT m.content FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.batch_id=? AND m.archived=0 ORDER BY i.message_id', (answer['batch_id'],)).fetchall()
            else:
                # v1/v2 历史也可接着聊；只取这条旧回复对应的用户输入。
                users = db.execute("SELECT content FROM messages WHERE session_id=? AND archived=0 AND role='user' AND delivery_status='complete' AND batch_id IS NULL AND id<? AND id>COALESCE((SELECT MAX(id) FROM messages WHERE session_id=? AND archived=0 AND role='assistant' AND id<?),0) ORDER BY id", (batch['session_id'],answer['id'],batch['session_id'],answer['id'])).fetchall()
            if users:
                history.extend([{'role':'user','content':'\n\n'.join(r['content'] for r in users)}, {'role':'assistant','content':answer['content']}])
    return history


def stage7_context(mode,sid,frozen,text,before_id=None):
    from app import budget
    from app.tools import specs_for
    from app.context_capacity import settings as context_settings
    cfg=context_settings(sid)
    loaded=autoload(mode,sid,cfg,query=text)
    system=frozen['system']
    from app.document_reader import MEDIA_REFERENCE
    material=bool(MEDIA_REFERENCE.search(text) or any(MEDIA_REFERENCE.search(m['content'])
        for g in recent_rag.groups(sid,before_id)[-cfg['keep_recent_turns']:]
        for m in g['messages'] if m['role']=='user'))
    from app.session_options import tool_context as with_options
    material=material or bool(frozen.get('schedule_context'))
    specs=specs_for(with_options({'schema_version':3,'mode':mode,'session_id':sid,'external_material':material,
        'ui_owner':frozen.get('ui_owner'),'scheduled':bool(frozen.get('schedule_context'))}))
    compact=recent_rag.build(chat.context_system(system,mode,True,material,sid),loaded['text'],text,mode,sid,specs,cfg,before_id)
    compact['external_material']=material
    messages=compact['messages']
    prompt_system=messages[0]['content']
    report={**budget.stage7_budget(messages,specs,cfg),'system':prompt_system,
            'messages':messages,'memory':{**loaded,'text':compact['memory']},'summary_id':compact['summary_id'],
            'summary_boundary':compact['summary_boundary'],'recent_turns':compact['recent_turns'],
            'complete_turns':compact['complete_turns'],'history_policy':compact['history_policy'],
            'recent_message_ids':compact['recent_message_ids'],'retrieval':compact['retrieval'],
            'document_reads':compact['document_reads'],'automatic_memory':not compact['external_material']}
    return {'system':system,'history':compact['history'],'memory':compact['memory'],
            'user_text':compact['user_text'],'external_material':compact['external_material'],
            'memory_settings':cfg,'report':report}


async def worker(bid):
    last_flush = 0.0
    text = ''; success = False; batch = None
    details={'usage':None,'tools':[]}
    try:
        with connect() as db:
            batch = dict(db.execute('SELECT * FROM stage6_batches WHERE id=?',(bid,)).fetchone())
            inputs = db.execute('SELECT m.id,m.content FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.batch_id=? ORDER BY m.id',(bid,)).fetchall()
        tool_ctx = writer.begin_attempt(bid)
        from app.main import ChatRequestV2
        frozen = json.loads(batch['snapshot']); mode = batch['mode']; speaker = frozen['speaker']
        body = {'mode':mode,'session_id':batch['session_id'],'request_id':bid,'expected_last_id':0,
                'messages':[{'role':'user','content':'\n\n'.join(r['content'] for r in inputs)}]}
        if speaker:
            body.update(character_id=speaker['id'],character_revision=speaker['revision'])
        from app.modules import chat_role
        from app.session_options import get as session_overrides
        body['model'] = session_overrides(batch['session_id'])['model'] or chat_role(mode)
        if mode == 'work':
            from app.extension_jobs import selected_model
            pinned = selected_model(batch['session_id'])
            if pinned:
                body['model'] = pinned[0]
        from app.session_options import effective as effective_options, temperature as session_temperature
        selected_options = effective_options(batch['session_id'])
        body['model'] = selected_options['effective']['model']
        req = ChatRequestV2(**body)
        from app.composer_compaction import before_turn
        await before_turn(batch['session_id'],body['messages'][0]['content'])
        parts=await asyncio.to_thread(stage7_context,mode,batch['session_id'],frozen,body['messages'][0]['content'],batch['assistant_id'])
        tool_ctx.update(recent_message_ids=parts['report']['recent_message_ids'],history_before_id=batch['assistant_id'],
                        external_material=parts['external_material'])
        tool_ctx.update(ui_owner=frozen.get('ui_owner'),scheduled=bool(frozen.get('schedule_context')))
        writer.set_material_policy(tool_ctx,parts['external_material'])
        details['context']=parts['report']
        with connect() as db:
            db.execute('INSERT INTO stage6_batch_records VALUES(?,?) ON CONFLICT(batch_id) DO UPDATE SET document=excluded.document',(bid,stage5.dump(details)))
        context = {'stage6':True, **{k:parts[k] for k in ('history','system','memory','memory_settings','user_text')}, 'speaker':speaker,
            'tool_ctx':tool_ctx,
            'persist':lambda result:save_complete(bid,result)}
        if mode == 'work':
            from app.extension_jobs import binding
            preset = binding(batch['session_id']).get('preset')
            if preset:
                context['preset_effort'] = preset['effort']
                context['preset_tools'] = preset['tools']
        context['preset_effort'] = selected_options['effective']['effort']
        temp_token = session_temperature.set(selected_options['effective']['temperature'])
        from app.reminders import current_schedule
        schedule_token = current_schedule.set(frozen.get('schedule_context'))
        try:
            async with aclosing(chat.run(req,request_context=context)) as stream:
                async for event in stream:
                    if event['type']=='answer':
                        text += event['text']
                        if time.monotonic()-last_flush >= .15:
                            await asyncio.to_thread(store_partial,bid,text)
                            last_flush=time.monotonic()
                    elif event['type'] in ('usage','tool','tool_start','approval','question','question_update'):
                        if event['type']=='usage':details['usage']={k:v for k,v in event.items() if k!='type'}
                        elif event['type']=='approval':details.setdefault('approvals',[]).append(event)
                        elif event['type'] in ('question','question_update'):
                            question=event['question'];items=details.setdefault('questions',[])
                            prior=next((v for v in items if v['id']==question['id']),None)
                            if prior is None:items.append(dict(question))
                            else:prior.update(question)
                        else:
                            if event.get('tool') in ('web_search','fetch_url','describe_image','search_project_docs'):
                                details.get('context',{})['external_material']=True
                                details.get('context',{})['automatic_memory']=False
                            prior=next((t for t in details['tools'] if t.get('activity_id')==event.get('activity_id')),None)
                            if prior is None:details['tools'].append(dict(event))
                            else:prior.update(event)
                            if event.get('approval_id'):
                                for approval in details.get('approvals',[]):
                                    if approval['id']==event['approval_id']:approval['status']=event['approval_status']
                        with connect() as db:
                            db.execute('INSERT INTO stage6_batch_records VALUES(?,?) ON CONFLICT(batch_id) DO UPDATE SET document=excluded.document',(bid,stage5.dump(details)))
                    elif event['type']=='complete':
                        success=True
                    elif event['type']=='work_proposal':
                        details['work_proposal']=event['proposal']
                        with connect() as db:
                            db.execute('INSERT INTO stage6_batch_records VALUES(?,?) ON CONFLICT(batch_id) DO UPDATE SET document=excluded.document',(bid,stage5.dump(details)))
        finally:
            current_schedule.reset(schedule_token)
            session_temperature.reset(temp_token)
        if not success:
            finish(bid,'failed','没有收到完整回复')
    except asyncio.CancelledError:
        store_partial(bid,text); finish(bid,'stopped','用户停止；排队输入仍保留，需明确继续')
    except Exception as exc:
        store_partial(bid,text); finish(bid,'failed',str(getattr(exc,'detail',exc)))
    finally:
        tasks.pop(bid,None)
        from app.handoff import deliver_returns
        await deliver_returns()
        with connect() as db:
            still_current = success and batch is not None and batch['session_version'] == timeline.version(db, batch['session_id'])
        if still_current:
            from app.composer_compaction import after_turn
            await after_turn(batch['session_id'])
            # Older source records stay available through validated checkpoints and local RAG.
            # 成功后自动接下一批；停止或失败不继续付费生成。
            following = claim(batch['session_id'],batch['mode'],expected_version=batch['session_version'])
            if following:
                tasks[following]=asyncio.create_task(worker(following))


@router.post('/sessions/{sid}/run')
async def run(sid: int, body: dict):
    check_session(sid,body.get('mode'))
    if chat.migrating:
        raise HTTPException(409,'迁移中，请稍后')
    # 重复启动自己的生成任务只是读取状态，追加输入无需报旧聊天冲突。
    with connect() as db:
        generating=db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'",(sid,)).fetchone()
    if generating:
        return view(sid,body['mode'])
    if body.get('only_schedule'):
        with connect() as db:
            first=db.execute('SELECT i.body FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.session_id=? AND i.batch_id IS NULL AND m.archived=0 AND m.delivery_status="queued" ORDER BY i.message_id LIMIT 1',(sid,)).fetchone()
        context=json.loads(first['body']).get('schedule_context') if first else None
        if not context or context['id']+':'+context['due']!=body['only_schedule']:
            raise HTTPException(409,'会话有其他排队输入，本次定时播报不自动启动它们')
    if ('session',sid) in chat.active:
        raise HTTPException(409,'旧聊天执行中，请稍后')
    bid = claim(sid,body['mode'],body.get('retry_batch_id'))
    if bid:
        tasks[bid]=asyncio.create_task(worker(bid))
    return view(sid,body['mode'])


@router.post('/sessions/{sid}/stop')
async def stop(sid: int, body: dict):
    check_session(sid,body.get('mode'))
    with connect() as db:
        row = db.execute("SELECT id FROM stage6_batches WHERE session_id=? AND status='generating'",(sid,)).fetchone()
    if row:
        task=tasks.get(row['id'])
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        # Also recover a claim whose in-memory worker never started. finish
        # uses a generating/version CAS and cannot overwrite a completed turn.
        finish(row['id'],'stopped','用户停止；原输入和排队内容保留')
    return view(sid,body['mode'])


@router.get('/schedule')
def schedule():
    with connect() as db:
        return {'items':[dict(r) for r in db.execute('SELECT * FROM stage6_schedule WHERE archived=0 ORDER BY date,id')]}


@router.post('/schedule')
def add_schedule(body: dict):
    title, day=body.get('title'),body.get('date')
    try:
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day): raise ValueError()
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422,'日期使用真实的YYYY-MM-DD')
    if not isinstance(title,str) or not 1<=len(title.strip())<=200:
        raise HTTPException(422,'事情名称限1～200字符')
    ident = body.get('client_id')
    try:
        ident = str(UUID(ident)) if ident is not None else str(uuid4())
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, '事项创建编号需要UUID') from None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM stage6_schedule WHERE id=?', (ident,)).fetchone()
        if old:
            if old['title'] != title.strip() or old['date'] != day or old['archived']:
                raise HTTPException(409, '创建编号已用于其他事项或该事项已删除，请重新新建')
        else:
            db.execute('INSERT INTO stage6_schedule VALUES(?,?,?,0,1,0)', (ident, title.strip(), day))
    return schedule()


@router.get('/sessions/{sid}/work-record')
def work_record(sid: int):
    check_session(sid,'work')
    with connect() as db:
        row=db.execute('SELECT document,revision FROM stage6_work_records WHERE session_id=?',(sid,)).fetchone()
    return {'revision':row['revision'] if row else 0,'record':json.loads(row['document']) if row else {'goal':'','materials':'','constraints':'','tasks':[],'status':'待开始'}}


@router.put('/sessions/{sid}/work-record')
def save_work_record(sid: int, body: dict):
    check_session(sid,'work');record=body.get('record')
    if not isinstance(record,dict) or set(record)!={'goal','materials','constraints','tasks','status'}:
        raise HTTPException(422,'工作记录需要目标、材料、限制、任务条目和状态')
    if any(not isinstance(record[k],str) for k in ('goal','materials','constraints','status')) or record['status'] not in ('待开始','进行中','暂停','完成'):
        raise HTTPException(422,'工作记录文字或状态错误')
    if not isinstance(record['tasks'],list) or len(record['tasks'])>20 or any(not isinstance(t,str) or len(t)>200 for t in record['tasks']):
        raise HTTPException(422,'任务最多20条，每条最多200字')
    if len(stage5.dump(record))>4000:
        raise HTTPException(422,'工作记录总长度最多4000字符，完整材料留文件供后续阶段检索')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT revision FROM stage6_work_records WHERE session_id=?',(sid,)).fetchone()
        if (old['revision'] if old else 0)!=body.get('expected_revision'):
            raise HTTPException(409,'工作记录已经变化，先复制本地文字，再重新打开比较')
        db.execute('INSERT INTO stage6_work_records VALUES(?,?,1) ON CONFLICT(session_id) DO UPDATE SET document=excluded.document,revision=stage6_work_records.revision+1',(sid,stage5.dump(record)))
    return work_record(sid)


@router.get('/sessions/{sid}/context')
def context_preview(sid: int, mode: str, character_id: str | None=None, character_revision: int | None=None):
    check_session(sid,mode)
    from app import budget
    from app.tools import specs_for
    with connect() as db:
        active=db.execute("SELECT * FROM stage6_batches WHERE session_id=? AND status='generating'",(sid,)).fetchone()
        queued=db.execute('SELECT i.body,m.content FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.session_id=? AND i.batch_id IS NULL AND m.archived=0 AND m.delivery_status="queued" ORDER BY i.message_id',(sid,)).fetchall()
        recorded=db.execute('SELECT document FROM stage6_batch_records WHERE batch_id=?',(active['id'],)).fetchone() if active else None
        inputs=db.execute('SELECT m.content FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.batch_id=? ORDER BY m.id',(active['id'],)).fetchall() if active else []
    frozen=json.loads(active['snapshot'] if active else queued[0]['body']) if active or queued else role_context(mode,sid,character_id,character_revision)
    report=json.loads(recorded['document']).get('context') if recorded else None
    if report is None:
        chosen=[r['content'] for r in inputs]
        size=0
        if not active:
            for row in queued:
                body=json.loads(row['body'])
                if body['speaker']!=frozen['speaker'] or body['system']!=frozen['system'] or size+len(row['content'])+2>8000:
                    break
                chosen.append(row['content']);size+=len(row['content'])+2
            if queued and not chosen:
                chosen=[queued[0]['content']]
        try:
            report=stage7_context(mode,sid,frozen,'\n\n'.join(chosen),active['assistant_id'] if active else None)['report']
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
    data=view(sid,mode)
    return {'note':'近期完整问答原文直接携带，窗口之外按需本地向量检索；按配置轮数与UTF-8字节保守估算容量确定边界。生成中展示已冻结的初始请求。',
            'initial_context':report,'system_chars':len(report['system']),'memory_chars':len(report['memory']['text']),
            'history_chars_before_trimming':sum(len(m['content'] or '') for m in data['messages'] if m['delivery_status']=='complete'),
            'pending_input_chars':sum(len(m['content'] or '') for m in data['messages'] if m['role']=='user' and m['delivery_status'] in ('queued','generating')),
            'tool_schema_chars':len(stage5.dump(specs_for({'schema_version':3}))), 'text_char_budget':budget.MAX_CONTEXT_CHARS,
            'latest_reported_usage':data['batches'][0]['details']['usage'] if data['batches'] else None}


@router.patch('/schedule/{item_id}')
def update_schedule(item_id: str, body: dict):
    if not isinstance(body.get('done'),bool):
        raise HTTPException(422,'完成状态必须是布尔值')
    with connect() as db:
        if db.execute('UPDATE stage6_schedule SET done=?,revision=revision+1 WHERE id=? AND revision=? AND archived=0',(body['done'],item_id,body.get('expected_revision'))).rowcount!=1:
            raise HTTPException(409,'日程已经变化，刷新后选择')
    return schedule()


@router.delete('/schedule/{item_id}')
def archive_schedule(item_id: str, expected_revision: int):
    with connect() as db:
        if db.execute('UPDATE stage6_schedule SET archived=1,revision=revision+1 WHERE id=? AND revision=? AND archived=0',(item_id,expected_revision)).rowcount!=1:
            raise HTTPException(409,'日程已经变化')
    return schedule()


async def shutdown():
    active_tasks=list(tasks.items())
    for _, task in active_tasks: task.cancel()
    await asyncio.gather(*(task for _,task in active_tasks),return_exceptions=True)
    # create_task 后、worker 第一次执行前也可能被取消。
    for bid, _ in active_tasks:
        finish(bid,'stopped','服务退出中断；原输入和部分输出保留，请明确继续')
        tasks.pop(bid,None)
