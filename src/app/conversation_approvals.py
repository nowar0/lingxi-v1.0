"""Live queue-chat approvals; old cards cannot authorize new attempts."""
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import approvals,queue6,timeline
import json
from app.db import connect

router=APIRouter(prefix='/v3/sessions')

class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    allowed:bool=Field(strict=True)
    fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    batch_id:str=Field(min_length=1,max_length=100)
    attempt_id:str=Field(min_length=1,max_length=100)
    session_version:int=Field(ge=0)

@router.post('/{sid}/approvals/{approval_id}')
async def decide(sid:int,approval_id:str,body:Decision):
    item=approvals.pending.get(approval_id)
    if not item:raise HTTPException(409,'本次审批已失效；请查看当前工具活动')
    saved=item.snapshot
    if (saved.get('session_id')!=sid or saved.get('batch_id')!=body.batch_id or saved.get('attempt_id')!=body.attempt_id
            or saved.get('session_version')!=body.session_version or item.fingerprint!=body.fingerprint):
        raise HTTPException(409,'审批与当前会话或工具参数不一致')
    if approvals.state(approval_id,body.fingerprint)!='waiting':raise HTTPException(409,'本次审批已处理或过期')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        batch=db.execute('SELECT status,session_version FROM stage6_batches WHERE id=? AND session_id=?',(body.batch_id,sid)).fetchone()
        attempt=db.execute('SELECT attempt_id FROM memory7_attempts WHERE batch_id=?',(body.batch_id,)).fetchone()
        if not batch or batch['status']!='generating' or batch['session_version']!=body.session_version or timeline.version(db,sid)!=body.session_version or not attempt or attempt['attempt_id']!=body.attempt_id:
            raise HTTPException(409,'此轮已停止、回退或重试；旧审批不能继续执行')
        record=db.execute('SELECT document FROM stage6_batch_records WHERE batch_id=?',(body.batch_id,)).fetchone()
        if record:
            details=json.loads(record['document'])
            for card in details.get('approvals',[]):
                if card['id']==approval_id:card['status']='approved' if body.allowed else 'denied'
            db.execute('UPDATE stage6_batch_records SET document=? WHERE batch_id=?',(json.dumps(details,ensure_ascii=False),body.batch_id))
    if not approvals.decide(approval_id,body.allowed,body.fingerprint):raise HTTPException(409,'本次审批已过期或处理过')
    return {'ok':True,'view':queue6.view(sid,saved['mode'])}

def enrich(details,batch_status):
    approvals_by_id={a['id']:a for a in details.get('approvals',[])}
    for item in approvals_by_id.values():
        if item.get('status') not in ('executed','failed','denied','expired','cancelled'):
            item['status']=approvals.state(item['id'],item.get('fingerprint')) if batch_status=='generating' else 'cancelled'
        item['valid']=item['status']=='waiting' and batch_status=='generating'
    for tool in details.get('tools',[]):
        tool.setdefault('status','complete' if tool.get('ok') is True else 'failed' if tool.get('ok') is False else 'unknown')
        if batch_status!='generating' and tool['status']=='running':tool['status']='unknown'
        if tool.get('approval_id') in approvals_by_id:tool['approval']=approvals_by_id[tool['approval_id']]
    # Approval may arrive after the start packet; match the stable activity id.
    for approval in approvals_by_id.values():
        tool=next((t for t in details.get('tools',[]) if t.get('activity_id')==approval.get('activity_id')),None)
        if tool is not None:tool['approval']=approval
    return details
