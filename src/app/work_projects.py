"""Folder groups for work conversations; metadata never deletes a folder or history."""
import hashlib
import json
import os
import re
from threading import RLock
_cache_lock=RLock()
_rail_cache=None
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, sessions, stage5, work_settings
from app.db import connect


def project_id(path):
    return hashlib.sha256(os.path.normcase(os.path.normpath(path)).encode()).hexdigest()[:24]


def project(path):
    return {'id': project_id(path), 'path': path, 'name': Path(path).name or path,
            'pinned': False, 'revision': 0}


def init_table():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS work_rail_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
        db.execute('INSERT OR IGNORE INTO work_rail_revision VALUES(1,0)')
        for table in ('sessions','capability_documents','messages'):
            for operation in ('INSERT','UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS rail_revision_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN UPDATE work_rail_revision SET revision=revision+1 WHERE id=1; END')


def rail(offset=0,limit=None,q=''):
    global _rail_cache
    with connect() as db:
        from app import db as db_module
        row=db.execute("SELECT revision FROM work_rail_revision WHERE id=1").fetchone()
        signature=(str(db_module.DB_PATH),row[0])
        with _cache_lock:
            cached=_rail_cache
        if cached and cached[0]==signature:
            items,owners=cached[1],cached[2]
            if q:items=[item for item in items if q.casefold() in (item['title']+' '+item['project_name']+' '+item['project_path']).casefold()]
            total=len(items)
            return {'sessions':items[offset:offset+limit] if limit else items,'projects':owners,'total':total,'next_offset':offset+limit if limit and offset+limit<total else None}
        rows = db.execute("SELECT *,EXISTS(SELECT 1 FROM messages m WHERE m.session_id=sessions.id AND m.role='user' AND m.archived=0) AS has_messages FROM sessions WHERE mode='work' AND archived=0 ORDER BY updated_at DESC,id DESC").fetchall()
        docs = db.execute("SELECT * FROM capability_documents WHERE kind IN ('work-project','work-settings','work-session-rail')").fetchall()
    settings, projects, flags = {}, {}, {}
    for row in docs:
        value = {**json.loads(row['document']), 'id': row['id'], 'revision': row['revision']}
        if row['kind'] == 'work-settings': settings[row['id']] = value
        elif row['kind'] == 'work-project': projects[row['id']] = value
        else: flags[row['id']] = value
    items = []
    for row in rows:
        item = dict(row); path = settings.get(str(row['id']), {}).get('path', '')
        owner = None
        if path:
            inferred = project(path); owner = projects.setdefault(inferred['id'], inferred)
        flag = flags.get(str(row['id']), {})
        if not item.pop('has_messages'): continue
        items.append({**item, 'project_id': owner['id'] if owner and not owner.get('deleted') else None,
                      'project_name': owner['name'] if owner else '', 'project_path': path,
                      'unread': int(flag.get('unread', False)), 'pinned': flag.get('pinned', False), 'rail_revision': flag.get('revision', 0)})
    owners=sorted((p for p in projects.values() if not p.get('deleted')),key=lambda p:(not p.get('pinned'),p['name'].casefold(),p['id']))
    with _cache_lock:_rail_cache=(signature,items,owners)
    if q:items=[item for item in items if q.casefold() in (item['title']+' '+item['project_name']+' '+item['project_path']).casefold()]
    total=len(items)
    page=items[offset:offset+limit] if limit else items
    return {'next_offset':offset+limit if limit and offset+limit<total else None,'total':total,'sessions': page, 'projects': sorted((p for p in projects.values() if not p.get('deleted')),
            key=lambda p: (not p.get('pinned'), p['name'].casefold(), p['id']))}


@catalog.router.get('/work-rail')
def read_rail(offset:int=0,limit:int=100,q:str=''):
    if offset<0 or not 1<=limit<=200 or len(q)>500:raise HTTPException(422,'会话分页无效')
    return rail(offset,limit,q)


class AddProject(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=2000)


@catalog.router.post('/work-projects')
def add_project(body: AddProject):
    folder = work_settings._validated_folder(body.path)
    value = project(folder['path'])
    with catalog.transaction() as db:
        row = db.execute("SELECT * FROM capability_documents WHERE kind='work-project' AND id=?", (value['id'],)).fetchone()
        if row:
            old = {**json.loads(row['document']), 'id': row['id'], 'revision': row['revision']}
            if not old.get('deleted'): return old
            value = {**old, 'deleted': False}
        return catalog.put_in_transaction(db, 'work-project', value['id'],
            {k: v for k, v in value.items() if k not in ('id','revision')}, row['revision'] if row else 0)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    pinned: bool = False
    removed: bool = False
    expected_revision: int = Field(ge=0)


def find_project(pid):
    value = next((p for p in rail()['projects'] if p['id'] == pid), None)
    if not value: raise HTTPException(404, '工作区不存在或已移除')
    return value


@catalog.router.put('/work-projects/{pid}')
def edit_project(pid: str, body: ProjectPatch):
    value = find_project(pid)
    if not body.name.strip(): raise HTTPException(422, '工作区名称不能为空')
    return catalog.put('work-project', pid, {'path': value['path'], 'name': body.name.strip(),
        'pinned': body.pinned, 'deleted': body.removed}, body.expected_revision)


class NewChat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    creation_id: UUID


@catalog.router.post('/work-projects/{pid}/sessions')
def create_project_session(pid: str, body: NewChat):
    value = find_project(pid)
    path = work_settings._validated_folder(value['path'])['path']
    key = str(body.creation_id)
    with catalog.transaction() as db:
        previous = db.execute("SELECT document FROM capability_documents WHERE kind='work-project-create' AND id=?", (key,)).fetchone()
        if previous:
            old = json.loads(previous['document'])
            if old['project_id'] != pid: raise HTTPException(409, '创建编号已用于另一工作区')
            item = db.execute('SELECT * FROM sessions WHERE id=?', (old['session_id'],)).fetchone()
            if not item or item['archived']: raise HTTPException(409, '原会话已归档或移除，请使用新编号')
            return dict(item)
        if db.execute('SELECT 1 FROM session_creations WHERE creation_id=?', (key,)).fetchone():
            raise HTTPException(409, '创建编号已用于其他会话')
        sid = db.execute("INSERT INTO sessions(title,mode,created_at,updated_at) VALUES(?,'work',?,?)",
                         ('新的工作会话', stage5.now(), stage5.now())).lastrowid
        db.execute("INSERT INTO session_creations VALUES(?,'work',?)", (key, sid))
        catalog.put_in_transaction(db, 'work-settings', str(sid), {'path':path,'acceptance':''}, 0)
        catalog.put_in_transaction(db, 'workspace', 'session_'+str(sid), {'session_id':sid,'path':path}, 0)
        catalog.put_in_transaction(db, 'work-project-create', key, {'project_id':pid,'session_id':sid}, 0)
        return dict(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())


class BindProject(BaseModel):
    expected_revision: int = Field(ge=0)


@catalog.router.put('/work-projects/{pid}/sessions/{sid}')
def bind_project(pid: str, sid: int, body: BindProject):
    value = find_project(pid)
    current = work_settings.get(sid)
    return work_settings.put(sid, work_settings.Settings(path=value['path'], acceptance=current['acceptance'], expected_revision=body.expected_revision))


class PinSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pinned: bool
    expected_revision: int = Field(ge=0)


@catalog.router.put('/work-sessions/{sid}/rail')
def pin_session(sid: int, body: PinSession):
    value = sessions.get_session(sid)
    if not value or value['mode'] != 'work' or value['archived']: raise HTTPException(404, '工作会话不存在')
    with catalog.transaction():
        prior = next((v for v in catalog.listing('work-session-rail') if v['id'] == str(sid)), {})
        return catalog.put('work-session-rail', str(sid), {**{k:v for k,v in prior.items() if k not in ('id','revision')}, 'session_id':sid, 'pinned':body.pinned}, body.expected_revision)


def name_first_question(db, sid, text):
    item = db.execute('SELECT mode,title FROM sessions WHERE id=?', (sid,)).fetchone()
    if not item or item['mode'] != 'work' or item['title'] not in ('新会话','新的工作会话','新工作会话','新聊天'): return
    if db.execute("SELECT 1 FROM capability_documents WHERE kind='manual-session-title' AND id=?", (str(sid),)).fetchone(): return
    if db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user'", (sid,)).fetchone()[0] != 1: return
    title = re.sub(r'\s+', ' ', text).strip()
    title = re.sub(r'^[#>*\s]+', '', title)
    if title:
        db.execute('UPDATE sessions SET title=? WHERE id=?', (title[:47]+'…' if len(title)>48 else title, sid))


class ReadSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unread: bool
    expected_revision: int = Field(ge=0)

@catalog.router.put('/work-sessions/{sid}/read-state')
def read_state(sid:int, body:ReadSession):
    item=sessions.get_session(sid)
    if not item or item['mode']!='work' or item['archived']:raise HTTPException(404,'工作会话不存在')
    with catalog.transaction():
        prior=next((v for v in catalog.listing('work-session-rail') if v['id']==str(sid)),{})
        return catalog.put('work-session-rail',str(sid),{**{k:v for k,v in prior.items() if k not in ('id','revision')},'session_id':sid,'unread':body.unread},body.expected_revision)

class ForkRequest(BaseModel):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_version: int = Field(ge=0)

@catalog.router.post('/work-sessions/{sid}/fork')
def fork_current(sid:int,body:ForkRequest):
    from app.workspace_ui import create_side_chat,SideChatCreate
    return create_side_chat(SideChatCreate(parent_session_id=sid,request_id=body.request_id,expected_version=body.expected_version,kind='fork'))
