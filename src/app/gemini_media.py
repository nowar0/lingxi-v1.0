"""Gemini non-streaming media generation on the configured service's own host."""
import base64
import io
import json
import re
import wave
from urllib.parse import quote
import httpx2
from fastapi import HTTPException


async def invoke(item, module_id, text, voice, attachment, auth, *, usage_out=None):
    base=item['base_url'].rstrip('/')
    if base.endswith('/openai'): base=base[:-7]
    model=item['model'].removeprefix('models/')
    endpoint=base+'/models/'+quote(model,safe='-._')+':generateContent'
    parts=[{'text':text or ('请描述这张图片。' if module_id=='vision' else '请将这段音频逐字转写为文字。')}]
    if attachment:
        row,path=attachment
        parts.insert(0,{'inlineData':{'mimeType':row['mime'],'data':base64.b64encode(path.read_bytes()).decode()}})
    payload={'contents':[{'role':'user','parts':parts}]}
    if module_id=='image': payload['generationConfig']={'responseModalities':['TEXT','IMAGE']}
    elif module_id=='tts':
        payload['generationConfig']={'responseModalities':['AUDIO'],'speechConfig':{'voiceConfig':{'prebuiltVoiceConfig':{'voiceName':voice}}}}
    key=auth.get('Authorization','').removeprefix('Bearer ')
    async with httpx2.AsyncClient(timeout=item['timeout'],follow_redirects=False) as client:
        async with client.stream('POST',endpoint,headers=({'x-goog-api-key':key} if key else {}),json=payload) as response:
            if not response.is_success: raise HTTPException(502,f'Gemini 返回 HTTP {response.status_code}，请检查模型、地址和能力')
            chunks=[]; size=0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>24*1024*1024: raise HTTPException(502,'模型结果超过接收限制')
                chunks.append(chunk)
    result=json.loads(b''.join(chunks))
    if not isinstance(result, dict): raise ValueError('Gemini 结果格式无效')
    candidates=result.get('candidates')
    if not isinstance(candidates,list) or not candidates or not isinstance(candidates[0],dict):
        raise ValueError('Gemini 没有返回可用结果')
    message=candidates[0].get('content')
    content=message.get('parts') if isinstance(message,dict) else None
    if not isinstance(content,list) or any(not isinstance(part,dict) for part in content):
        raise ValueError('Gemini 内容格式无效')
    metadata=result.get('usageMetadata', {})
    usage={}
    if isinstance(metadata,dict):
        for source,target in (('promptTokenCount','prompt_tokens'),('candidatesTokenCount','completion_tokens')):
            number=metadata.get(source)
            if isinstance(number,int) and not isinstance(number,bool) and number>=0:usage[target]=number
        for source,target,key in (('cachedContentTokenCount','prompt_tokens_details','cached_tokens'),
                                  ('thoughtsTokenCount','completion_tokens_details','reasoning_tokens')):
            number=metadata.get(source)
            if isinstance(number,int) and not isinstance(number,bool) and number>=0:usage[target]={key:number}
    if usage_out is not None:usage_out.update(usage)
    if module_id in ('stt','vision'):
        text=''.join(part.get('text','') for part in content if isinstance(part.get('text'),str) and not part.get('thought'))
        if not text.strip():raise ValueError('Gemini 没有返回识别文字')
        result={'text':text} if module_id=='stt' else {'choices':[{'message':{'content':text}}]}
        return json.dumps({**result,'usage':usage or None}).encode()
    inline=next((part.get('inlineData',part.get('inline_data')) for part in content if 'inlineData' in part or 'inline_data' in part),None)
    if not isinstance(inline,dict) or not isinstance(inline.get('data'),str): raise ValueError('Gemini 未返回请求的媒体内容')
    binary=base64.b64decode(inline['data'],validate=True)
    if module_id=='image': return json.dumps({'data':[{'b64_json':inline['data']}],'usage':usage or None}).encode()
    mime=inline.get('mimeType',inline.get('mime_type',''))
    if 'wav' in mime: return binary
    if 'L16' not in mime and 'pcm' not in mime: raise ValueError('Gemini 返回了不支持的音频格式')
    rate=re.search(r'rate=(\d+)',mime)
    sample_rate=int(rate.group(1)) if rate else 24000
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(binary)
    return output.getvalue()
