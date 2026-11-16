"""Preview traditional and config-defined Git hooks without executing them."""
import hashlib
import json
import os
from pathlib import Path
import re
from fastapi import HTTPException
from app.extension_tools import executable,process
from app.managed_process import run

EVENTS=('pre-commit','prepare-commit-msg','commit-msg','post-commit')


def raw_argv(root,*args):
    # Only metadata commands and explicitly approved `hook run` use this path.
    return [executable('git'),'--no-pager','-c','core.fsmonitor=false','-C',str(root),*args]


async def metadata(root,*args):
    return await process(raw_argv(root,*args),env={**os.environ,'LC_ALL':'C','LANG':'C'})


async def inventory(root):
    version=(await metadata(root,'version'))['output']
    match=re.search(r'(\d+)\.(\d+)',version)
    modern=bool(match and tuple(map(int,match.groups()))>=(2,54))
    config=await metadata(root,'config','--null','--get-regexp',r'^hook\..*\.(command|event|enabled|parallel)$')
    if config['code'] not in (0,1):raise HTTPException(409,'Git hooks配置未读取')
    values={}
    for item in config['output'].split('\0'):
        key,separator,value=item.partition('\n')
        if separator:values[key]=value
    custom=await metadata(root,'config','--path','--get','core.hooksPath')
    if custom['code'] not in (0,1):raise HTTPException(409,'Git hooks目录未读取')
    location=custom['output'].strip()
    if not location:
        location=(await metadata(root,'rev-parse','--git-path','hooks'))['output'].strip()
    base=Path(location)
    if not base.is_absolute():base=root/base
    if base.is_symlink() or base.is_junction():raise HTTPException(409,'hooks目录是链接，请先在外部Git工具核对')
    base=base.resolve()
    items=[]
    for event in EVENTS:
        if modern:
            listed=await metadata(root,'hook','list','-z','--show-scope',event)
            if listed['code'] not in (0,1):raise HTTPException(409,'Git hooks清单未读取：'+listed['output'][:500])
            entries=listed['output'].split('\0') if listed['code']==0 else []
        else:
            path=base/event
            entries=['hook from hookdir'] if path.is_file() and (os.name=='nt' or os.access(path,os.X_OK)) else []
        for entry in entries:
            if not entry:continue
            if entry=='hook from hookdir':
                path=base/event
                if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size>1_000_000:raise HTTPException(409,'hook脚本为链接、缺失或过大，请先在外部核对：'+str(path))
                with path.open('rb') as stream:raw=stream.read(1_000_001)
                if len(raw)>1_000_000:raise HTTPException(409,'hook脚本在读取时变大')
                items.append({'event':event,'name':event,'scope':'hookdir','command':str(path),'sha256':hashlib.sha256(raw).hexdigest(),
                              'code':raw[:32000].decode('utf-8',errors='replace'),'truncated':len(raw)>32000})
            else:
                scope,separator,name=entry.partition('\t')
                if not separator:raise HTTPException(409,'此Git版本的hook清单格式暂不支持')
                command=values.get('hook.'+name+'.command')
                if command is None:raise HTTPException(409,'配置型hook命令缺失，请刷新')
                items.append({'event':event,'name':name,'scope':scope,'command':command,'sha256':hashlib.sha256(command.encode()).hexdigest(),'code':'','truncated':False})
    return {'path':str(base),'items':items,'config_hash':hashlib.sha256(config['output'].encode()).hexdigest(),'modern':modern}


async def execute(root,approved,event,*args):
    if event not in EVENTS:raise HTTPException(422,'不支持的提交hook事件')
    if await inventory(root)!=approved:raise HTTPException(409,'hooks目录、配置或脚本已改变，请重新预览批准')
    if not any(item['event']==event for item in approved['items']):return {'code':0,'output':''}
    return await run(raw_argv(root,'hook','run','--ignore-missing',event,*(('--',*args) if args else ())),cwd=root,timeout=45,env={**os.environ,'LC_ALL':'C','LANG':'C'})
