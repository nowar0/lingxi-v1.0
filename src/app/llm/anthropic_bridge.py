"""Native Messages protocol, retaining text, tool approvals, signatures and usage."""
import json
from urllib.parse import urlsplit
import httpx2
from openai.types import CompletionUsage


def endpoint(base, suffix):
    base=base.rstrip('/')
    if urlsplit(base).path in ('','/anthropic'): base+='/v1'
    return base+'/'+suffix


def messages_for_api(messages):
    system, result=[],[]
    for message in messages:
        role=message['role']
        if role in ('system','developer'):
            system.append(str(message.get('content') or '')); continue
        if role=='tool':
            block={'type':'tool_result','tool_use_id':message['tool_call_id'],'content':str(message.get('content') or '')}
            if result and result[-1]['role']=='user' and isinstance(result[-1]['content'],list): result[-1]['content'].append(block)
            else: result.append({'role':'user','content':[block]})
            continue
        if role=='assistant' and message.get('_anthropic_blocks'):
            result.append({'role':'assistant','content':message['_anthropic_blocks']}); continue
        content=message.get('content')
        blocks=[]
        if isinstance(content,str) and content: blocks.append({'type':'text','text':content})
        elif isinstance(content,list):
            for part in content:
                if part.get('type')=='text': blocks.append({'type':'text','text':part['text']})
                elif part.get('type')=='image_url':
                    url=part['image_url']['url']
                    if url.startswith('data:'):
                        header,data=url.split(',',1)
                        blocks.append({'type':'image','source':{'type':'base64','media_type':header[5:].split(';')[0],'data':data}})
                    else: blocks.append({'type':'image','source':{'type':'url','url':url}})
        for call in message.get('tool_calls',[]):
            blocks.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],'input':json.loads(call['function']['arguments'])})
        if blocks: result.append({'role':role,'content':blocks})
    return '\n\n'.join(system),result


async def stream(provider,key,model,messages,tools,extra,output_limit):
    system,converted=messages_for_api(messages)
    maximum=output_limit or 4096
    payload={'model':model,'messages':converted,'max_tokens':maximum,'stream':True,**extra}
    if provider.get('top_p') is not None:payload['top_p']=provider['top_p']
    if provider.get('stop'):payload['stop_sequences']=provider['stop']
    if system: payload['system']=system
    if payload.get('thinking',{}).get('type')=='enabled':
        if maximum<=1024: raise RuntimeError('当前回复预算不足以开启此模型的思考，请提高回复预留 Token')
        payload['thinking']['budget_tokens']=min(4096,maximum-256)
    if tools: payload['tools']=[{'name':value['function']['name'],'description':value['function'].get('description',''),'input_schema':value['function']['parameters']} for value in tools]
    headers={'x-api-key':key,'anthropic-version':'2023-06-01'}
    blocks,arguments,usage={}, {}, {}
    completed=False; stop=None
    async with httpx2.AsyncClient(timeout=provider.get('timeout',90),follow_redirects=False) as client:
        async with client.stream('POST',endpoint(provider['base_url'],'messages'),headers=headers,json=payload) as response:
            if not response.is_success:
                from app.provider_failover import ProviderHTTPError
                raise ProviderHTTPError(response.status_code)
            async for line in response.aiter_lines():
                if not line.startswith('data:'): continue
                event=json.loads(line[5:].strip())
                kind=event.get('type')
                if kind=='message_start': usage.update(event.get('message',{}).get('usage',{}))
                elif kind=='content_block_start':
                    if len(blocks)>=32: raise RuntimeError('模型返回的内容块过多')
                    blocks[event['index']]=event['content_block']
                elif kind=='content_block_delta':
                    block=blocks[event['index']]; delta=event['delta']; variant=delta.get('type')
                    if variant=='text_delta': block['text']=block.get('text','')+delta['text']; yield 'answer',delta['text']
                    elif variant=='thinking_delta': block['thinking']=block.get('thinking','')+delta['thinking']; yield 'think',delta['thinking']
                    elif variant=='signature_delta': block['signature']=block.get('signature','')+delta['signature']
                    elif variant=='input_json_delta':
                        index=event['index']; arguments[index]=arguments.get(index,'')+delta['partial_json']
                        if len(arguments[index])>12000: raise RuntimeError('工具参数超过限制')
                elif kind=='message_delta':
                    stop=event.get('delta',{}).get('stop_reason',stop); usage.update(event.get('usage',{}))
                elif kind=='message_stop': completed=True
                elif kind=='error': raise RuntimeError('模型服务返回错误，本轮未标为成功')
    if not completed or stop not in ('end_turn','tool_use','stop_sequence'): raise RuntimeError('模型未完整结束，本轮未保存为成功回复')
    calls=[]
    for index,block in blocks.items():
        if block.get('type')=='tool_use':
            if index in arguments: block['input']=json.loads(arguments[index])
            if not block.get('id') or not block.get('name'): raise RuntimeError('工具调用缺少编号或名称')
            calls.append({'id':block['id'],'type':'function','function':{'name':block['name'],'arguments':json.dumps(block.get('input',{}),ensure_ascii=False)}})
    if len(calls)>8 or stop=='tool_use' and not calls: raise RuntimeError('工具调用数量或内容不完整')
    inputs=usage.get('input_tokens',0)+usage.get('cache_creation_input_tokens',0)+usage.get('cache_read_input_tokens',0)
    outputs=usage.get('output_tokens',0)
    yield 'usage',CompletionUsage(prompt_tokens=inputs,completion_tokens=outputs,total_tokens=inputs+outputs,prompt_tokens_details={'cached_tokens':usage.get('cache_read_input_tokens',0)})
    ordered=[blocks[index] for index in sorted(blocks)]
    message={'role':'assistant','content':''.join(block.get('text','') for block in ordered if block.get('type')=='text') or None,'_anthropic_blocks':ordered}
    if calls: message['tool_calls']=calls
    yield 'message',message


async def vision(item,key,messages,extra):
    result=None;usage=None
    async for kind,value in stream(item,key,item['model'],messages,None,extra,4096):
        if kind=='message': result=value
        elif kind=='usage': usage=value.model_dump()
    return json.dumps({'choices':[{'message':{'content':result['content']}}],'usage':usage}).encode()
