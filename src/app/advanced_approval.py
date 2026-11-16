"""Single-use previews for explicit human operations in the workbench."""
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import catalog, sessions


class Commit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    token: str = Field(pattern=r'^[a-f0-9]{32}$')


@dataclass(frozen=True)
class Preview:
    kind: str
    session_id: int
    state: object
    action: object
    expires: float


_pending = OrderedDict()
_lock = Lock()


def require_work(session_id):
    session = sessions.get_session(session_id)
    if not session or session.get('archived') or session['mode'] != 'work':
        raise HTTPException(409, '请选择未归档的工作会话')
    return session


def workspace(session_id):
    from app.workspace_ui import binding
    require_work(session_id)
    return catalog.get('workspace', binding(session_id))


def prepare(kind, session_id, state, action, details):
    require_work(session_id)
    token = uuid4().hex
    now = time.monotonic()
    with _lock:
        while _pending and (len(_pending) >= 128 or next(iter(_pending.values())).expires < now):
            _pending.popitem(last=False)
        _pending[token] = Preview(kind, session_id, state, action, now + 120)
    return {'token': token, 'expires_in': 120, 'details': details}


def take(kind, body, state):
    require_work(body.session_id)
    with _lock:
        preview = _pending.get(body.token)
        if preview and (preview.kind, preview.session_id) == (kind, body.session_id):
            del _pending[body.token]
        else:
            preview = None
    if not preview or preview.expires < time.monotonic() or preview.state != state:
        raise HTTPException(409, '确认已使用、超时或目标发生变化，请重新预览')
    return preview.action
