"""Explicit generation settings; unsupported parameters fail visibly."""
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from app import catalog

class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    top_p:float|None=Field(default=None,gt=0,le=1)
    stop:list[str]=Field(default_factory=list,max_length=4)
    max_output_tokens:int|None=Field(default=None,ge=128,le=131072)
    expected_revision:int=Field(ge=0)

def supported(item):
    from app.reasoning import for_module,openai_model
    from app.llm.responses_bridge import required
    capability=for_module(item)
    # A single saved default must work both with and without tool calls.
    reasoning=capability.get('family')=='openai' and openai_model(item.get('model',''))
    sampling='text' in item.get('capabilities',[]) and item.get('protocol','openai') in ('openai','anthropic') and capability.get('family') not in ('kimi','gemini') and not reasoning and not required(item.get('model',''),True,item.get('reasoning_effort'))
    return {'temperature':'text' in item.get('capabilities',[]) and capability.get('family') not in ('kimi','gemini') and (not reasoning or item.get('reasoning_effort')=='none'),'top_p':sampling,'stop':sampling,'max_output_tokens':'text' in item.get('capabilities',[])}

def validate(item,body):
    flags=supported(item)
    if body.top_p is not None and not flags['top_p'] or body.stop and not flags['stop']:raise HTTPException(422,'此模型不支持采样或停止词覆盖，请留空')
    if body.max_output_tokens is not None and not flags['max_output_tokens']:raise HTTPException(422,'输出上限仅用于文字模型')
    if any(not value.strip() or len(value)>200 for value in body.stop):raise HTTPException(422,'停止词不能为空且每项最多200字')

def read(kind,key):
    if kind not in ('profile','module'):raise HTTPException(404,'配置不存在')
    item=catalog.get('model-profile' if kind=='profile' else 'module',key)
    return {**{k:item.get(k,[] if k=='stop' else None) for k in ('top_p','stop','max_output_tokens')},'revision':item['revision'],'supported':supported(item)}

def save(kind,key,body):
    from app.model_references import assert_idle
    if kind not in ('profile','module'):raise HTTPException(404,'配置不存在')
    with catalog.transaction():
        assert_idle();table='model-profile' if kind=='profile' else 'module';item=catalog.get(table,key);validate(item,body)
        values=body.model_dump(exclude={'expected_revision'})
        result=catalog.put(table,key,{**item,**values},body.expected_revision)
        if kind=='profile':
            for linked in catalog.listing('module'):
                if linked.get('profile_id')==key:catalog.put('module',linked['id'],{**linked,**values},linked['revision'])
    return read(kind,key)

def request_parameters(provider,reasoning=False):
    values={}
    if provider.get('top_p') is not None:values['top_p']=provider['top_p']
    if provider.get('stop'):values['stop']=provider['stop']
    if values and reasoning:raise ValueError('当前思考模型不接受采样/停止词，请在模型参数中清空这些覆盖')
    return values
