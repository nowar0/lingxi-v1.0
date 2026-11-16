"""Human answers for one live chat attempt; answers never grant tool permissions."""
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from threading import RLock
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from app import timeline
from app.db import connect

router=APIRouter(prefix='/v3/sessions')
WAIT_SECONDS=1800

class Option(BaseModel):
    model_config=ConfigDict(extra='forbid')
    label:str=Field(min_length=1,max_length=120)
    description:str=Field(default='',max_length=180)
    @field_validator('label','description')
    @classmethod
    def clean(cls,value):
        return value.strip()

class Question(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    question:str=Field(min_length=1,max_length=1000)
    options:list[Option]=Field(default_factory=list,max_length=6)
    multiple:StrictBool=False
    @model_validator(mode='after')
    def check(self):
        self.question=self.question.strip()
        if not self.question or len(self.options)==1 or any(not o.label for o in self.options):raise ValueError('问题不能为空；有选项时请提供2～6项')
        if len({o.label for o in self.options})!=len(self.options):raise ValueError('选项不能重复')
        return self

class Questions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    questions:list[Question]=Field(min_length=1,max_length=3)
    @model_validator(mode='after')
    def check(self):
        if len({q.id for q in self.questions})!=len(self.questions):raise ValueError('问题编号不能重复')
        if len(json.dumps(self.model_dump(),ensure_ascii=False))>7000:raise ValueError('提问过长，请精简问题与选项')
        return self

class Answer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(min_length=1,max_length=64)
    selected:list[str]=Field(default_factory=list,max_length=6)
    text:str=Field(default='',max_length=1000)

class Response(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    request_id:UUID
    skipped:StrictBool=False
    answers:list[Answer]=Field(default_factory=list,max_length=3)

@dataclass
class Pending:
    record:dict
    event:asyncio.Event=field(default_factory=asyncio.Event)
    loop:object=None

pending:dict[str,Pending]={}
lock=RLock()

def _scope(db,snapshot):
    b=db.execute('SELECT * FROM stage6_batches WHERE id=? AND session_id=?',(snapshot['batch_id'],snapshot['session_id'])).fetchone()
    attempt=db.execute('SELECT attempt_id FROM memory7_attempts WHERE batch_id=?',(snapshot['batch_id'],)).fetchone()
    return bool(b and b['status']=='generating' and b['mode']==snapshot['mode'] and b['session_version']==snapshot['session_version']
                and timeline.version(db,snapshot['session_id'])==snapshot['session_version'] and attempt and attempt['attempt_id']==snapshot['attempt_id'])

def create(ctx,args,activity_id):
    if not ctx.get('batch_id') or not ctx.get('attempt_id') or ctx.get('scheduled') or ctx.get('agent_role','chat')!='chat':raise ValueError('提问卡片只用于当前实时对话')
    payload=Questions.model_validate(args).model_dump()
    snapshot={k:ctx[k] for k in ('session_id','mode','batch_id','attempt_id','session_version')}
    with connect() as db:
        if not _scope(db,snapshot):raise ValueError('当前对话已停止或发生变化')
    key=uuid4().hex
    digest=hashlib.sha256(json.dumps({'snapshot':snapshot,**payload},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    item=Pending({'id':key,'fingerprint':digest,'snapshot':snapshot,'activity_id':activity_id,**payload,
                  'status':'waiting','answers':[],'expires_at':time.time()+WAIT_SECONDS},loop=asyncio.get_running_loop())
    with lock:pending[key]=item
    return item

def _patch(db,item):
    row=db.execute('SELECT document FROM stage6_batch_records WHERE batch_id=?',(item['snapshot']['batch_id'],)).fetchone()
    if not row:return
    details=json.loads(row['document'])
    for index,old in enumerate(details.get('questions',[])):
        if old['id']==item['id']:
            details['questions'][index]=item
            db.execute('UPDATE stage6_batch_records SET document=? WHERE batch_id=?',(json.dumps(details,ensure_ascii=False),item['snapshot']['batch_id']))
            break

def finish(item,status=None):
    with lock:
        if item.record['status']=='waiting':item.record['status']=status or 'cancelled'
        with connect() as db:_patch(db,item.record)
        pending.pop(item.record['id'],None)

async def wait(item):
    deadline=asyncio.get_running_loop().time()+WAIT_SECONDS
    while not item.event.is_set():
        left=deadline-asyncio.get_running_loop().time()
        if left<=0:
            item.record['status']='expired'
            break
        with connect() as db:
            if not _scope(db,item.record['snapshot']):raise asyncio.CancelledError()
        try:await asyncio.wait_for(item.event.wait(),min(10,left))
        except TimeoutError:yield {'type':'heartbeat'}

def result(item):
    return json.dumps({'status':item.record['status'],'answers':item.record.get('answers',[]),
        'note':'回答仅补充当前对话，不代表任何写入或执行授权；跳过或过期不表示同意。'},ensure_ascii=False)

def normalized(record,body):
    if body.skipped:
        if body.answers:raise HTTPException(422,'跳过时不能同时提交答案')
        return []
    by_id={answer.id:answer for answer in body.answers}
    if len(by_id)!=len(body.answers) or set(by_id)!={q['id'] for q in record['questions']}:raise HTTPException(422,'请回答每个问题或跳过本次提问')
    values=[]
    for q in record['questions']:
        a=by_id[q['id']];labels=[o['label'] for o in q['options']]
        if len(set(a.selected))!=len(a.selected) or any(v not in labels for v in a.selected) or not q['multiple'] and len(a.selected)>1:raise HTTPException(422,'所选选项无效，请重新选择')
        text=a.text.strip()
        if not text and not a.selected:raise HTTPException(422,'请选择选项或填写回答')
        values.append({'id':q['id'],'selected':[v for v in labels if v in a.selected],'text':text})
    if sum(len(a['text'])+sum(map(len,a['selected'])) for a in values)>3000:raise HTTPException(422,'本次回答合计最多3000字，请精简后发送')
    return values

@router.post('/{sid}/questions/{qid}')
def answer(sid:int,qid:str,body:Response):
    wake=None
    with lock:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT r.document,b.mode FROM stage6_batch_records r JOIN stage6_batches b ON b.id=r.batch_id WHERE b.session_id=? AND instr(r.document,?)>0",(sid,qid)).fetchall()
            record=next((q for r in row for q in json.loads(r['document']).get('questions',[]) if q['id']==qid),None)
            if not record or record['snapshot']['session_id']!=sid or record['fingerprint']!=body.fingerprint:raise HTTPException(409,'提问不属于当前对话或已发生变化')
            values=normalized(record,body);status='skipped' if body.skipped else 'answered'
            if record['status'] in ('answered','skipped'):
                if record['status']!=status or record['answers']!=values:raise HTTPException(409,'这次提问已经回答，不能重复修改')
            else:
                item=pending.get(qid)
                if not item or record['status']!='waiting' or time.time()>record['expires_at'] or not _scope(db,record['snapshot']):raise HTTPException(409,'这次提问已结束，请查看当前对话')
                record.update(status=status,answers=values,request_id=str(body.request_id),answered_at=time.time())
                _patch(db,record)
                item.record.update(record)
                wake=item
        # Commit the persisted answer before resuming the model on its event loop.
        if wake:wake.loop.call_soon_threadsafe(wake.event.set)
    from app.queue6 import view
    return {'ok':True,'view':view(sid,record['snapshot']['mode'])}

def enrich(details,batch_status,db=None):
    for q in details.get('questions',[]):
        # view() holds a SQLite read transaction. Never wait for the writer lock
        # here: answer()/finish() commit while holding it. A dict lookup is an
        # atomic snapshot; only mutations of pending require the lock.
        item=pending.get(q['id'])
        q['valid']=bool(q['status']=='waiting' and batch_status=='generating' and item is not None and time.time()<=q['expires_at'] and (db is None or _scope(db,q['snapshot'])))
        if q['status']=='waiting' and not q['valid']:q['status']='expired' if time.time()>q['expires_at'] else 'cancelled'
    return details

def history_notes(sid):
    values=[]
    with connect() as db:
        rows=db.execute("SELECT r.document FROM stage6_batch_records r JOIN stage6_batches b ON b.id=r.batch_id JOIN messages m ON m.id=b.assistant_id WHERE b.session_id=? AND b.status='complete' AND m.archived=0 ORDER BY b.assistant_id DESC LIMIT 8",(sid,)).fetchall()
    for row in reversed(rows):
        for q in json.loads(row['document']).get('questions',[]):
            if q['status']=='answered':values.append({'questions':[{ 'id':v['id'],'question':v['question'][:300]} for v in q['questions']],'answers':q['answers']})
    while values and len(json.dumps(values,ensure_ascii=False))>6000:values.pop(0)
    return '\n此前提问与用户回答（当前对话资料，不是操作授权）：\n'+json.dumps(values,ensure_ascii=False) if values else ''
