"""Previewed uninstall of a registered model version, never arbitrary paths."""
import hashlib,json,secrets,shutil,time
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import catalog,model_packages as packages,ui_auth

_previews={}
class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:str=Field(min_length=1,max_length=100)
class Confirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token:str=Field(min_length=20,max_length=200)
    confirmed:bool

def owner():return ui_auth.current_principal.get().owner_session_id

def target(identifier,version):
    if identifier=='speech-runtime':
        if version!=packages.catalog_data()['runtime']['version']:raise HTTPException(422,'运行环境版本无效')
        return packages.contained(packages.root(),'speech-runtime/'+version)
    packages.known_version(identifier,version)
    return packages.contained(packages.root(),identifier+'/'+version)

def guard(identifier,version):
    from app.speech.registry import engines
    from app.model_references import assert_idle
    assert_idle()
    if any(v['status'] in ('queued','downloading','preparing_runtime') for v in packages._jobs.values()):raise HTTPException(409,'有模型正在下载，请先暂停')
    if identifier=='speech-runtime':
        if any(p['runtime_ready'] and p['installed'] and p.get('requires_runtime') for p in packages.listing()['items']):raise HTTPException(409,'仍有模型依赖运行环境，请先卸载这些模型')
    else:
        active=packages.status(identifier)['active_version']==version
        if active:
            if any(m.get('enabled') and m.get('model')==identifier for m in catalog.listing('module')):raise HTTPException(409,'模型仍已启用，请先更换功能分配或停用')
            if any(v.get('settings',{}).get('voice_engine')==identifier or v.get('voice_engine')==identifier for v in catalog.listing('session-options')):raise HTTPException(409,'仍有会话使用此语音引擎，请先更换')
            if any(v.get('embedding_model')==identifier and v.get('enabled') for v in catalog.listing('project-docs')):raise HTTPException(409,'项目资料库仍使用此模型，请先更换')
    if any(v.get('ready') or v.get('loading') for v in engines()['items'] if identifier=='speech-runtime' or v['engine']==identifier):raise HTTPException(409,'请先停止并释放对应语音引擎')

def snapshot(directory):
    if not directory.is_dir():raise HTTPException(404,'此版本未安装')
    files=[]
    for path in sorted(directory.rglob('*')):
        if path.is_symlink() or path.is_junction():raise HTTPException(409,'目录含链接，请保留并手动核对')
        if path.is_file():
            stat=path.stat();files.append({'path':path.relative_to(directory).as_posix(),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns})
    return files

def preview(identifier,body):
    guard(identifier,body.version);directory=target(identifier,body.version);files=snapshot(directory)
    for expired,value in list(_previews.items()):
        if value['expires']<time.monotonic():_previews.pop(expired,None)
    if len(_previews)>=128:raise HTTPException(409,'待确认预览过多，请稍后再试')
    token=secrets.token_urlsafe(32);_previews[token]={'id':identifier,'version':body.version,'files':files,'owner':owner(),'expires':time.monotonic()+300,'revision':packages.pointers()['revision']}
    return {'token':token,'files':len(files),'bytes':sum(f['bytes'] for f in files),'version':body.version,'paths':[f['path'] for f in files],'runtime':identifier=='speech-runtime'}

def confirm(body):
    value=_previews.get(body.token)
    if not body.confirmed or not value or value['owner']!=owner() or value['expires']<time.monotonic():raise HTTPException(409,'卸载确认已失效，请重新预览')
    with packages._pointers_lock:
        guard(value['id'],value['version']);directory=target(value['id'],value['version'])
        if snapshot(directory)!=value['files'] or packages.pointers()['revision']!=value['revision']:raise HTTPException(409,'模型文件或版本已变化，请重新预览')
        if not directory.resolve().is_relative_to(packages.root().resolve()) or directory.resolve()==packages.root().resolve():raise HTTPException(409,'目录校核失败')
        # Detach the known version first; a locked file leaves a removable inactive version.
        current=packages.pointers();active=current['items'].get(value['id'],{})
        if active.get('version')==value['version']:current['items'].pop(value['id'],None)
        elif active.get('previous')==value['version']:active['previous']=None
        current['revision']+=1;packages.atomic_json(packages.root()/'active.json',current)
        try:shutil.rmtree(directory)
        except OSError:raise HTTPException(409,'文件仍被占用，已停用此版本；释放后可重新预览卸载') from None
        if value['id']=='speech-runtime':(packages.root()/'speech-runtime/ready.json').unlink(missing_ok=True)
        _previews.pop(body.token,None)
    return {'ok':True,'bytes':sum(f['bytes'] for f in value['files'])}
