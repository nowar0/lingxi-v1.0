"""Known model packages: resumable files, hashes, atomic versions and rollback.

The bundled catalog is data, never a script. Requests select an existing id;
they cannot supply URLs, commands, filesystem roots or executable hooks.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import subprocess
import threading
import time
from urllib import request,error
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import catalog,config

_jobs={}
_tasks={}
_cancel={}
_gate=asyncio.Semaphore(2)
_runtime_lock=threading.Lock()
_pointers_lock=threading.RLock()


def catalog_data():
    value=json.loads((config.BASE_DIR/'config/model_packages.json').read_text(encoding='utf-8'))
    if value.get('schema')!='lingxi.model-packages.v1': raise ValueError('模型包清单版本不可用')
    return value


def packages():
    return {item['id']:item for item in catalog_data()['packages']}


def known_version(identifier,version):
    current=packages().get(identifier)
    choices=([current] if current else [])+catalog_data().get('history',{}).get(identifier,[])
    item=next((value for value in choices if value['version']==version),None)
    if not item: raise HTTPException(409,'此模型版本不在受支持的清单中，未加载其内容')
    return item


def root():
    value=config.DATA_DIR/'model-packages'
    value.mkdir(parents=True,exist_ok=True)
    if value.is_symlink() or value.is_junction(): raise ValueError('模型包目录不允许链接或联接点')
    return value


def contained(base,relative):
    normalized=str(relative).replace('\\','/')
    rel=PurePosixPath(normalized)
    if not normalized or rel.is_absolute() or '..' in rel.parts or ':' in normalized:
        raise ValueError('模型包路径越界')
    dest=base.joinpath(*rel.parts)
    for part in [dest,*list(dest.parents)[:len(rel.parts)]]:
        if part.is_symlink() or part.is_junction(): raise ValueError('模型包路径不允许链接')
    if not dest.resolve().is_relative_to(base.resolve()): raise ValueError('模型包路径越界')
    return dest


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.writing-'+uuid4().hex)
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temp,path)


def pointers():
    path=root()/'active.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'revision':0,'items':{}}


def model_root(identifier):
    item=packages().get(identifier)
    if not item: raise HTTPException(422,'未登记的模型包')
    state=pointers()['items'].get(identifier)
    if state:
        path=contained(root(),identifier+'/'+state['version'])
        if (path/'.ready.json').is_file():
            known=known_version(identifier,state['version'])
            saved=json.loads((path/'.ready.json').read_text(encoding='utf-8'))
            if saved['files']!=known['files']: raise HTTPException(409,'模型版本记录与受支持清单不一致')
            return path,known
    if item.get('builtin_path'):
        builtin=item['builtin_path']
        if not builtin.startswith('models/'):
            raise ValueError('随包模型路径无效')
        path=contained(config.BUNDLED_MODEL_ROOT,builtin.removeprefix('models/'))
        if all(contained(path,file['path']).is_file() for file in item['files']): return path,item
    raise HTTPException(409,'此模型包尚未安装，请先下载或导入匹配的模型包')


def verify(identifier):
    path,item=model_root(identifier)
    for file in item['files']:
        target=contained(path,file['path'])
        if not target.is_file() or target.stat().st_size!=file['size'] or digest(target)!=file['sha256']:
            raise HTTPException(409,'模型包文件缺失或校验失败：'+file['path'])
    return path,item


def runtime_root():
    state=root()/'speech-runtime/ready.json'
    if not state.is_file(): raise HTTPException(409,'语音推理运行环境尚未安装')
    value=json.loads(state.read_text(encoding='utf-8'))
    if value['version']!=catalog_data()['runtime']['version']:raise HTTPException(409,'独立语音环境版本需要更新，请重新准备运行环境')
    directory=contained(root(),'speech-runtime/'+value['version'])
    python=contained(directory,value['python'])
    if not python.is_file(): raise HTTPException(409,'语音运行环境不完整，请重新安装')
    return directory,python


def status(identifier):
    item=packages().get(identifier)
    if not item: raise HTTPException(404,'模型包不存在')
    active=pointers()
    try:
        directory,installed_manifest=model_root(identifier)
        installed=True
        for file in installed_manifest['files']:
            path=contained(directory,file['path'])
            if not path.is_file() or path.stat().st_size!=file['size']: installed=False;break
    except HTTPException:
        installed=False;directory=None
    runtime_ready=True
    if item.get('runtime')=='speech-pytorch':
        try: runtime_root()
        except HTTPException: runtime_ready=False
    versions=[]
    folder=contained(root(),identifier)
    if folder.is_dir():
        for path in folder.iterdir():
            if not path.is_dir():continue
            try:known_version(identifier,path.name)
            except HTTPException:continue
            versions.append(path.name)
    latest=next((dict(job) for job in reversed(list(_jobs.values())) if job['package_id']==identifier),None)
    return {'id':identifier,'name':item['name'],'kind':item['kind'],'version':item['version'],
            'size':sum(file['size'] for file in item['files']),'file_count':len(item['files']),
            'license':item.get('license','见上游许可'),'source':item['source'],'min_ram_mb':item.get('min_ram_mb'),
            'device_hint':item.get('device_hint','CPU'),'installed':installed,'runtime_ready':runtime_ready,
            'active_version':active['items'].get(identifier,{}).get('version') or (item['version'] if installed else None),
            'pointer_revision':active['revision'],'versions':versions,'job':latest,
            'requires_runtime':item.get('runtime')=='speech-pytorch'}


@catalog.router.get('/speech/packages')
@catalog.router.get('/model-packages')
def listing():
    return {'items':[status(identifier) for identifier in packages()],
        'runtime_note':'大型语音引擎共用独立Python/PyTorch环境，体积另计；不纳入整包release-manifest'}


@catalog.router.get('/model-packages/{identifier}/manifest')
def manifest(identifier:str):
    if identifier not in packages(): raise HTTPException(404,'模型包不存在')
    return packages()[identifier]


def trusted_url(url):
    value=urlsplit(url)
    host=value.hostname or ''
    if value.scheme!='https' or value.username or value.password or value.fragment:
        raise ValueError('模型包仅从清单中的HTTPS源下载')
    allowed=('huggingface.co','hf.co','github.com','githubusercontent.com','objects.githubusercontent.com','download.pytorch.org')
    if not any(host==entry or host.endswith('.'+entry) for entry in allowed):
        raise ValueError('模型包下载跳转到未允许的源')
    return url


class Redirect(request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        trusted_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download_file(file,destination,state,cancel):
    if not 0<=file['size']<=8*1024**3 or len(file['sha256'])!=64: raise ValueError('模型包文件描述无效')
    if destination.is_file() and destination.stat().st_size==file['size'] and digest(destination)==file['sha256']:
        state['completed_bytes']+=file['size'];return
    destination.parent.mkdir(parents=True,exist_ok=True)
    if 'inline' in file:
        raw=file['inline'].encode('utf-8')
        if len(raw)>65536 or len(raw)!=file['size'] or hashlib.sha256(raw).hexdigest()!=file['sha256']:
            raise ValueError('内置模型元数据校验失败')
        destination.write_bytes(raw);state['completed_bytes']+=len(raw);return
    partial=destination.with_name(destination.name+'.part')
    opener=request.build_opener(request.ProxyHandler({}),Redirect())
    for attempt in range(3):
        if cancel.is_set(): raise InterruptedError('用户暂停下载，可稍后继续')
        offset=partial.stat().st_size if partial.is_file() else 0
        if offset>file['size']: partial.unlink();offset=0
        if offset==file['size'] and partial.is_file():
            if digest(partial)==file['sha256']:
                os.replace(partial,destination);state['completed_bytes']+=file['size'];state['current_bytes']=0;return
            partial.unlink();offset=0
        headers={'User-Agent':'Lingxi/0.9 package-manager','Accept-Encoding':'identity'}
        if offset: headers['Range']='bytes='+str(offset)+'-'
        try:
            response=opener.open(request.Request(trusted_url(file['url']),headers=headers),timeout=30)
            if response.status==206:
                expected=f'bytes {offset}-'
                if not response.headers.get('Content-Range','').startswith(expected): raise ValueError('断点响应位置不一致')
            elif response.status==200:
                offset=0
            else: raise ValueError('下载服务未返回可用文件')
            state['current_file']=file['path'];state['current_bytes']=offset
            with response,partial.open('ab' if offset else 'wb') as output:
                size=offset
                while True:
                    if cancel.is_set(): raise InterruptedError('用户暂停下载，可稍后继续')
                    chunk=response.read(1024*1024)
                    if not chunk: break
                    size+=len(chunk)
                    if size>file['size']: raise ValueError('下载内容超过清单大小')
                    output.write(chunk);state['current_bytes']=size
            if partial.stat().st_size!=file['size']: raise OSError('文件尚未下载完整')
            if digest(partial)!=file['sha256']:
                partial.unlink();raise OSError('校验失败，已丢弃损坏的片段')
            os.replace(partial,destination);state['completed_bytes']+=file['size'];state['current_bytes']=0
            return
        except InterruptedError: raise
        except (OSError,error.URLError,TimeoutError):
            if attempt==2: raise
    raise ValueError('下载失败')


def unpack_zip(archive,destination):
    with zipfile.ZipFile(archive) as bundle:
        total=sum(item.file_size for item in bundle.infolist())
        if total>4*1024**3 or len(bundle.infolist())>50000: raise ValueError('运行工具归档超过限制')
        for item in bundle.infolist():
            target=contained(destination,item.filename)
            if (item.external_attr>>16)&0o170000==0o120000: raise ValueError('归档不允许符号链接')
            if item.is_dir(): target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with bundle.open(item) as incoming,target.open('wb') as output: shutil.copyfileobj(incoming,output,1024*1024)


def run_owned(argv,cwd,env,state,cancel):
    logpath=root()/'runtime-install.log'
    with logpath.open('ab') as log:
        process=subprocess.Popen([str(value) for value in argv],cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            while process.poll() is None:
                if cancel.wait(.2): raise InterruptedError('用户停止运行环境安装；下次可重新准备')
            if process.returncode: raise ValueError('运行环境准备失败，请在下载详情查看 runtime-install.log')
        finally:
            if process.poll() is None:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
                else: process.kill()
                process.wait(timeout=10)


def prepare_runtime(state,cancel):
    with _runtime_lock:
        try: return runtime_root()
        except HTTPException: pass
        recipe=catalog_data()['runtime']
        directory=contained(root(),'speech-runtime/'+recipe['version'])
        directory.mkdir(parents=True,exist_ok=True)
        bootstrap=contained(root(),'installer/'+recipe['bootstrap']['path'])
        state['status']='preparing_runtime';state['phase']='准备已固定版本的语音运行环境'
        runtime_state={'completed_bytes':0,'current_bytes':0}
        download_file(recipe['bootstrap'],bootstrap,runtime_state,cancel)
        tools=contained(root(),'installer/uv')
        tools.mkdir(parents=True,exist_ok=True);unpack_zip(bootstrap,tools)
        choices=list(tools.rglob('uv.exe'))
        if len(choices)!=1: raise ValueError('安装工具归档布局不可用')
        uv=choices[0]
        env={key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','COMSPEC','PATH','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA') if key in os.environ}
        env.update(UV_CACHE_DIR=str(root()/'installer/cache'),UV_PYTHON_INSTALL_DIR=str(directory/'python'),
            UV_PYTHON_BIN_DIR=str(directory/'bin'),UV_TOOL_BIN_DIR=str(directory/'bin'),UV_TOOL_DIR=str(directory/'tools'),
            UV_NO_CONFIG='1',UV_PYTHON_DOWNLOADS='automatic',HF_HUB_DISABLE_TELEMETRY='1')
        run_owned([uv,'python','install',recipe['python_version']],directory,env,state,cancel)
        choices=[path for path in (directory/'python').rglob('python.exe') if 'Scripts' not in path.parts and not any(p.is_symlink() or p.is_junction() for p in [path.parent])]
        if not choices: raise ValueError('独立Python未安装完整')
        python=sorted(choices,key=lambda value:len(value.parts))[0]
        state['phase']='安装固定依赖（可能需要较长时间，下载可复用缓存）'
        lock=config.BASE_DIR/'config/speech-runtime.lock'
        if digest(lock)!=recipe['requirements_sha256']: raise ValueError('运行环境依赖锁文件校验失败')
        run_owned([uv,'pip','install','--python',python,'--system','--break-system-packages','--require-hashes','--index-strategy','unsafe-best-match','-r',lock],directory,env,state,cancel)
        for archive in recipe.get('extra_archives',[]):
            target=contained(root(),'installer/'+archive['path'])
            download_file(archive,target,runtime_state,cancel)
            unpack_zip(target,contained(directory,archive['destination']))
        # Probe the CPU path plus GPU availability without loading any model.
        run_owned([python,'-I','-c','import torch,torchaudio,transformers,onnxruntime; assert torch.__version__.startswith("2.8."); print(torch.cuda.is_available())'],directory,env,state,cancel)
        atomic_json(root()/'speech-runtime/ready.json',{'version':recipe['version'],'python':python.relative_to(directory).as_posix()})
        return directory,python


async def do_download(identifier,jobid):
    state=_jobs[jobid];cancel=_cancel[jobid];item=packages()[identifier]
    async with _gate:
        stage=contained(root(),'.downloads/'+identifier+'-'+item['version'])
        stage.mkdir(parents=True,exist_ok=True)
        try:
            state['status']='downloading'
            for file in item['files']:
                destination=contained(stage,file['path'])
                if not destination.exists():
                    try:
                        previous,_=model_root(identifier)
                        cached=contained(previous,file['path'])
                        if cached.is_file() and cached.stat().st_size==file['size'] and await asyncio.to_thread(digest,cached)==file['sha256']:
                            destination.parent.mkdir(parents=True,exist_ok=True)
                            await asyncio.to_thread(shutil.copy2,cached,destination)
                            state['reused_bytes']=state.get('reused_bytes',0)+file['size']
                    except HTTPException: pass
                await asyncio.to_thread(download_file,file,contained(stage,file['path']),state,cancel)
            if item.get('runtime')=='speech-pytorch': await asyncio.to_thread(prepare_runtime,state,cancel)
            for file in item['files']:
                if file.get('unpack')=='zip': await asyncio.to_thread(unpack_zip,contained(stage,file['path']),contained(stage,file['destination']))
            atomic_json(stage/'.ready.json',{'id':identifier,'version':item['version'],'files':item['files']})
            destination=contained(root(),identifier+'/'+item['version']);destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                # A completed immutable version may already exist after a restart.
                if not (destination/'.ready.json').is_file(): raise ValueError('目标版本残缺，请检查模型包目录')
                saved=json.loads((destination/'.ready.json').read_text(encoding='utf-8'))
                if saved['files']!=item['files'] or any(not contained(destination,file['path']).is_file() or digest(contained(destination,file['path']))!=file['sha256'] for file in item['files']):
                    raise ValueError('已安装的同名版本受损，未覆盖或启用；完整下载仍保留在 .downloads 中，请移走损坏版本后重试')
            else: os.replace(stage,destination)
            with _pointers_lock:
                current=pointers()
                if identifier not in current['items']:
                    current['items'][identifier]={'version':item['version'],'previous':None}
                    current['revision']+=1;atomic_json(root()/'active.json',current)
            state.update(status='complete',phase='文件与运行环境已验证，尚未更改全局语音引擎',current_bytes=0)
        except asyncio.CancelledError:
            cancel.set();state.update(status='paused',phase='程序退出，已暂停；片段保留')
            raise
        except InterruptedError as error:
            state.update(status='paused',error=str(error),phase='片段已保留，可继续下载')
        except Exception as error:
            state.update(status='failed',error=str(error)[:600],phase='未启用不完整版本；原有版本保留')


class Download(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(max_length=100)
    version:str=Field(max_length=100)


@catalog.router.post('/speech/download')
@catalog.router.post('/model-packages/download')
async def start(body:Download):
    item=packages().get(body.id)
    if not item or body.version!=item['version']: raise HTTPException(409,'模型包版本已变化，请刷新清单')
    existing=next((value for value in _jobs.values() if value['package_id']==body.id and value['status'] in ('queued','downloading','preparing_runtime')),None)
    if existing: return dict(existing)
    identifier=uuid4().hex
    state={'id':identifier,'package_id':body.id,'version':body.version,'status':'queued','completed_bytes':0,
        'current_bytes':0,'total_bytes':sum(file['size'] for file in item['files']),'phase':'等待下载','error':''}
    _jobs[identifier]=state;_cancel[identifier]=threading.Event()
    task=asyncio.create_task(do_download(body.id,identifier));_tasks[identifier]=task
    task.add_done_callback(lambda finished:_tasks.pop(identifier,None))
    return dict(state)


@catalog.router.post('/model-packages/downloads/{identifier}/pause')
def pause(identifier:str):
    if identifier not in _cancel: raise HTTPException(404,'下载任务不存在')
    _cancel[identifier].set()
    return {'status':'stopping','note':'当前网络读取结束后停止，已下载片段保留'}


class Activate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:str=Field(max_length=100)
    expected_revision:int=Field(ge=0)


@catalog.router.post('/model-packages/{identifier}/activate')
def activate(identifier:str,body:Activate):
    if identifier not in packages(): raise HTTPException(404,'模型包不存在')
    target=contained(root(),identifier+'/'+body.version)
    marker=target/'.ready.json'
    if not marker.is_file(): raise HTTPException(409,'此版本未经完整校验')
    saved=json.loads(marker.read_text(encoding='utf-8'))
    if saved['files']!=known_version(identifier,body.version)['files']:
        raise HTTPException(409,'此版本的描述与受支持清单不一致')
    for file in saved['files']:
        path=contained(target,file['path'])
        if not path.is_file() or digest(path)!=file['sha256']: raise HTTPException(409,'此版本校验失败，未切换')
    with _pointers_lock:
        current=pointers()
        if current['revision']!=body.expected_revision: raise HTTPException(409,'模型包指针已变化，请刷新')
        previous=current['items'].get(identifier,{}).get('version')
        current['items'][identifier]={'version':body.version,'previous':previous if previous!=body.version else None}
        current['revision']+=1;atomic_json(root()/'active.json',current)
    return status(identifier)


def rollback_failed(identifier,version):
    with _pointers_lock:
        current=pointers();active=current['items'].get(identifier,{})
        previous=active.get('previous')
        if active.get('version')!=version or not previous: return False
        target=contained(root(),identifier+'/'+previous)
        if not (target/'.ready.json').is_file(): return False
        current['items'][identifier]={'version':previous,'previous':None};current['revision']+=1
        atomic_json(root()/'active.json',current)
        return True


async def shutdown():
    for event in list(_cancel.values()):event.set()
    tasks=list(_tasks.values())
    if tasks:
        try:await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),35)
        except TimeoutError:
            for task in tasks:task.cancel()
