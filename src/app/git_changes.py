"""Structured local changes and single-use staging approvals."""
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from fastapi import APIRouter,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from app import catalog,work_settings
from app.workbench_git import git,root_for,status,lock

router=APIRouter(prefix='/v9/git')


async def snapshot(root):
    head=await git(root,'rev-parse','--verify','HEAD')
    branch=await git(root,'symbolic-ref','--quiet','HEAD')
    index=await git(root,'ls-files','--stage','-z')
    if index['code']:raise HTTPException(409,'无法读取暂存区')
    return {'head':head['output'].strip() if not head['code'] else None,
            'branch':branch['output'].strip() if not branch['code'] else None,
            'index_hash':hashlib.sha256(index['output'].encode()).hexdigest()}


async def changes(root):
    raw=await git(root,'status','--porcelain=v1','-z','--untracked-files=all')
    if raw['code']:raise HTTPException(409,'无法读取项目文件状态')
    entries=raw['output'].split('\0');items=[];index=0
    while index<len(entries):
        entry=entries[index];index+=1
        if not entry:continue
        flag,path=entry[:2],entry[3:];old=None
        if 'R' in flag or 'C' in flag:
            if index>=len(entries):raise HTTPException(409,'Git重命名状态不完整，请刷新')
            old=entries[index];index+=1
        conflict=flag in ('DD','AU','UD','UA','DU','AA','UU')
        items.append({'path':path,'old_path':old,'status':flag,'untracked':flag=='??','conflict':conflict,
                      'staged':flag[0] not in (' ','?','!'),'unstaged':flag=='??' or flag[1] not in (' ','!')})
        if len(items)>2000:raise HTTPException(413,'改动超过2000项，请在外部Git工具处理或缩小项目')
    return items


def file_fingerprint(root,path):
    rel=Path(path)
    if not path or rel.is_absolute() or rel.drive or '..' in rel.parts or ':' in path or '\0' in path:
        raise HTTPException(422,'Git文件路径必须位于仓库内')
    target=root
    for part in rel.parts:
        target=target/part
        if target.is_symlink() or target.is_junction():raise HTTPException(422,'此入口不暂存链接文件，请在外部Git工具处理')
    if not target.resolve().is_relative_to(root):raise HTTPException(422,'文件超出仓库')
    if not target.exists():return 'deleted'
    if not target.is_file() or target.stat().st_size>64*1024*1024:raise HTTPException(413,'此入口只处理64MiB以内普通文件')
    sha=hashlib.sha256();size=0
    with target.open('rb') as stream:
        while chunk:=stream.read(128*1024):
            size+=len(chunk)
            if size>64*1024*1024:raise HTTPException(413,'文件在读取期间超过64MiB')
            sha.update(chunk)
    return sha.hexdigest()


@router.get('/changes')
async def read_changes(session_id:int=Query(gt=0)):
    root=await root_for(session_id)
    return {**await status(root),'items':await changes(root),'snapshot':await snapshot(root)}


class FileAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)
    path:str=Field(min_length=1,max_length=2000)
    action:Literal['stage','unstage']


@router.post('/file-preview')
async def file_preview(body:FileAction):
    root=await root_for(body.session_id)
    async with lock(root):
        state=await status(root)
        if state['conflicts'] or state['operationInProgress']:raise HTTPException(409,'请先完成冲突或未结束的Git操作')
        item=next((r for r in await changes(root) if r['path']==body.path),None)
        if not item or not item['unstaged' if body.action=='stage' else 'staged']:raise HTTPException(409,'文件状态已改变，请刷新后重选')
        paths=[body.path]+([item['old_path']] if item['old_path'] and body.action=='unstage' else [])
        fingerprints={path:file_fingerprint(root,path) for path in paths}
        diff=await git(root,'--literal-pathspecs','diff',*(('--cached',) if body.action=='unstage' else ()), '--no-ext-diff','--no-textconv','--no-color','--',*paths,limit=1000000)
        if diff['code']:raise HTTPException(409,'文件差异未读取，请在Git工具中核对')
        text=diff['output']
        if item['untracked']:
            target=root/body.path
            with target.open('rb') as stream:raw=stream.read(16000)
            try:text=raw.decode('utf-8') if b'\0' not in raw else '二进制文件'
            except UnicodeError:text='非UTF-8文件'
            if target.stat().st_size>len(raw):text+='\n[预览仅显示开头16KB，确认会暂存整个文件]'
        token=uuid4().hex
        doc={**body.model_dump(),'root':str(root),'workspace':work_settings.get(body.session_id),
             'snapshot':await snapshot(root),'files':fingerprints,'paths':paths,'diff':text,
             'expires':time.time()+120,'status':'prepared'}
        catalog.put('git-action',token,doc,0)
    return {'token':token,**doc}


class Token(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token:str=Field(pattern=r'^[a-f0-9]{32}$')


@router.post('/file-commit')
async def file_commit(body:Token):
    item=catalog.get('git-action',body.token)
    if item['status']!='prepared':return item
    root=await root_for(item['session_id'])
    async with lock(root):
        item=catalog.get('git-action',body.token)
        if item['status']!='prepared':return item
        if (item['expires']<time.time() or str(root)!=item['root'] or work_settings.get(item['session_id'])!=item['workspace']
                or await snapshot(root)!=item['snapshot'] or any(file_fingerprint(root,p)!=sha for p,sha in item['files'].items())):
            raise HTTPException(409,'批准的文件、暂存区、分支或项目已改变，请重新预览')
        doc={k:v for k,v in item.items() if k not in ('id','revision')};doc['status']='running'
        saved=catalog.put('git-action',body.token,doc,item['revision']);final='unknown';result=None
        try:
            if item['action']=='stage':args=['add','--',*item['paths']]
            elif item['snapshot']['head']:args=['restore','--staged','--',*item['paths']]
            else:args=['update-index','--force-remove','--',*item['paths']]
            result=await git(root,'--literal-pathspecs',*args)
            final='complete' if result['code']==0 else 'failed'
            return {**saved,'status':final,'result':result}
        finally:
            catalog.put('git-action',body.token,{**doc,'status':final,'result':result},saved['revision'])
