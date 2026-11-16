"""Customer-facing management; explicit scope, revision checks and inert previews."""
import json
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app import catalog, sessions
from app.db import connect

router = APIRouter(prefix='/v6/manage')

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class ConversationDefaults(Strict):
    auto_compact: bool = True
    proactive_compact: bool = True
    auto_ratio: float = Field(default=.85, ge=.6, le=.95)
    proactive_ratio: float = Field(default=.65, ge=.3, le=.85)
    keep_recent_turns: int = Field(default=2, ge=1, le=10)
    summary_model: str = Field(default='', max_length=500)
    auto_read: bool = False
    voice_engine: Literal['','gpt-sovits-v2','cosyvoice2','indextts2','indextts2.5'] = ''

    @model_validator(mode='after')
    def thresholds(self):
        if self.proactive_ratio >= self.auto_ratio: raise ValueError('提前压缩阈值应低于自动压缩阈值')
        return self

class Defaults(Strict):
    companion: ConversationDefaults = Field(default_factory=ConversationDefaults)
    work: ConversationDefaults = Field(default_factory=ConversationDefaults)
    expected_revision: int = Field(default=0, ge=0)

def defaults():
    from app.extensions import optional
    return {**Defaults().model_dump(exclude={'expected_revision'}), **optional('customer-preferences','conversations'), 'revision': optional('customer-preferences','conversations').get('revision',0)}

def conversation_defaults(mode):
    return defaults().get(mode, {})

@router.get('/conversations')
def read_defaults(): return defaults()

@router.put('/conversations')
def save_defaults(body: Defaults):
    from app.llm import router as models
    for item in (body.companion,body.work):
        if item.summary_model:
            if item.summary_model not in set(models.list_models())|set(models.list_roles()): raise HTTPException(422,'请选择可用的摘要模型')
            models.resolve(item.summary_model)
    return catalog.put('customer-preferences','conversations',body.model_dump(exclude={'expected_revision'}),body.expected_revision)

@router.get('/sessions')
def listing(mode: Literal['all','work','companion']='all', archived: bool=True, q: str=Query('',max_length=200), offset: int=Query(0,ge=0), limit: int=Query(50,ge=1,le=100)):
    with connect() as db:
        where='archived=?'; args=[int(archived)]
        if mode!='all': where+=' AND mode=?';args.append(mode)
        where+=' AND instr(lower(title),lower(?))>0';args.append(q)
        rows=[dict(r) for r in db.execute(f'SELECT * FROM sessions WHERE {where} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?',[*args,limit+1,offset])]
    workspaces={w['session_id']:w['path'] for w in catalog.listing('workspace')}
    return {'items':[{**r,'project_path':workspaces.get(r['id'],'')} for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None}

class SessionChange(Strict):
    action: Literal['archive','restore','delete']
    confirmed: bool = False

@router.post('/sessions/{sid}')
def change_session(sid:int, body:SessionChange):
    from app import session_lifecycle,stage5
    with catalog.transaction() as db:
        if not sessions.get_session(sid):raise HTTPException(404,'会话不存在')
        session_lifecycle.guard_idle(db,sid)
        if body.action=='delete':
            if not body.confirmed:raise HTTPException(422,'请确认永久删除；此操作不能撤销')
            session_lifecycle.remove(db,sid)
        else:
            db.execute('UPDATE sessions SET archived=?,updated_at=? WHERE id=?',(int(body.action=='archive'),stage5.now(),sid))
    return {'ok':True}

@router.get('/schedules')
def schedule_list():
    from app import extension_jobs
    rows=catalog.listing('companion-schedule')
    return {'reminders':rows,'work':extension_jobs.schedules()['items']}

class ScheduleEdit(Strict):
    content: str = Field(default='',max_length=2000)
    cron: str = Field(max_length=100)
    enabled: bool
    daily_token_limit: int = Field(default=0,ge=0,le=1000000)
    confirmed: bool = False
    expected_revision: int = Field(ge=1)

@router.put('/schedules/{kind}/{identifier}')
def edit_schedule(kind:Literal['reminder','work'], identifier:str, body:ScheduleEdit):
    from app import reminders,extension_jobs
    key='companion-schedule' if kind=='reminder' else 'task-schedule'
    with catalog.transaction():
        old=catalog.get(key,identifier)
        if old.get('deleted') or old['revision']!=body.expected_revision:raise HTTPException(409,'计划已变化，请刷新')
        if kind=='reminder':
            payload={k:old[k] for k in reminders.Schedule.model_fields if k in old}
            payload.update(content=body.content,cron=body.cron,enabled=body.enabled,daily_token_limit=body.daily_token_limit,expected_revision=body.expected_revision)
            return reminders.save(identifier,reminders.Schedule(**payload))
        if body.enabled and not body.confirmed:raise HTTPException(422,'请确认允许按已保存计划自动启动')
        if body.enabled and not extension_jobs.settings()['daily_tokens']:raise HTTPException(422,'请先设置任务每日预算')
        sid=old['plan']['session_id']
        if body.enabled and old.get('context_hash')!=extension_jobs.context_hash(sid):raise HTTPException(409,'工作区或预设已变化，请在原任务中重新保存计划')
        try:due=extension_jobs.next_due(body.cron)
        except (ValueError,OverflowError):raise HTTPException(422,'时间规则无效') from None
        value={k:v for k,v in old.items() if k not in ('id','revision')}
        value.update(cron=body.cron,enabled=body.enabled,next_due=due)
        return catalog.put(key,identifier,value,body.expected_revision)

class Delete(Strict):
    expected_revision: int = Field(ge=1)
    confirmed: bool = False

@router.post('/schedules/{kind}/{identifier}/delete')
def delete_schedule(kind:Literal['reminder','work'],identifier:str,body:Delete):
    key='companion-schedule' if kind=='reminder' else 'task-schedule'
    table='companion_schedule_runs' if kind=='reminder' else 'extension_schedule_runs'
    if not body.confirmed:raise HTTPException(422,'请确认移除计划；已有消息和执行记录保留')
    with catalog.transaction() as db:
        old=catalog.get(key,identifier)
        if db.execute(f"SELECT 1 FROM {table} WHERE schedule_id=? AND status='launching'",(identifier,)).fetchone():raise HTTPException(409,'计划正在触发，请稍后再移除')
        return catalog.put(key,identifier,{**old,'deleted':True,'enabled':False},body.expected_revision)

@router.post('/schedule-preview')
def preview_schedule(body:dict):
    from app.extension_jobs import next_due
    try:return {'next_due':next_due(str(body.get('cron','')))}
    except (ValueError,OverflowError):raise HTTPException(422,'请选择有效的时间规则') from None

@router.post('/presets/{pid}/delete')
def delete_preset(pid:str,body:Delete):
    if not body.confirmed:raise HTTPException(422,'请确认删除预设；已绑定会话保留原快照')
    with catalog.transaction():
        old=catalog.get('agent-preset',pid)
        return catalog.put('agent-preset',pid,{**old,'deleted':True},body.expected_revision)

@router.get('/characters')
def characters(archived:bool=False):
    with connect() as db:return {'items':[{**json.loads(r['document']),'archived':bool(r['archived'])} for r in db.execute('SELECT * FROM character_cards WHERE archived=?',(int(archived),))]}

@router.post('/characters/{cid}')
def character_action(cid:str,body:SessionChange):
    from app import stage5
    if cid=='lingxi':raise HTTPException(409,'默认零汐卡需要保留')
    with catalog.transaction() as db:
        from app.model_references import assert_idle
        assert_idle()
        old=db.execute('SELECT * FROM character_cards WHERE id=?',(cid,)).fetchone()
        if not old:raise HTTPException(404,'人物卡不存在')
        if body.action=='delete':
            if not body.confirmed:raise HTTPException(422,'请确认永久删除人物卡')
            if db.execute('SELECT 1 FROM messages WHERE instr(speaker_snapshot,?)>0',(cid,)).fetchone():raise HTTPException(409,'人物仍有关联聊天，请先归档保留历史')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'memory_v2_sources' in tables and db.execute('SELECT 1 FROM memory_v2_sources WHERE character_id=?',(cid,)).fetchone():raise HTTPException(409,'人物仍关联记忆来源，请归档保留')
            if 'fact_versions' in tables and db.execute("SELECT 1 FROM fact_versions WHERE json_extract(source,'$.character_id')=?",(cid,)).fetchone():raise HTTPException(409,'人物仍关联记忆来源，请归档保留')
            db.execute('DELETE FROM character_versions WHERE id=?',(cid,))
            db.execute('DELETE FROM character_cards WHERE id=?',(cid,))
        else:db.execute('UPDATE character_cards SET archived=? WHERE id=?',(int(body.action=='archive'),cid))
        ui=stage5.ui_state()
        if ui['active_character_id']==cid and body.action!='restore':
            row=db.execute('SELECT document,revision FROM stage5_state WHERE id=1').fetchone();value=json.loads(row['document']);value['active_character_id']='lingxi'
            db.execute('UPDATE stage5_state SET document=?,revision=revision+1 WHERE id=1',(json.dumps(value,ensure_ascii=False),))
    return {'ok':True}

@router.get('/model-usage/{pid}')
def model_usage(pid:str):
    from app import model_references
    item=catalog.get('model-profile',pid)
    route='profile_'+pid+'/'+item['model']
    modules=[m['id'] for m in catalog.listing('module') if m.get('profile_id')==pid]
    return {'modules':modules,'references':model_references.references({route},[pid])}

class Decay(Strict):
    short_days: float = Field(default=1,ge=.1,le=3650)
    long_days: float = Field(default=30,ge=1,le=36500)
    important_days: float = Field(default=180,ge=1,le=36500)
    important_threshold: float = Field(default=.7,ge=0,le=1)
    expected_revision: int = Field(default=0,ge=0)

@router.get('/memory-decay')
def decay_settings():
    with connect() as db:
        row=db.execute("SELECT value FROM memory_v2_meta WHERE key='decay-settings'").fetchone()
    return json.loads(row[0]) if row else {**Decay().model_dump(exclude={'expected_revision'}),'revision':0}

@router.put('/memory-decay')
def save_decay(body:Decay):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT value FROM memory_v2_meta WHERE key='decay-settings'").fetchone()
        revision=json.loads(row[0])['revision'] if row else 0
        if body.expected_revision!=revision:raise HTTPException(409,'记忆规则已变化，请刷新')
        value={**body.model_dump(exclude={'expected_revision'}),'revision':revision+1}
        db.execute("INSERT OR REPLACE INTO memory_v2_meta VALUES('decay-settings',?)",(json.dumps(value),))
    return value

@router.get('/project-docs/{sid}')
def docs_storage(sid:int):
    from app import project_docs,config
    project_docs.settings(sid)
    with project_docs.connect() as db:
        count=db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM project_docs WHERE session_id=?',(sid,)).fetchone()
        vectors=db.execute('SELECT COALESCE(SUM(length(vector)),0) FROM project_embeddings WHERE session_id=?',(sid,)).fetchone()[0]
    path=config.DATA_DIR/'project-docs.sqlite3'
    return {'documents':count[0],'source_bytes':count[1],'vector_bytes':vectors,'shared_database_bytes':path.stat().st_size if path.exists() else 0}

class DocsAction(Strict):
    action:Literal['cancel','clear']
    confirmed:bool=False

@router.post('/project-docs/{sid}')
def docs_action(sid:int,body:DocsAction):
    from app import project_docs
    project_docs.settings(sid)
    active=[v for v in project_docs._jobs.values() if v['session_id']==sid and v['status'] in ('queued','indexing')]
    for state in active:project_docs._cancel_indices[state['id']].set()
    if body.action=='clear':
        if not body.confirmed:raise HTTPException(422,'请确认清空当前会话索引；原文件保留')
        if active:raise HTTPException(409,'正在停止索引，请稍后清空')
        with project_docs.connect() as db:db.execute('DELETE FROM project_docs WHERE session_id=?',(sid,))
    return {'ok':True,'stopping':bool(active)}

@router.get('/performance/{sid}')
def performance(sid:int):
    if not sessions.get_session(sid):raise HTTPException(404,'会话不存在')
    with connect() as db:
        rows=[dict(r) for r in db.execute("SELECT duration_ms,first_token_ms,generation_ms,usage FROM model_requests WHERE session_id=? AND status='complete'",(sid,))]
    timings=[r['first_token_ms'] for r in rows if r['first_token_ms'] is not None]
    rates=[json.loads(r['usage']).get('输出')/max(.001,r['generation_ms']/1000) for r in rows if r['generation_ms'] is not None and json.loads(r['usage']).get('输出') is not None]
    return {'calls':len(rows),'model_ms':sum(r['duration_ms'] for r in rows) if rows else None,'ttft_ms':sum(timings)/len(timings) if timings else None,'tps':sum(rates)/len(rates) if rates else None}

@router.get('/export-range/{sid}')
def export_range(sid:int):
    if not sessions.get_session(sid):raise HTTPException(404,'会话不存在')
    with connect() as db:
        row=db.execute('SELECT COUNT(*),MIN(id),MAX(id) FROM messages WHERE session_id=? AND archived=0',(sid,)).fetchone()
    return {'count':row[0],'first_id':row[1],'last_id':row[2]}

from app.generation_settings import Settings as GenerationSettings

@router.get('/generation/{kind}/{key}')
def read_generation(kind:str,key:str):
    from app import generation_settings
    return generation_settings.read(kind,key)

@router.put('/generation/{kind}/{key}')
def save_generation(kind:str,key:str,body:GenerationSettings):
    from app import generation_settings
    return generation_settings.save(kind,key,body)

from app.package_cleanup import Selection as PackageSelection,Confirm as PackageConfirm

@router.post('/packages/{identifier}/preview')
def package_preview(identifier:str,body:PackageSelection):
    from app import package_cleanup
    return package_cleanup.preview(identifier,body)

@router.post('/packages/confirm')
def package_confirm(body:PackageConfirm):
    from app import package_cleanup
    return package_cleanup.confirm(body)

@router.get('/package-runtime')
def package_runtime():
    from app import model_packages
    version=model_packages.catalog_data()['runtime']['version']
    return {'version':version,'installed':(model_packages.root()/'speech-runtime'/version).is_dir()}

class BrowserPreferences(Strict):
    browser:dict
    expected_revision:str|None=None

@router.put('/browser-preferences')
def save_browser_preferences(body:BrowserPreferences):
    import hashlib,os
    from app import config,data_management
    from uuid import uuid4
    allowed={'lingxi-next-theme-v1','lingxi-message-theme-v1','lingxi-effort-slider-v1','lingxi-pinned-companion','lingxi-pinned-work','lingxi-sidebar-layout-v1','lingxi-work-rail-open','lingxi-work-rail-width','lingxi-work-tool-width','lingxi-work-projects-collapsed','lingxi-companion-rail:v1'}
    if body.browser.get('format')!='lingxi-browser-preferences-v1':raise HTTPException(422,'界面偏好格式无效')
    entries=body.browser.get('entries',{})
    if not isinstance(entries,dict) or not set(entries)<=allowed or any(not isinstance(v,str) or len(v)>30000 for v in entries.values()):raise HTTPException(422,'界面偏好内容无效')
    for v in entries.values():
        try:json.loads(v)
        except ValueError:raise HTTPException(422,'界面偏好内容无效') from None
    from app.browser_preferences import lock
    with lock:
        path=data_management.assert_no_links(config.DATA_DIR/'browser-preferences.json')
        raw=path.read_bytes() if path.is_file() else None
        revision=hashlib.sha256(raw).hexdigest() if raw is not None else None
        if revision!=body.expected_revision:raise HTTPException(409,'偏好在其他窗口更新，请重新打开后设置')
        doc=json.loads(raw) if raw else {};doc['browser']=body.browser
        value=json.dumps(doc,ensure_ascii=False).encode()
        if len(value)>200000:raise HTTPException(422,'偏好内容过大')
        temp=path.with_name('.browser-preferences-'+uuid4().hex+'.tmp');temp.write_bytes(value);os.replace(temp,path)
        return {'revision':hashlib.sha256(value).hexdigest()}


class UnifiedModelSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    configuration:dict
    generation:dict


@catalog.router.put('/manage/model-settings/{kind}/{key}')
def save_unified_model(kind:str,key:str,body:UnifiedModelSettings):
    from app import credential_lifecycle,model_profiles,modules,generation_settings
    from pydantic import ValidationError
    try:
        generation=generation_settings.Settings(**{**body.generation,'expected_revision':0})
        configuration=model_profiles.ProfileUpdate(**body.configuration) if kind=='profile' else modules.Module(**body.configuration)
    except ValidationError:raise HTTPException(422,'模型参数无效，请检查数值和必填项') from None
    with credential_lifecycle.mutation():
        if kind=='profile':
            result=model_profiles.update_configuration(key,configuration,generation_override=generation.model_dump(exclude={'expected_revision'}))
            current=catalog.get('model-profile',key)
        elif kind=='module':
            result=modules.save_module(key,configuration)
            current=catalog.get('module',key)
        else:raise HTTPException(404,'配置不存在')
        values={**body.generation,'expected_revision':current['revision']}
        generation_settings.save(kind,key,generation_settings.Settings(**values))
    return {'ok':True}
