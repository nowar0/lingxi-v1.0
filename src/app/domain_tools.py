"""App business operations: server-owned scope and revision-pinned approval."""
import asyncio
import hashlib
import json
import time
from uuid import uuid4
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, sessions
from app.db import connect
from app.tools.capabilities import spec
from app.tools.web_tools import host_only

ROUTES={'home':'/','companion':'/companion','work':'/work','tasks':'/tasks','memory':'/memory',
        'settings':'/settings',
        'skills':'/skills','connections':'/connections','models':'/settings?tab=models',
        'characters':'/settings?tab=characters','diagnostics':'/settings?tab=data&section=diagnostics'}


def init_table():
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS ui_presence(owner TEXT PRIMARY KEY,page TEXT NOT NULL,session_id INTEGER,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ui_commands(id TEXT PRIMARY KEY,owner TEXT NOT NULL,session_id INTEGER NOT NULL,
            action TEXT NOT NULL,target TEXT NOT NULL,status TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS domain_executions(id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT);''')
        db.execute("UPDATE domain_executions SET status='unknown' WHERE status='running'")


def scope(ctx):
    if ctx.get('agent_role') in ('worker','orchestrator') or not sessions.get_session(ctx.get('session_id')):
        raise HTTPException(403,'应用控制能力仅对当前聊天开放')
    sid=ctx['session_id']
    with connect() as db:
        row=db.execute('SELECT work_session_id FROM work_handoff_links WHERE origin_session_id=?',(sid,)).fetchone()
    return {sid,*([row['work_session_id']] if row else [])}


def ensure_work(ctx):
    from app import stage5
    allowed=scope(ctx)
    source=sessions.get_session(ctx['session_id'])
    if source['mode']=='work': return source['id']
    for sid in allowed:
        value=sessions.get_session(sid)
        if value and value['mode']=='work' and not value['archived']: return sid
    sid=stage5.create_session(stage5.SessionInput(mode='work',creation_id='domain-'+str(source['id']),title='来自聊天的工作'))['id']
    with connect() as db:
        db.execute('INSERT INTO work_handoff_links VALUES(?,?) ON CONFLICT(origin_session_id) DO UPDATE SET work_session_id=excluded.work_session_id',(source['id'],sid))
    return sid


def task(ctx,jid):
    from app import jobs
    state=jobs.snapshot(jid)
    if state['session_id'] not in scope(ctx): raise HTTPException(403,'任务不属于当前聊天及其关联工作会话')
    return state


def brief(state):
    return {'id':state['id'],'session_id':state['session_id'],'status':state['status'],'revision':state['revision'],
            'goal':state['document']['goal'],'acceptance':state['document']['acceptance'],
            'nodes':[{'id':node['id'],'title':node['document']['title'],'status':node['status'],
                      'result':(node.get('result') or '')[:600],'error':node.get('error')} for node in state['nodes']],
            'link':'#/tasks?job='+state['id']}


def prepare(name,args,ctx):
    from app import jobs, modules
    scope(ctx)
    frozen={'name':name,'args':args,'source_session_id':ctx['session_id']}
    if name in ('start_task','pause_task','cancel_task','retry_node','export_artifact'):
        state=task(ctx,args['job_id'])
        frozen['job']={key:state[key] for key in ('id','session_id','status','revision')}
        if name=='start_task':
            frozen['preflight']=jobs.preflight(state)
            frozen['configuration_fingerprint']=configuration_fingerprint(state['session_id'])
            frozen['cost_hint']='启动后每个节点及最终汇总会调用模型；外部写入仍须逐项确认'
            if frozen['preflight']['issues']: raise HTTPException(409,{'code':'needs_configuration','issues':frozen['preflight']['issues']})
        if name=='retry_node' and args['node_id'] not in {node['id'] for node in state['nodes']}:
            raise HTTPException(404,'此任务没有该节点')
        if name=='export_artifact':
            from app import workspace
            node=next((node for node in state['nodes'] if node['id']==args['node_id']),None)
            if not node or node['status']!='complete' or not node.get('result'): raise HTTPException(409,'节点没有可导出的已完成成果')
            selected=catalog.get('workspace',args['workspace_id'])
            if selected['session_id']!=state['session_id']: raise HTTPException(403,'导出目录不属于本任务')
            _,path=workspace.resolve(selected['id'],state['session_id'],args['path'])
            if path.exists() and (not path.is_file() or path.stat().st_size>2*1024*1024): raise HTTPException(422,'目标文件无法安全覆盖')
            frozen['destination']={'workspace_id':selected['id'],'revision':selected['revision'],'root':selected['path'],
                'path':args['path'],'expected_hash':hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'new',
                'content_hash':hashlib.sha256(node['result'].encode()).hexdigest(),'bytes':len(node['result'].encode()),
                'content_preview':node['result'][:800],'full_content_link':f"/v6/jobs/{state['id']}/artifacts/{node['id']}"}
    elif name=='bind_workspace':
        from pathlib import Path
        target=ensure_work(ctx);path=Path(args['path'])
        if not path.is_absolute() or not path.is_dir(): raise HTTPException(422,'绑定前需选择存在的绝对目录')
        wid=args.get('workspace_id') or 'chat_'+str(target)
        try:
            old=catalog.get('workspace',wid)
            if old['session_id']!=target: raise HTTPException(403,'目录编号已属于另一工作会话')
            revision=old['revision']
        except HTTPException as error:
            if error.status_code!=404: raise
            revision=0
        frozen['workspace']={'id':wid,'session_id':target,'path':str(path.resolve()),'expected_revision':revision}
    elif name=='set_module':
        if args['module_id'] not in modules.KINDS: raise HTTPException(422,'未知模态')
        selected=next(item for item in modules.modules()['items'] if item['id']==args['module_id'])
        profile=catalog.get('model-profile',args['profile_id'])
        if profile.get('deleted') or not profile.get('enabled'): raise HTTPException(409,'模型配置不可用')
        if modules.KINDS[args['module_id']] not in profile.get('capabilities',[]): raise HTTPException(422,'模型没有此模态能力')
        frozen['module']={'id':selected['id'],'revision':selected['revision'],'model':selected['model']}
        frozen['profile']={'id':profile['id'],'revision':profile['revision'],'name':profile['name'],'model':profile['model'],
                           'base_url':profile['base_url'],'protocol':profile.get('protocol','openai')}
        if profile.get('credential_ref'):
            from app.credential_generation import snapshot_for_reference,CredentialGenerationError
            try:frozen['profile']['credential_generation']=snapshot_for_reference(profile['credential_ref'])
            except CredentialGenerationError as error:raise HTTPException(409,str(error)) from error
    elif name=='open_ui':
        page=args['page']
        if page not in ROUTES: raise HTTPException(422,'不在允许的页面名单中')
        owner=ctx.get('ui_owner')
        with connect() as db:
            presence=db.execute('SELECT * FROM ui_presence WHERE owner=?',(owner,)).fetchone()
        if not owner or not presence or presence['session_id']!=ctx['session_id'] or time.time()-presence['at']>90: raise HTTPException(409,'没有当前聊天的活动界面，不能切换页面')
        frozen['ui']={'owner':owner,'target':ROUTES[page],'current':presence['page']}
    return frozen


def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def configuration_fingerprint(sid):
    """Pin revisions/routes/budget/credential metadata, never a Key or Key hash."""
    from app import session_options,extension_jobs,config,credential_generation
    from app.extensions import optional
    configured=[];references=set()
    for kind in ('module','model-profile','model-key'):
        for item in catalog.listing(kind):
            keys=('id','revision','enabled','base_url','model','protocol','credential_ref','local','capabilities','reasoning_effort','temperature','timeout')
            configured.append({key:item.get(key) for key in keys})
            if item.get('credential_ref'):references.add(item['credential_ref'])
    document=config.load_models()
    providers=[]
    for item in document.get('providers',[]):
        metadata={key:item.get(key) for key in ('id','base_url','protocol','temperature','timeout','supports_effort')}
        metadata['models']=[{key:model.get(key) for key in ('id','supports_effort')} for model in item.get('models',[])]
        providers.append(metadata)
        if item.get('id'):references.add(item['id'])
    generations={}
    for reference in references:
        try:generations[reference]=credential_generation.snapshot_for_reference(reference)
        except credential_generation.CredentialGenerationError:generations[reference]={'status':'not_registered'}
    return canonical({'configuration':configured,'providers':providers,'roles':document.get('roles'),'think':document.get('think'),
        'credential_metadata':generations,'session_options':session_options.get(sid),'preset':optional('agent-binding',str(sid)),
        'workspaces':[item for item in catalog.listing('workspace') if item['session_id']==sid],
        'budget':extension_jobs.settings(),'fallbacks':catalog.listing('provider-failover')})


async def execute(name,args,ctx):
    from app import evolution_api,jobs,modules,workspace,stage5
    if not evolution_api.settings()['domain_tools']: raise HTTPException(403,'应用控制工具尚未在设置中开启')
    allowed=scope(ctx)
    write_names={'bind_workspace','start_task','pause_task','cancel_task','retry_node','set_module','export_artifact','open_ui'}
    frozen=None; ticket=None
    if name in write_names:
        approved=ctx.get('domain_approval')
        if not approved or approved.get('name')!=name or approved.get('args')!=args or approved.get('expires',0)<time.time():
            raise HTTPException(403,'缺少本次具体操作的有效用户批准')
        frozen=prepare(name,args,ctx)
        if canonical(frozen)!=approved['fingerprint']: raise HTTPException(409,'批准后任务、模型或目录已变化；请重新预览')
        ticket=approved['id']
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM domain_executions WHERE id=?',(ticket,)).fetchone()
            if old:
                if old['status']=='complete': return json.loads(old['result'])
                raise HTTPException(409,'此前操作结果不确定，不能自动重放')
            db.execute("INSERT INTO domain_executions VALUES(?,'running',NULL)",(ticket,))
    try:
        if name=='get_app_state':
            with connect() as db:
                current=db.execute('SELECT page,at FROM ui_presence WHERE owner=?',(ctx.get('ui_owner'),)).fetchone()
            result={'session':sessions.get_session(ctx['session_id']),'current_page':current['page'] if current and time.time()-current['at']<90 else None,
                    'tasks':[brief(jobs.snapshot(row['id'])) for row in scoped_jobs(allowed)[:8]],
                    'modules':[public_module(item) for item in modules.modules()['items']]}
        elif name=='list_tasks': result={'items':[brief(jobs.snapshot(row['id'])) for row in scoped_jobs(allowed)[:30]]}
        elif name=='get_task': result=brief(task(ctx,args['job_id']))
        elif name=='list_workspaces': result={'items':[item for item in catalog.listing('workspace') if item['session_id'] in allowed]}
        elif name=='list_skills': result={'items':catalog.skill_metadata()}
        elif name=='get_module_settings': result={'items':[public_module(item) for item in modules.modules()['items']]}
        elif name=='create_work_session': result={'session_id':ensure_work(ctx),'status':'created','cost_hint':'未调用模型、未启动任务'}
        elif name=='create_task':
            target=ensure_work(ctx)
            definitions=json.loads(args['nodes_json'])
            request=jobs.Job(session_id=target,goal=args['goal'],acceptance=args['acceptance'],nodes=definitions)
            result=brief(jobs.create(request));result.update(cost_hint='仅创建等待启动的任务，未调用模型')
        elif name=='save_agent_preset':
            from app import extension_jobs
            identifier=uuid4().hex
            body=extension_jobs.AgentPreset(name=args['name'],brief=args.get('brief',''),model=args.get('model',''))
            extension_jobs.validate_preset(body)
            result=catalog.put('agent-preset',identifier,body.model_dump(exclude={'expected_revision'}),0)
            result={'id':result['id'],'status':'created','cost_hint':'尚未绑定或执行此预设'}
        elif name=='bind_workspace': result=workspace.register(frozen['workspace']['id'],workspace.Workspace(**{key:value for key,value in frozen['workspace'].items() if key!='id'}))
        elif name in ('start_task','pause_task','cancel_task','retry_node'):
            action={'start_task':'start','pause_task':'pause','cancel_task':'cancel','retry_node':'retry'}[name]
            result=brief(await jobs.action(args['job_id'],jobs.Action(action=action,expected_revision=frozen['job']['revision'],node_id=args.get('node_id',''))))
        elif name=='set_module':
            from app import model_profiles
            result=model_profiles.assign(args['module_id'],model_profiles.Assignment(profile_id=args['profile_id'],expected_revision=frozen['module']['revision']))
            result=public_module(result)
        elif name=='export_artifact':
            state=task(ctx,args['job_id']);node=next(node for node in state['nodes'] if node['id']==args['node_id'])
            if hashlib.sha256(node['result'].encode()).hexdigest()!=frozen['destination']['content_hash']: raise HTTPException(409,'成果内容已变化')
            result=workspace.write_file(args['workspace_id'],state['session_id'],args['path'],node['result'],frozen['destination']['expected_hash'])
            result.update(status='exported')
        elif name=='open_ui':
            identifier=uuid4().hex
            with connect() as db:
                db.execute("INSERT INTO ui_commands VALUES(?,?,?,'navigate',?,'pending',?)",(identifier,frozen['ui']['owner'],ctx['session_id'],frozen['ui']['target'],time.time()+30))
            result={'id':identifier,'status':'queued','link':'#'+frozen['ui']['target'],'cost_hint':'已排队切换页面，待活动界面确认'}
        else: raise HTTPException(422,'未登记的应用操作')
        result={'ok':True,**result}
        if ticket:
            with connect() as db: db.execute("UPDATE domain_executions SET status='complete',result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),ticket))
        return result
    except BaseException:
        if ticket:
            with connect() as db: db.execute("UPDATE domain_executions SET status='unknown' WHERE id=?",(ticket,))
        raise


def public_module(item):
    return {key:item.get(key) for key in ('id','revision','enabled','model','protocol','capabilities','reasoning_effort','temperature','connection_status','capability_status')}


def scoped_jobs(allowed):
    with connect() as db:
        return db.execute('SELECT id FROM work_jobs WHERE session_id IN ('+','.join('?' for _ in allowed)+') ORDER BY rowid DESC LIMIT 30',list(allowed)).fetchall()


class Presence(BaseModel):
    model_config=ConfigDict(extra='forbid')
    page:str=Field(max_length=200)
    session_id:int|None=None


@catalog.router.post('/ui/presence')
def presence(body:Presence,request:Request):
    from app.ui_auth import sessions as auth
    owner=auth.principal(request).owner_session_id
    if body.page not in ROUTES.values(): raise HTTPException(422,'未登记的界面地址')
    if body.session_id and not sessions.get_session(body.session_id): raise HTTPException(404,'会话不存在')
    with connect() as db:
        db.execute('INSERT INTO ui_presence VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE SET page=excluded.page,session_id=excluded.session_id,at=excluded.at',
                   (owner,body.page,body.session_id,time.time()))
    return {'ok':True}


@catalog.router.get('/ui/commands')
def commands(request:Request):
    from app.ui_auth import sessions as auth
    owner=auth.principal(request).owner_session_id
    with connect() as db:
        db.execute("UPDATE ui_commands SET status='expired' WHERE status='pending' AND expires<?",(time.time(),))
        rows=db.execute("SELECT id,action,target,session_id FROM ui_commands WHERE owner=? AND status='pending' AND expires>? LIMIT 10",(owner,time.time())).fetchall()
    return {'items':[dict(row) for row in rows]}


@catalog.router.post('/ui/commands/{identifier}/ack')
def acknowledge(identifier:str,request:Request):
    from app.ui_auth import sessions as auth
    owner=auth.principal(request).owner_session_id
    with connect() as db:
        changed=db.execute("UPDATE ui_commands SET status='delivered' WHERE id=? AND owner=? AND status='pending' AND expires>?",(identifier,owner,time.time())).rowcount
    if not changed: raise HTTPException(409,'界面动作已失效')
    return {'ok':True}


def definition(name,description,fields,required=None,write=False):
    return {'spec':spec(name,description,{key:{'type':'string'} for key in fields},required if required is not None else fields),
            'run':host_only,'write':write,'timeout':30}


TOOLS=[
 definition('get_app_state','读取当前会话、活动页面、配置状态及关联任务，不读取Key',[]),
 definition('list_tasks','列出当前聊天及关联工作会话的任务',[]),
 definition('get_task','按编号查询当前聊天可见任务，不读取其他会话任务',['job_id']),
 definition('list_workspaces','列出当前聊天已授权的工作目录',[]),
 definition('list_skills','只读技能名称用途目录，不读取正文',[]),
 definition('get_module_settings','只读模态设置与实际验证状态，不含凭据',[]),
 definition('create_work_session','建立或复用关联工作会话，不运行、不调用模型',[]),
 definition('create_task','根据已给出的节点JSON创建等待启动的任务；不拆分、不启动、不花费模型调用',['goal','acceptance','nodes_json']),
 definition('save_agent_preset','新建未绑定的工作预设，不执行任务',['name','brief','model'],['name']),
 definition('bind_workspace','预览并经用户确认绑定目录，模型无法自行授权',['path','workspace_id'],['path'],True),
 definition('start_task','预览最新任务、配置与调用估算，经用户确认后启动',['job_id'],write=True),
 definition('pause_task','经用户独立确认暂停当前任务',['job_id'],write=True),
 definition('cancel_task','经用户独立确认取消任务，已产出成果保留',['job_id'],write=True),
 definition('retry_node','经用户确认重试失败节点；不自动重放未知外部写入',['job_id','node_id'],write=True),
 definition('set_module','经用户确认将已存在的模型配置分配至模态；不修改Key',['module_id','profile_id'],write=True),
 definition('export_artifact','预览完成节点的成果指纹、完整下载链接与目标路径，经确认后写到已绑定目录',['job_id','node_id','workspace_id','path'],write=True),
 definition('open_ui','经用户确认切换活动界面白名单页面；不接受JS或任意地址',['page'],write=True),
]
