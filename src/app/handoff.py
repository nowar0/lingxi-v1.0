"""Chat proposals create pending work exactly once, never implicitly start it."""
import asyncio
import hashlib
import json
import re
from uuid import uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, sessions, stage5
from app.db import connect

_locks = {}


def init_table():
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS work_proposals(
            id TEXT PRIMARY KEY,session_id INTEGER NOT NULL,topic TEXT NOT NULL,batch_id TEXT,
            goal TEXT NOT NULL,acceptance TEXT NOT NULL,why TEXT NOT NULL,status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,work_session_id INTEGER,job_id TEXT,error TEXT NOT NULL DEFAULT '',
            created_message_id INTEGER NOT NULL,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE UNIQUE INDEX IF NOT EXISTS work_proposal_batch ON work_proposals(session_id,batch_id) WHERE batch_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS work_handoff_links(origin_session_id INTEGER PRIMARY KEY,work_session_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS work_handoff_returns(job_id TEXT PRIMARY KEY,origin_session_id INTEGER NOT NULL,
                status TEXT NOT NULL,message_id INTEGER);''')
        columns={row['name'] for row in db.execute('PRAGMA table_info(work_jobs)')}
        if 'origin_session_id' not in columns:
            db.execute('ALTER TABLE work_jobs ADD COLUMN origin_session_id INTEGER')
        db.execute("UPDATE work_proposals SET status='failed',error='上次拆分中断，可能已消耗模型调用；请明确重试',revision=revision+1 WHERE status='planning'")


def present(row):
    return {**dict(row), 'planning_calls':1, 'est_nodes':None,
            'cost_hint':'创建时将调用模型拆分，预计1次调用；创建后仍需另行启动任务',
            'link': '#/tasks?job=' + row['job_id'] if row['job_id'] else None}


def suggest(ctx,goal,acceptance,why,manual=False):
    sid=ctx.get('session_id')
    session=sessions.get_session(sid)
    if not session or session['mode'] not in ('work','companion') or ctx.get('agent_role') in ('worker','orchestrator'):
        raise ValueError('只有当前聊天能提出工作建议')
    if not goal.strip() or len(goal)>12000 or not acceptance.strip() or len(acceptance)>8000 or len(why)>2000:
        raise ValueError('工作目标或验收条件不完整或过长')
    batch=ctx.get('batch_id') if not manual else None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if batch:
            existing=db.execute('SELECT * FROM work_proposals WHERE session_id=? AND batch_id=?',(sid,batch)).fetchone()
            if existing: return present(existing)
        busy=db.execute("SELECT * FROM work_proposals WHERE session_id=? AND status='planning'",(sid,)).fetchone()
        if busy: raise ValueError('当前会话已有一个转接正在拆分，请等待完成')
        if not manual:
            rejected=db.execute("SELECT * FROM work_proposals WHERE session_id=? AND status='rejected' ORDER BY rowid DESC LIMIT 1",(sid,)).fetchone()
            if rejected:
                turns=db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant' AND archived=0 AND id>? AND delivery_status='complete'",(sid,rejected['created_message_id'])).fetchone()[0]
                if turns<3: return {'suppressed':True,'reason':'用户刚拒绝工作建议，三轮内不再次打扰'}
        last=db.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?',(sid,)).fetchone()[0]
        identifier=uuid4().hex
        topic=hashlib.sha256(goal.strip().casefold().encode()).hexdigest()
        db.execute('INSERT INTO work_proposals(id,session_id,topic,batch_id,goal,acceptance,why,status,created_message_id) VALUES(?,?,?,?,?,?,?,?,?)',
            (identifier,sid,topic,batch,goal.strip(),acceptance.strip(),why.strip(),'proposed',last))
        return present(db.execute('SELECT * FROM work_proposals WHERE id=?',(identifier,)).fetchone())


class Suggest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    goal:str=Field(min_length=1,max_length=12000)
    acceptance:str=Field(default='提供可复核的成果并说明实际验证结果',min_length=1,max_length=8000)
    why:str=Field(default='用户主动转交工作模块',max_length=2000)


@catalog.router.post('/sessions/{sid}/suggest-work-task')
def manual_suggest(sid:int,body:Suggest):
    return suggest({'session_id':sid},**body.model_dump(),manual=True)


@catalog.router.get('/sessions/{sid}/work-proposals')
def proposals(sid:int):
    if not sessions.get_session(sid): raise HTTPException(404,'会话不存在')
    with connect() as db:
        rows=db.execute('SELECT * FROM work_proposals WHERE session_id=? ORDER BY rowid DESC LIMIT 20',(sid,)).fetchall()
    return {'items':[present(row) for row in rows]}


class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=1)
    accept:bool
    allow_planning_cost:bool=False
    retry:bool=False


@catalog.router.post('/sessions/{sid}/work-proposals/{pid}/decision')
async def decide(sid:int,pid:str,body:Decision):
    from app import jobs, workspace
    lock=_locks.setdefault(pid,asyncio.Lock())
    async with lock:
        with connect() as db:
            row=db.execute('SELECT * FROM work_proposals WHERE id=? AND session_id=?',(pid,sid)).fetchone()
        if not row: raise HTTPException(404,'当前会话没有此工作建议')
        if row['job_id']: return present(row)
        if row['revision']!=body.expected_revision: raise HTTPException(409,'工作建议已变化，请刷新')
        if not body.accept:
            with connect() as db:
                db.execute("UPDATE work_proposals SET status='rejected',revision=revision+1 WHERE id=? AND revision=?",(pid,body.expected_revision))
            return next(item for item in proposals(sid)['items'] if item['id']==pid)
        if not body.allow_planning_cost: raise HTTPException(422,'拆分前需确认本次模型调用')
        if row['status']=='failed' and not body.retry: raise HTTPException(409,'上次拆分失败；请明确确认重试可能产生的新调用')
        if row['status'] not in ('proposed','failed'): raise HTTPException(409,'建议当前不能再次拆分；选择目录后请使用继续入口')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM work_proposals WHERE session_id=? AND status='planning' AND id!=?",(sid,pid)).fetchone():
                raise HTTPException(409,'当前会话已有另一个转接正在拆分')
            db.execute("UPDATE work_proposals SET status='planning',error='',revision=revision+1 WHERE id=? AND revision=?",(pid,body.expected_revision))
            link=db.execute('SELECT work_session_id FROM work_handoff_links WHERE origin_session_id=?',(sid,)).fetchone()
        if sessions.get_session(sid)['mode']=='work':
            target=sid
        elif link and sessions.get_session(link['work_session_id']) and not sessions.get_session(link['work_session_id'])['archived']:
            target=link['work_session_id']
        else:
            target=stage5.create_session(stage5.SessionInput(mode='work',creation_id='handoff-'+str(sid)+'-'+pid,title=row['goal'][:20]))['id']
            with connect() as db:
                db.execute('INSERT INTO work_handoff_links VALUES(?,?) ON CONFLICT(origin_session_id) DO UPDATE SET work_session_id=excluded.work_session_id',(sid,target))
        try:
            attachments=transfer_attachments(sid,target,pid,row['goal'])
            request=await jobs.build_plan(jobs.Plan(session_id=target,goal=row['goal'],acceptance=row['acceptance'],materials='来自聊天的用户确认目标；原会话保持不变。明确引用的附件映射：'+json.dumps(attachments,ensure_ascii=False)))
            bound=[item for item in catalog.listing('workspace') if item['session_id']==target]
            needs_files=any(any(name.startswith('workspace_') for name in (node.tools or [])) for node in request.nodes)
            if needs_files and not bound:
                # Keep the proposed plan so selecting a directory does not spend
                # a second planning call. The job is not created until confirmed.
                catalog.put('handoff-plan',pid,{'plan':request.model_dump(),'origin':sid},0)
                with connect() as db:
                    db.execute("UPDATE work_proposals SET status='needs_workspace',work_session_id=?,error='请选择工作目录后继续；已拆分的计划会复用',revision=revision+1 WHERE id=?",(target,pid))
                return next(item for item in proposals(sid)['items'] if item['id']==pid)
            if not bound:
                for node in request.nodes:
                    if node.capability=='text': node.tools=[]
            return commit_plan(sid,pid,target,request)
        except BaseException as error:
            with connect() as db:
                db.execute("UPDATE work_proposals SET status='failed',error=?,revision=revision+1 WHERE id=? AND job_id IS NULL",(str(getattr(error,'detail',error))[:500] or '拆分被中断',pid))
            raise


def commit_plan(sid,pid,target,request):
    from app import jobs
    jobs.validate_job(request)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM work_proposals WHERE id=? AND session_id=?',(pid,sid)).fetchone()
        if row['job_id']: return present(row)
        if row['status'] not in ('planning','needs_workspace'): raise HTTPException(409,'建议已取消或状态发生变化')
        identifier=jobs.create_pending(db,request)
        db.execute('UPDATE work_jobs SET origin_session_id=? WHERE id=?',(sid,identifier))
        db.execute("UPDATE work_proposals SET status='created',work_session_id=?,job_id=?,error='',revision=revision+1 WHERE id=?",(target,identifier,pid))
        return present(db.execute('SELECT * FROM work_proposals WHERE id=?',(pid,)).fetchone())


@catalog.router.post('/sessions/{sid}/work-proposals/{pid}/resume')
async def resume(sid:int,pid:str,body:Decision):
    from app import jobs
    async with _locks.setdefault(pid,asyncio.Lock()):
        if not body.accept: raise HTTPException(422,'继续创建需要用户明确确认')
        with connect() as db:
            row=db.execute('SELECT * FROM work_proposals WHERE id=? AND session_id=?',(pid,sid)).fetchone()
        if not row: raise HTTPException(404,'建议不存在')
        if row['job_id']: return present(row)
        if row['revision']!=body.expected_revision or row['status']!='needs_workspace': raise HTTPException(409,'建议状态已变化')
        target=row['work_session_id']
        if not any(item['session_id']==target for item in catalog.listing('workspace')): raise HTTPException(409,'请先为目标工作会话选择目录')
        saved=catalog.get('handoff-plan',pid)
        if saved['origin']!=sid: raise HTTPException(409,'计划来源不一致')
        return commit_plan(sid,pid,target,jobs.Job(**saved['plan']))


def transfer_attachments(origin,target,pid,goal):
    if origin==target:return []
    from app import media
    from app.extensions import optional
    saved=optional('handoff-media',pid)
    if 'items' in saved:return saved['items']
    identifiers=list(dict.fromkeys(re.findall(r'/v6/media/([a-f0-9]{32})',goal)))[:20]
    copied=[]
    created=[]
    try:
        with catalog.transaction() as db:
            for identifier in identifiers:
                row,path=media.read(identifier,origin)
                result=media.save(path.read_bytes(),row['mime'],target,row['name'],db=db,created=created)
                copied.append({'origin_id':identifier,'attachment_id':result['id'],'mime':row['mime'],'name':row['name']})
            catalog.put('handoff-media',pid,{'items':copied},0)
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        raise
    return copied


def append_notice(sid,text,key):
    """One assistant notice when idle; no user impersonation or model invocation."""
    from app import chat,timeline
    if ('session',sid) in chat.active: return None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        session=db.execute('SELECT * FROM sessions WHERE id=? AND archived=0',(sid,)).fetchone()
        if not session: return None
        if db.execute("SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'",(sid,)).fetchone(): return None
        if db.execute("SELECT 1 FROM messages WHERE session_id=? AND archived=0 AND delivery_status='queued'",(sid,)).fetchone(): return None
        old=db.execute('SELECT result FROM completed_requests WHERE request_id=?',(key,)).fetchone()
        if old: return json.loads(old['result'])['last_message_id']
        mid=db.execute("INSERT INTO messages(session_id,role,content,at,delivery_status) VALUES(?,'assistant',?,?,'complete')",(sid,text,stage5.now())).lastrowid
        db.execute('INSERT INTO completed_requests VALUES(?,?,?)',(key,hashlib.sha256(key.encode()).hexdigest(),json.dumps({'last_message_id':mid,'session_id':sid})))
        db.execute('UPDATE sessions SET updated_at=? WHERE id=?',(stage5.now(),sid))
        timeline.checkpoint(db,sid,mid)
        return mid


async def deliver_returns():
    with connect() as db:
        rows=db.execute("""SELECT j.* FROM work_jobs j LEFT JOIN work_handoff_returns r ON r.job_id=j.id
            WHERE j.origin_session_id IS NOT NULL AND j.status IN ('complete','cancelled')
            AND (r.status IS NULL OR r.status NOT IN ('delivered','source_unavailable')) ORDER BY j.rowid LIMIT 100""").fetchall()
    for row in rows:
        origin=sessions.get_session(row['origin_session_id'])
        if not origin or origin['archived']:
            with connect() as db:
                db.execute("INSERT INTO work_handoff_returns VALUES(?,?,'source_unavailable',NULL) ON CONFLICT(job_id) DO UPDATE SET status='source_unavailable'",(row['id'],row['origin_session_id']))
            continue
        document=json.loads(row['document'])
        summary=(document.get('review_report') or '查看工作模块中的节点成果与实际工具记录').replace('\n',' ')[:200]
        state='已完成并验收' if row['status']=='complete' else '已取消'
        text=f"工作任务「{document['goal'][:80]}」{state}。\n\n{summary}\n\n[查看任务](#/tasks?job={row['id']})"
        mid=append_notice(row['origin_session_id'],text,'task:'+row['id'])
        with connect() as db:
            db.execute('INSERT INTO work_handoff_returns VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,message_id=excluded.message_id',
                (row['id'],row['origin_session_id'],'delivered' if mid else 'waiting_idle',mid))
