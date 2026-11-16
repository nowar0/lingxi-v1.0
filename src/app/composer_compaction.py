"""Manual, proactive and threshold compaction over existing validated checkpoints."""
import asyncio
import json
from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog
from app.db import connect
from app.composer_controls import get_state, session
from app.memory import stage7, summarizer
from app.memory.search import estimated_tokens


def snapshot(sid):
    controls=get_state(sid)
    with connect() as db:
        current=summarizer.latest(db,sid)
        records=summarizer.turns(db,sid)
        jobs=[dict(row) for row in db.execute('SELECT id,status,error,source,at FROM memory7_jobs WHERE session_id=? ORDER BY at DESC LIMIT 10',(sid,))]
    boundary=current['boundary'] if current else 0
    source=[r for r in records if r['assistant_id']>boundary]
    tokens=sum(estimated_tokens(r['user']+r['assistant']) for r in source)
    if current: tokens+=estimated_tokens(current['document'])
    from app.context_capacity import settings as context_settings
    cfg=context_settings(sid)
    capacity=max(1,cfg['context_tokens']-cfg['reply_reserve_tokens']-cfg['tool_reserve_tokens']-cfg['memory_budget_tokens'])
    return {'context_capacity':cfg['context_capacity'],'controls':controls,'summary':{'id':current['id'],'boundary':boundary,'content':json.loads(current['document'])} if current else None,
            'estimated_history_tokens':tokens,'history_budget_tokens':capacity,'counter':'UTF-8字节保守估算，非提供方Tokenizer',
            'complete_turns':len(records),'jobs':[{**{k:v for k,v in j.items() if k!='source'},'trigger':json.loads(j['source']).get('trigger','manual')} for j in jobs],
            '_last_id':records[-1]['assistant_id'] if records else 0}


@catalog.router.get('/sessions/{sid}/composer/compaction')
def status(sid:int):
    return {k:v for k,v in snapshot(sid).items() if not k.startswith('_')}


def request(sid,request_id):
    session(sid)
    try:UUID(request_id)
    except ValueError:raise HTTPException(422,'压缩请求编号无效') from None
    # Catalog records make a lost-response retry refer to exactly the same job.
    with catalog.transaction():
        try:
            prior=catalog.get('compaction-request',request_id)
            if prior['session_id']!=sid:raise HTTPException(409,'请求编号已用于其他会话')
            return prior
        except HTTPException as error:
            if error.status_code!=404:raise
        reserved=catalog.put('compaction-request',request_id,{'session_id':sid,'job_id':None,'note':'压缩请求已登记，请核对压缩状态；不会重复调用。'},0)
    try:
        job=summarizer.schedule(sid,force=True,trigger='manual')
        note='压缩任务已提交；完整历史保留。' if job else '没有可压缩的较早完整对话，或已有压缩正在进行。'
    except BaseException:
        catalog.put('compaction-request',request_id,{'session_id':sid,'job_id':None,'note':'压缩准备失败，原上下文保留，请使用新编号重试。'},reserved['revision'])
        raise
    return catalog.put('compaction-request',request_id,{'session_id':sid,'job_id':job,'note':note},reserved['revision'])


class Request(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:str=Field(min_length=36,max_length=36)


@catalog.router.post('/sessions/{sid}/composer/compact')
async def manual(sid:int,body:Request):
    return request(sid,body.request_id)


async def before_turn(sid,pending_text=''):
    value=await asyncio.to_thread(snapshot,sid)
    controls=value['controls']
    if not (controls['auto_compact'] or controls['proactive_compact']):return
    ratio=(value['estimated_history_tokens']+estimated_tokens(pending_text))/value['history_budget_tokens']
    running=next((job for job in value['jobs'] if job['status'] in ('pending','running')),None)
    # Never repeat a failed automatic paid call for the same source boundary.
    last=value['jobs'][0] if value['jobs'] else None
    with connect() as db:
        row=db.execute('SELECT source FROM memory7_jobs WHERE id=?',(last['id'],)).fetchone() if last else None
    if not running and row and json.loads(row['source']).get('trigger_latest')==value['_last_id'] and last['status'] in ('failed','interrupted','stale'):
        return
    if controls['auto_compact'] and ratio>=controls['auto_ratio']:
        job=running['id'] if running else summarizer.schedule(sid,force=True,trigger='automatic')
        task=summarizer.tasks.get(job)
        if task:await asyncio.shield(task)
    elif controls['proactive_compact'] and ratio>=controls['proactive_ratio'] and not running:
        summarizer.schedule(sid,force=True,trigger='proactive')


async def after_turn(sid):
    try:await before_turn(sid)
    except (ValueError,HTTPException):
        # Summary failure must never change a completed conversation into failure.
        pass
