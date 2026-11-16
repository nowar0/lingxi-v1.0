"""Immutable local commit previews, optional hooks and a final approval."""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import time
from uuid import uuid4
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import catalog,config,git_hooks,work_settings
from app.git_changes import snapshot,changes,Token
from app.workbench_git import git,root_for,status,lock
from app.extension_tools import process,git_argv

router=APIRouter(prefix='/v9/git')
active={}


class Preview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)
    message:str=Field(min_length=1,max_length=5000)
    run_hooks:bool=False


def fields(item):return {k:v for k,v in item.items() if k not in ('id','revision')}


async def identity(root):
    result={}
    for kind in ('AUTHOR','COMMITTER'):
        item=await git(root,'var','GIT_'+kind+'_IDENT')
        match=re.fullmatch(r'(.+) <([^<>\r\n]+)> (\d+ [+-]\d{4})\s*',item['output']) if not item['code'] else None
        if not match:raise HTTPException(409,'请先配置Git提交者姓名和邮箱，再重新预览')
        result[kind]={'name':match[1],'email':match[2],'date':match[3]}
    return result


async def prepare(body,root,*,hooks_done=False,hook_results=None):
    if not body.message.strip() or '\0' in body.message:raise HTTPException(422,'请输入有效提交说明')
    state=await status(root)
    if state['conflicts'] or state['operationInProgress']:raise HTTPException(409,'先处理冲突或未结束的Git操作')
    before=await snapshot(root)
    if not before['branch'] or not before['branch'].startswith('refs/heads/'):raise HTTPException(409,'请先切换到本地分支，再创建提交')
    items=[item for item in await changes(root) if item['staged']]
    if not items:raise HTTPException(409,'暂存区没有改动，请先选择要提交的文件')
    tree=await git(root,'write-tree')
    if tree['code']:raise HTTPException(409,'暂存区无法生成提交快照')
    after=await snapshot(root)
    if before!=after:raise HTTPException(409,'生成预览时暂存区有变化，请重试')
    diff=await git(root,'diff','--cached','--no-ext-diff','--no-textconv','--no-color','--',limit=1_000_000)
    if diff['code']:raise HTTPException(409,'提交差异未能读取')
    if await snapshot(root)!=after:raise HTTPException(409,'读取差异时暂存区有变化，请重试')
    hooks=await git_hooks.inventory(root) if body.run_hooks else None
    token=uuid4().hex
    doc={**body.model_dump(),'root':str(root),'workspace':work_settings.get(body.session_id),'snapshot':after,
         'tree':tree['output'].strip(),'files':items,'diff':diff['output'],'identity':await identity(root),
         'hooks':hooks,'hooks_done':hooks_done,'hook_results':hook_results or [],'status':'prepared','expires':time.time()+120}
    saved=catalog.put('git-commit',token,doc,0)
    return {'token':token,**saved}


@router.post('/commit-preview')
async def preview(body:Preview):
    root=await root_for(body.session_id)
    async with lock(root):return await prepare(body,root)


async def validate(item,root):
    if (item['expires']<time.time() or item['root']!=str(root) or work_settings.get(item['session_id'])!=item['workspace']
            or await snapshot(root)!=item['snapshot']):raise HTTPException(409,'项目、分支或暂存内容已改变，请重新预览')
    if item['run_hooks'] and await git_hooks.inventory(root)!=item['hooks']:raise HTTPException(409,'hooks配置或脚本已改变，请重新预览')


def claim(item,token):
    # Reserve the session while the SQLite write lock still excludes a concurrent
    # project rebind. No network/process awaits are held inside this transaction.
    try:
        with catalog.transaction():
            if work_settings.get(item['session_id'])!=item['workspace']:raise HTTPException(409,'项目目录已改变，请重新预览')
            current=catalog.get('git-commit',token)
            if current['status']!='prepared':raise HTTPException(409,'此确认已处理，请刷新记录')
            doc=fields(current);doc['status']='running'
            result=catalog.put('git-commit',token,doc,current['revision'])
            active[token]={'session_id':item['session_id'],'task':asyncio.current_task()}
        return result
    except BaseException:
        active.pop(token,None);raise


@router.post('/commit-confirm')
async def confirm(body:Token):
    item=catalog.get('git-commit',body.token)
    if item['status']!='prepared':return item
    root=await root_for(item['session_id'])
    async with lock(root):
        item=catalog.get('git-commit',body.token)
        if item['status']!='prepared':return item
        await validate(item,root)
        saved=claim(item,body.token);doc=fields(saved);result=None;final='unknown';oid=None;publishing=False
        try:
            if item['run_hooks'] and not item['hooks_done']:
                folder=config.DATA_DIR/'git-commit-jobs'/body.token
                if folder.exists() or folder.parent.is_symlink() or folder.parent.is_junction():raise HTTPException(409,'提交检查目录不安全，请重新预览')
                folder.mkdir(parents=True)
                message=folder/'COMMIT_MESSAGE';message.write_text(item['message'],encoding='utf-8',newline='')
                records=[]
                try:
                    for event,args in (('pre-commit',()),('prepare-commit-msg',(str(message),'message')),('commit-msg',(str(message),))):
                        step=await git_hooks.execute(root,item['hooks'],event,*args)
                        records.append({'event':event,**step})
                        if step['code']:
                            result={'hook_failed':event,'hook_results':records,'message':'检查未通过；脚本已产生的文件或暂存变化请查看差异'};final='failed'
                            return {**saved,'status':final,'result':result}
                    if not message.is_file() or message.is_symlink() or message.stat().st_size>20_000:raise HTTPException(409,'hook生成的提交说明缺失或过大，请重新填写')
                    changed=message.read_text('utf-8')
                    followup=await prepare(Preview(session_id=item['session_id'],message=changed,run_hooks=True),root,hooks_done=True,hook_results=records)
                    result={'next_preview':followup};final='awaiting_review'
                    return {**saved,'status':final,'result':result}
                finally:
                    # Only our known message file is cleaned. Hooks may create
                    # other files; preserve them for inspection instead of deleting.
                    if message.is_file() and not message.is_symlink():message.unlink(missing_ok=True)
                    try:folder.rmdir()
                    except OSError:pass
            env=os.environ.copy()
            for kind,values in item['identity'].items():
                for key in ('name','email','date'):env['GIT_'+kind+'_'+key.upper()]=values[key]
            created=await process(git_argv(root,'-c','commit.gpgSign=false','commit-tree',item['tree'],*(('-p',item['snapshot']['head']) if item['snapshot']['head'] else ())),text=item['message'],timeout=20,env=env)
            if created['code']:raise HTTPException(409,'提交对象未生成：'+created['output'][:2000])
            oid=created['output'].strip()
            if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}',oid):raise HTTPException(502,'提交对象编号未确认')
            # Commit the approved immutable tree, then atomically compare/swap
            # this named branch. A later index mutation cannot enter this commit.
            publishing=True;result={'candidate_oid':oid,'message':'分支更新结果尚未确认，请核对本次编号'}
            updated=await git(root,'update-ref','-m','commit: '+item['message'].splitlines()[0][:100],
                              item['snapshot']['branch'],oid,item['snapshot']['head'] or '0'*len(oid))
            if updated['code']:
                result={'unreferenced_oid':oid,'message':'分支已改变，未更新分支；请刷新核对'};final='failed'
                return {**saved,'status':final,'result':result}
            result={'oid':oid,'branch':item['snapshot']['branch'],'message':item['message'],'hook_results':item['hook_results']};final='complete'
            # The commit is already complete. A failing/interrupted post-hook
            # never changes this to a retryable commit failure.
            if item['run_hooks']:
                try:result['post_hook']=await git_hooks.execute(root,item['hooks'],'post-commit')
                except asyncio.CancelledError:result['post_hook']={'code':None,'output':'完成脚本已停止；提交已创建'}
                except BaseException as error:result['post_hook']={'code':None,'output':str(error)}
            return {**saved,'status':final,'result':result}
        except asyncio.CancelledError:
            if final!='complete':
                final='unknown' if publishing else 'stopped'
                result={'candidate_oid':oid,'message':'分支更新结果未知，请核对提交编号；不会自动重复提交' if publishing else '操作已停止，脚本可能产生的文件变化请核对；不会自动重做'}
            return {**saved,'status':final,'result':result}
        finally:
            try:catalog.put('git-commit',body.token,{**doc,'status':final,'result':result},saved['revision'])
            finally:active.pop(body.token,None)


@router.get('/commit-result/{token}')
def receipt(token:str):
    if not re.fullmatch(r'[a-f0-9]{32}',token):raise HTTPException(422,'请求编号无效')
    item=catalog.get('git-commit',token)
    if item['status']=='running' and token not in active:item={**item,'status':'unknown'}
    return item


@router.post('/commit-cancel')
async def cancel(body:Token):
    item=catalog.get('git-commit',body.token);current=active.get(body.token)
    requested=bool(current and not current.get('cancelled'))
    if requested:current['cancelled']=True;current['task'].cancel()
    return {'requested':requested,'receipt':body.token,'status':item['status']}


def is_running(sid):return any(item['session_id']==sid for item in list(active.values()))


async def shutdown():
    entries=list(active.values());tasks=[item['task'] for item in entries]
    for item in entries:
        if not item.get('cancelled'):item['cancelled']=True;item['task'].cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
