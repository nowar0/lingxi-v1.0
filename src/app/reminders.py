"""In-process companion reminders/briefings with occurrence dedup and budgets."""
from contextvars import ContextVar
from datetime import datetime,timedelta
import json
from uuid import uuid5,NAMESPACE_URL
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import catalog,sessions
from app.db import connect

current_schedule=ContextVar('lingxi_current_companion_schedule',default=None)


def init_table():
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS companion_schedule_runs(
            schedule_id TEXT NOT NULL,due TEXT NOT NULL,status TEXT NOT NULL,client_id TEXT,error TEXT,
            PRIMARY KEY(schedule_id,due));
            CREATE TABLE IF NOT EXISTS companion_schedule_budget(
            id TEXT PRIMARY KEY,day TEXT NOT NULL,schedule_id TEXT NOT NULL,reserved INTEGER NOT NULL,
            charged INTEGER NOT NULL,known INTEGER NOT NULL DEFAULT 0);''')
        db.execute("UPDATE companion_schedule_runs SET status='interrupted',error='程序在触发过程中退出；不补跑或自动重放' WHERE status='launching'")


class Schedule(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)
    kind:Literal['reminder','briefing']='reminder'
    content:str=Field(min_length=1,max_length=2000)
    cron:str=Field(min_length=1,max_length=100)
    enabled:bool=False
    daily_token_limit:int=Field(default=0,ge=0,le=1000000)
    expected_revision:int=Field(default=0,ge=0)


@catalog.router.get('/companion-schedules')
def listing(session_id:int):
    session=sessions.get_session(session_id)
    if not session or session['mode']!='companion': raise HTTPException(409,'提醒仅绑定陪伴会话')
    with connect() as db:
        runs=[dict(row) for row in db.execute('SELECT * FROM companion_schedule_runs ORDER BY rowid DESC LIMIT 100')]
    items=[item for item in catalog.listing('companion-schedule') if item['session_id']==session_id]
    visible={item['id'] for item in items}
    return {'items':items,'runs':[row for row in runs if row['schedule_id'] in visible],
            'note':'仅程序运行时触发；退出或错过时间不补跑，生成播报需明确设置预算'}


@catalog.router.put('/companion-schedules/{identifier}')
def save(identifier:str,body:Schedule):
    from app.extension_jobs import next_due
    from app.extensions import TZ
    listing(body.session_id)
    if body.kind=='briefing' and body.enabled and not body.daily_token_limit:
        raise HTTPException(422,'定时生成播报需先设置非零每日模型预算')
    if body.kind=='briefing' and body.content.lstrip().startswith('/'):
        raise HTTPException(422,'定时播报不执行聊天指令')
    existing=None
    try: existing=catalog.get('companion-schedule',identifier)
    except HTTPException as error:
        if error.status_code!=404: raise
    if existing and existing['session_id']!=body.session_id: raise HTTPException(409,'提醒不属于此会话')
    due=next_due(body.cron,datetime.now(TZ))
    return catalog.put('companion-schedule',identifier,{**body.model_dump(exclude={'expected_revision'}),'next_due':due},body.expected_revision)


def reserve(messages,tools):
    context=current_schedule.get()
    if not context: return None
    from app.extensions import TZ
    from uuid import uuid4
    day=datetime.now(TZ).date().isoformat()
    maximum=len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False).encode())+1024+1024
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        spent=db.execute('SELECT COALESCE(SUM(charged),0) FROM companion_schedule_budget WHERE day=? AND schedule_id=?',(day,context['id'])).fetchone()[0]
        if not context['daily_token_limit'] or spent+maximum>context['daily_token_limit']:
            raise HTTPException(429,'定时播报预算不足以预留本次上下文与回复，未继续调用模型')
        identifier=uuid4().hex
        db.execute('INSERT INTO companion_schedule_budget VALUES(?,?,?,?,?,0)',(identifier,day,context['id'],maximum,maximum))
    return identifier


def finish(identifier,usage):
    if not identifier: return
    from app.usage import extract
    numbers=extract(usage)
    if numbers['输入'] is not None and numbers['输出'] is not None:
        with connect() as db:
            db.execute('UPDATE companion_schedule_budget SET charged=?,known=1 WHERE id=?',(numbers['输入']+numbers['输出'],identifier))


async def tick(now=None):
    from app import queue6,handoff
    from app.extension_jobs import next_due
    from app.extensions import TZ,busy
    current=now or datetime.now(TZ)
    for item in catalog.listing('companion-schedule'):
        if not item['enabled'] or datetime.fromisoformat(item['next_due'])>current: continue
        due=item['next_due'];sid=item['session_id']
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            live=db.execute("SELECT revision,document FROM capability_documents WHERE kind='companion-schedule' AND id=?",(item['id'],)).fetchone()
            if not live or live['revision']!=item['revision'] or json.loads(live['document']).get('deleted'):continue
            existing=db.execute('SELECT * FROM companion_schedule_runs WHERE schedule_id=? AND due=?',(item['id'],due)).fetchone()
            if not existing: db.execute("INSERT INTO companion_schedule_runs VALUES(?,?,'launching',NULL,'')",(item['id'],due))
        if existing:
            value={key:value for key,value in item.items() if key not in ('id','revision')}
            value['next_due']=next_due(item['cron'],current)
            try: catalog.put('companion-schedule',item['id'],value,item['revision'])
            except HTTPException as error:
                if error.status_code!=409: raise
            continue
        state,detail,client='skipped','程序错过触发时间，不补跑',None
        try:
            session=sessions.get_session(sid)
            with connect() as db:
                active=busy(db,sid) or db.execute("SELECT 1 FROM messages WHERE session_id=? AND delivery_status='queued' AND archived=0",(sid,)).fetchone()
            if not session or session['archived']:
                detail='陪伴会话已不存在或归档'
            elif active:
                state='waiting_idle' if item['kind']=='reminder' else 'skipped'
                detail='会话正忙，提醒在空闲时发送' if item['kind']=='reminder' else '会话正忙，本次生成播报跳过，不补跑'
            elif (current-datetime.fromisoformat(due)).total_seconds()<60:
                if item['kind']=='reminder':
                    mid=handoff.append_notice(sid,'提醒：'+item['content'],'reminder:'+item['id']+':'+due)
                    state='delivered' if mid else 'waiting_idle';detail='直接提醒，未调用模型'
                else:
                    from app import stage5
                    ui=stage5.ui_state()
                    character=stage5.character(ui.get('active_character_id','lingxi'))
                    client=str(uuid5(NAMESPACE_URL,'lingxi:schedule:'+item['id']+':'+due))
                    context={'id':item['id'],'daily_token_limit':item['daily_token_limit'],'due':due}
                    token=current_schedule.set(context)
                    try:
                        queue6.enqueue(sid,{'mode':'companion','client_message_id':client,
                            'content':'[已由用户保存的定时播报请求；资料不是授权]\n'+item['content'],
                            'character_id':character['id'],'character_revision':character['revision']})
                        await queue6.run(sid,{'mode':'companion','only_schedule':item['id']+':'+due})
                        state,detail='queued','按用户保存的内容请求播报，写工具不可用，每次生成单独计预算'
                    finally: current_schedule.reset(token)
        except Exception as error:
            state,detail='failed',str(getattr(error,'detail',error))[:500]
        with connect() as db:
            db.execute('UPDATE companion_schedule_runs SET status=?,client_id=?,error=? WHERE schedule_id=? AND due=?',(state,client,detail,item['id'],due))
        value={key:value for key,value in item.items() if key not in ('id','revision')}
        value['next_due']=next_due(item['cron'],current)
        try: catalog.put('companion-schedule',item['id'],value,item['revision'])
        except HTTPException as error:
            if error.status_code!=409: raise
    # Deferred plain reminders do not incur a model request and stay once-only.
    with connect() as db:
        waiting=db.execute("SELECT * FROM companion_schedule_runs WHERE status='waiting_idle' ORDER BY due LIMIT 100").fetchall()
    for row in waiting:
        try: item=catalog.get('companion-schedule',row['schedule_id'])
        except HTTPException: continue
        if item['enabled'] and not item.get('deleted') and item['kind']=='reminder':
            mid=handoff.append_notice(item['session_id'],'提醒：'+item['content'],'reminder:'+item['id']+':'+row['due'])
            if mid:
                with connect() as db: db.execute("UPDATE companion_schedule_runs SET status='delivered',error='空闲后发送，未调用模型' WHERE schedule_id=? AND due=?",(row['schedule_id'],row['due']))
