"""Human slash commands run before any model loop; never interpret tool text."""
import asyncio
import hashlib
import json
import re
from uuid import UUID,uuid4
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import catalog,sessions
from app.db import connect

_locks={}


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS command_requests(
            id TEXT PRIMARY KEY,session_id INTEGER NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,result TEXT)''')
        db.execute("UPDATE command_requests SET status='unknown' WHERE status='running'")


def parse(text):
    match=re.fullmatch(r'\s*(/[a-z]+)(?:\s+(.*))?\s*',text,flags=re.S)
    if not match: return None
    from app.tools.registry import registry
    command=registry.entries.get(match[1])
    if not command or command.kind!='command':
        raise HTTPException(422,'未知指令，请使用 /help 查看；未提交给模型')
    return command.name,(match[2] or '').strip()


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=8000)
    request_id:str=Field(min_length=36,max_length=36)


@catalog.router.get('/commands')
def listing():
    from app.tools.registry import registry
    return {'items':[item.spec for item in registry.entries.values() if item.kind=='command']}


@catalog.router.post('/sessions/{sid}/commands')
async def execute(sid:int,body:Input):
    from app import handoff,queue6,stage5,session_options
    try: UUID(body.request_id)
    except ValueError: raise HTTPException(422,'指令需要独立UUID请求编号') from None
    current=sessions.get_session(sid)
    if not current or current['mode'] not in ('companion','work') or current['archived']:
        raise HTTPException(404,'当前会话不存在或已归档')
    parsed=parse(body.text)
    if not parsed: raise HTTPException(422,'此输入不是支持的指令')
    digest=hashlib.sha256(body.text.encode()).hexdigest()
    async with _locks.setdefault(body.request_id,asyncio.Lock()):
        with connect() as db:
            old=db.execute('SELECT * FROM command_requests WHERE id=?',(body.request_id,)).fetchone()
            if old:
                if old['session_id']!=sid or old['fingerprint']!=digest: raise HTTPException(409,'指令请求编号已用于其他内容')
                if old['status']=='complete': return json.loads(old['result'])
                raise HTTPException(409,'此前指令结果未确认，请查看会话后用新编号明确操作')
            db.execute("INSERT INTO command_requests VALUES(?,?,?,'running',NULL)",(body.request_id,sid,digest))
        name,argument=parsed
        try:
            result={'command':True,'name':name,'session_id':sid,'status':'complete','model_invoked':False}
            notice=None
            if name=='/help':
                notice='可用指令：\n'+ '\n'.join(item['name']+' '+item['argument']+'：'+item['description'] for item in listing()['items'])
            elif name=='/work':
                if not argument: raise HTTPException(422,'用法：/work <目标>')
                result['proposal']=handoff.suggest({'session_id':sid},argument,'提供可复核成果并说明实际检查结果','用户通过 /work 主动转交',manual=True)
            elif name in ('/goal','/plan','/compact','/permission'):
                from app.composer_controls import get_state,patch_state
                controls=get_state(sid)
                if name=='/goal':
                    if argument:
                        controls=patch_state(sid,goal='' if argument=='clear' else argument)
                    notice='当前会话目标：'+(controls['goal'] or '尚未设置')
                elif name=='/plan':
                    if argument not in ('','on','off'): raise HTTPException(422,'用法：/plan [on|off]')
                    controls=patch_state(sid,plan_mode=not controls['plan_mode'] if not argument else argument=='on')
                    notice='已进入计划模式，仅允许只读工具。' if controls['plan_mode'] else '已退出计划模式，仍遵守当前权限与逐项审批。'
                elif name=='/compact':
                    if argument: raise HTTPException(422,'用法：/compact；自动压缩开关在会话设置中调整')
                    from app import composer_compaction
                    result['compaction']=composer_compaction.request(sid,body.request_id)
                    notice='已提交上下文压缩，摘要将使用配置的模型生成；原始消息保留。' if result['compaction'].get('job_id') else result['compaction']['note']
                else:
                    if argument not in ('ask','readonly','workspace'): raise HTTPException(422,'用法：/permission readonly 或 /permission workspace；完全权限请在权限菜单勾选确认')
                    controls=patch_state(sid,permission=argument)
                    notice='已设为只读权限。' if argument=='readonly' else '已恢复逐次审批；未授予免审批或完整系统权限。'
                result['controls']=controls
            elif name=='/feedback':
                from app.composer_controls import feedback,Feedback
                result['feedback']=feedback(sid,Feedback(text=argument,request_id=body.request_id))
                notice='反馈已保存到本地会话，没有自动外发。'
            elif name=='/export':
                if argument: raise HTTPException(422,'/export 不接受参数')
                result['download']=f'/v6/sessions/{sid}/composer/export.zip'
                result['message']='已准备当前会话ZIP。'
            elif name=='/stop':
                if argument: raise HTTPException(422,'/stop 不接受参数')
                await queue6.stop(sid,{'mode':current['mode']})
                notice='已停止当前聊天生成，排队输入仍保留；工作任务需在任务界面单独暂停。'
            elif name=='/reset':
                if argument: raise HTTPException(422,'/reset 不接受参数')
                new=stage5.create_session(stage5.SessionInput(mode=current['mode'],creation_id=body.request_id,title='新会话'))
                result['session_id']=new['id']
                result['message']='已建立同模式的新会话，原会话和历史记录保留。'
            elif name=='/model':
                from app.llm import router
                candidates=[]
                for value in [*router.list_models(),*router.list_roles()]:
                    try:
                        _,model=router.resolve(value)
                        if argument.casefold() in (value.casefold(),model.casefold()): candidates.append(value)
                    except ValueError: continue
                candidates=list(dict.fromkeys(candidates))
                if argument in candidates: selected=argument
                elif len(candidates)==1: selected=candidates[0]
                else: raise HTTPException(422,{'message':'模型名称未找到或有多个匹配，请使用完整编号','choices':candidates[:20]})
                old=session_options.get(sid)
                payload={key:old[key] for key in session_options.Options.model_fields if key!='expected_revision' and key in old}
                payload.update(model=selected,expected_revision=old['revision'])
                result['options']=session_options.save(sid,session_options.Options(**payload))
                notice='当前会话已选择模型 '+selected+'；未执行模型调用。'
            elif name=='/skill':
                metadata=[item for item in catalog.skill_metadata() if item.get('available') and argument in (item['id'],item['name'])]
                if len(metadata)!=1: raise HTTPException(422,'技能名称未找到或不唯一，请使用已启用技能的完整编号')
                selected=metadata[0]
                item=catalog.assigned_skill(selected['id'])
                if len(item['body'])>24000: raise HTTPException(422,'技能正文过长，请先拆分')
                key='session_'+str(sid)
                try: prior=catalog.get('session-skills',key)
                except HTTPException as error:
                    if error.status_code!=404: raise
                    prior={'revision':0,'skill_ids':[]}
                chosen=list(dict.fromkeys([*prior['skill_ids'],selected['id']]))[-20:]
                catalog.put('session-skills',key,{'skill_ids':chosen},prior['revision'])
                result['skill']={key:value for key,value in selected.items() if key!='body'}
                notice='已选择技能「'+selected['name']+'」作为当前会话参考，正文将按需加载；未执行技能中的命令。'
            if notice:
                mid=handoff.append_notice(sid,notice,'command:'+body.request_id)
                result.update(message=notice,message_id=mid)
            with connect() as db:
                db.execute("UPDATE command_requests SET status='complete',result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),body.request_id))
            return result
        except BaseException:
            with connect() as db: db.execute("UPDATE command_requests SET status='failed' WHERE id=?",(body.request_id,))
            raise
