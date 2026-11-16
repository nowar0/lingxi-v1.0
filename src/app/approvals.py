"""One-use approvals bound to the precise live execution snapshot."""
import asyncio
from dataclasses import dataclass,field
import hashlib
import json
from threading import RLock
import time
from uuid import uuid4

@dataclass
class Approval:
    id:str=field(default_factory=lambda:str(uuid4()))
    event:asyncio.Event=field(default_factory=asyncio.Event)
    allowed:bool|None=None
    snapshot:dict=field(default_factory=dict)
    fingerprint:str=''
    expires_at:float=field(default_factory=lambda:time.time()+120)
    loop:object=None

pending:dict[str,Approval]={}
_lock=RLock()

def create(snapshot=None):
    snapshot=snapshot or {}
    encoded=json.dumps(snapshot,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    if len(encoded.encode('utf-8'))>1_000_000:raise ValueError('工具确认参数超过容量，请拆分本次操作')
    item=Approval(snapshot=snapshot,fingerprint=hashlib.sha256(encoded.encode()).hexdigest())
    try:item.loop=asyncio.get_running_loop()
    except RuntimeError:pass
    with _lock:pending[item.id]=item
    return item

def decide(approval_id,allowed,expected_fingerprint=None):
    with _lock:
        item=pending.get(approval_id)
        if (item is None or item.allowed is not None or item.event.is_set() or item.expires_at<time.time()
                or item.snapshot and expected_fingerprint!=item.fingerprint):return False
        item.allowed=allowed
        try:current=asyncio.get_running_loop()
        except RuntimeError:current=None
        if item.loop and item.loop is not current:item.loop.call_soon_threadsafe(item.event.set)
        else:item.event.set()
        return True

def state(approval_id,fingerprint):
    with _lock:
        item=pending.get(approval_id)
        if not item or item.fingerprint!=fingerprint:return 'invalid'
        if item.allowed is not None:return 'approved' if item.allowed else 'denied'
        return 'expired' if item.expires_at<time.time() else 'waiting'
