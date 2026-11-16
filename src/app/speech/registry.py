"""Lazy engine adapters. Preserve the existing offline services and API behavior."""
from dataclasses import dataclass
from fastapi import HTTPException,Request
from app.speech.contract import SpeechEngine, check_wav


@dataclass(frozen=True)
class EngineRegistration:
    id: str
    kind: str
    factory: object
    name: str
    transport: str


class GPTSoVITS:
    id = 'gpt-sovits-v2'

    def health(self):
        from app import local_tts
        state = local_tts.status(include_engines=False)
        settings = state['settings']
        selected = next((v for v in state['voices'] if v['id'] == settings['default_voice']), {})
        issues = [*state['package_issues'], *selected.get('issues', [])]
        return {'engine': self.id, 'model': self.id, 'ready': state['running'] and not issues,
                'installed': not state['package_issues'], 'device': settings.get('device', 'cpu'),
                'loaded_at': local_tts.service.loaded_at, 'running': state['running'],
                'issues': issues, 'last_error': state['last_error']}

    async def synthesize(self, request):
        from app import local_tts
        # Configuration supplies all reference paths. Model-supplied paths must
        # never reach the external service. Streaming is a later-wave feature.
        if request.stream or request.ref_audio or request.ref_text or request.lang or request.speed is not None:
            raise HTTPException(422, '当前角色引擎沿用已保存音色、语速和语言；暂不接受逐次覆盖或流式请求')
        return check_wav(await local_tts.service.synthesize(request.text, request.voice_id))

    async def transcribe(self, path):
        raise HTTPException(422, '此引擎不支持语音输入')


class Paraformer:
    id = 'paraformer-zh-offline'

    def health(self):
        from app import local_stt
        state = local_stt.status(include_engines=False)
        return {'engine': self.id, 'model': self.id, 'ready': state['running'] and not state['issues'],
                'installed': not state['issues'], 'device': state['settings'].get('device', 'cpu'),
                'loaded_at': local_stt.service.loaded_at, 'running': state['running'], 'issues': state['issues']}

    async def synthesize(self, request):
        raise HTTPException(422, '此引擎不支持语音输出')

    async def transcribe(self, path):
        from app import local_stt
        return await local_stt.service.transcribe(path)


ENGINES = {}
_instances = {}


def register(registration):
    if registration.id in ENGINES:
        raise ValueError('重复语音引擎')
    ENGINES[registration.id] = registration


def engine(identifier) -> SpeechEngine:
    if identifier not in ENGINES:
        raise HTTPException(422, '未登记的语音引擎')
    if identifier not in _instances:
        _instances[identifier] = ENGINES[identifier].factory()
    return _instances[identifier]


register(EngineRegistration(GPTSoVITS.id, 'tts', GPTSoVITS, 'GPT-SoVITS V2', 'http'))
register(EngineRegistration(Paraformer.id, 'stt', Paraformer, 'FunASR Paraformer', 'stdio'))

from app.speech.sidecar import OwnedEngine
for identifier,kind,name in [('sensevoice-small','stt','SenseVoice Small · 五语离线'),
        ('cosyvoice2','tts','CosyVoice2 · 默认推荐'),('indextts2.5','tts','IndexTTS 2.5 · 建议独立显卡')]:
    register(EngineRegistration(identifier,kind,lambda key=identifier:OwnedEngine(key),name,'owned-http'))


from app.catalog import router


@router.get('/speech/engines')
def engines():
    return {'items': [{**engine(key).health(), 'kind': item.kind, 'name': item.name,
                       'transport': item.transport} for key, item in ENGINES.items()]}


@router.get('/speech/{identifier}/health')
def health(identifier: str):
    return engine(identifier).health()


from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from fastapi.responses import Response,StreamingResponse
from app.speech.contract import SynthesisRequest


@router.post('/speech/device-check')
async def device_check():
    import asyncio,json,os,subprocess
    from app import model_packages
    _,python=model_packages.runtime_root()
    script='import json,torch; print(json.dumps({"cpu":True,"cuda":torch.cuda.is_available(),"gpu":torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"vram_mb":torch.cuda.get_device_properties(0).total_memory//1048576 if torch.cuda.is_available() else None}))'
    process=await asyncio.create_subprocess_exec(str(python),'-I','-c',script,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        raw,errors=await asyncio.wait_for(process.communicate(),45)
        if process.returncode:raise HTTPException(503,'独立语音环境检测失败，请重新安装运行环境')
        value=json.loads(raw);return {**value,'note':'设备检测不代表具体模型已实际合成；CosyVoice2 支持CPU，IndexTTS建议独立显卡'}
    finally:
        if process.returncode is None:process.kill();await process.wait()


@router.post('/speech/{identifier}/synthesize')
async def synthesize(identifier:str,body:SynthesisRequest,request:Request):
    return await while_connected(request,lambda:synthesize_response(identifier,body))


async def while_connected(request,operation):
    import asyncio
    owner=asyncio.current_task()
    async def watch():
        while owner and not owner.done():
            if await request.is_disconnected():owner.cancel();return
            await asyncio.sleep(.2)
    watcher=asyncio.create_task(watch())
    try:return await operation()
    finally:
        watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)


async def synthesize_response(identifier:str,body:SynthesisRequest):
    if ENGINES.get(identifier) is None or ENGINES[identifier].kind!='tts': raise HTTPException(422,'请选择合成引擎')
    if body.stream:
        if not isinstance(engine(identifier),OwnedEngine): raise HTTPException(422,'此引擎不提供流式传输')
        iterator=engine(identifier).stream(body)
        # Await the first inference byte before committing response headers.
        first=await anext(iterator)
        async def chunks():
            try:
                yield first
                async for chunk in iterator: yield chunk
            finally: await iterator.aclose()
        return StreamingResponse(chunks(),media_type='application/x-lingxi-pcm',headers={'Cache-Control':'no-store'})
    return Response(await engine(identifier).synthesize(body),media_type='audio/wav',headers={'Cache-Control':'no-store'})


@router.post('/speech/{identifier}/stop')
async def stop(identifier:str):
    item=engine(identifier)
    if isinstance(item,OwnedEngine): await item.close()
    elif identifier==GPTSoVITS.id:
        from app.local_tts import service
        await service.close()
    else:
        from app.local_stt import service
        await service.close()
    return item.health()


from app.local_tts import Voice as ReferenceVoice

class EngineSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    device:Literal['cpu','cuda:0']='cpu'
    hotwords:list[str]=Field(default_factory=list,max_length=100)
    voices:list[ReferenceVoice] | None = Field(default=None,min_length=1,max_length=30)
    default_voice:str | None = Field(default=None,max_length=60)
    speed:float | None = Field(default=None,ge=0.5,le=2)
    expected_revision:int=Field(default=0,ge=0)


@router.get('/speech/{identifier}/settings')
def get_settings(identifier:str):
    from app.speech.sidecar import options
    if not isinstance(engine(identifier),OwnedEngine): raise HTTPException(422,'旧引擎使用原有设置')
    value=options(identifier)
    if ENGINES[identifier].kind=='tts':
        from app import local_tts
        inherited=local_tts.settings()
        value={**value,**{key:value.get(key) if value.get(key) is not None else inherited[key] for key in ('voices','default_voice','speed')}}
    return value


@router.put('/speech/{identifier}/settings')
async def save_settings(identifier:str,body:EngineSettings):
    from app import catalog
    from app.speech.sidecar import options
    if not isinstance(engine(identifier),OwnedEngine): raise HTTPException(422,'旧引擎使用原有设置')
    if identifier=='sensevoice-small' and body.device!='cpu': raise HTTPException(422,'随包 SenseVoice 使用 CPU')
    if identifier=='sensevoice-small' and any(value is not None for value in (body.voices,body.default_voice,body.speed)):
        raise HTTPException(422,'语音输入不使用合成音色配置')
    if ENGINES[identifier].kind=='tts':
        merged={**get_settings(identifier),**body.model_dump(exclude_unset=True,exclude={'expected_revision'})}
        ids=[voice['id'] if isinstance(voice,dict) else voice.id for voice in merged['voices']]
        if len(ids)!=len(set(ids)) or merged['default_voice'] not in ids:
            raise HTTPException(422,'音色编号不能重复，默认音色必须存在')
        removed={voice['id'] for voice in get_settings(identifier)['voices']}-set(ids)
        if any(item.get('model')==identifier and item.get('voice') in removed for item in catalog.listing('module')):
            raise HTTPException(409,'音色仍被功能分配使用，请先更换')
    for entry in body.hotwords:
        if entry.count('=>')!=1 or any(not value.strip() or len(value)>60 for value in entry.split('=>')):
            raise HTTPException(422,'热词后处理使用 原识别=>替换词，每项不超过60字；不是解码器热词偏置')
    patch=body.model_dump(exclude_unset=True,exclude={'expected_revision'},exclude_none=True)
    previous={key:value for key,value in options(identifier).items() if key!='revision'}
    saved=catalog.put('speech-engine',identifier.replace('.','_'),{**previous,**patch},body.expected_revision)
    await engine(identifier).close()
    return saved


class Activation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=0)
    voice_id:str=Field(default='',max_length=200)


@router.post('/speech/{identifier}/activate')
async def activate(identifier:str,body:Activation,request:Request):
    return await while_connected(request,lambda:activate_engine(identifier,body))


async def activate_engine(identifier:str,body:Activation):
    from app import catalog,local_tts,local_stt
    from app.speech.sidecar import voice_configuration
    if identifier not in ENGINES: raise HTTPException(422,'未登记语音引擎')
    kind=ENGINES[identifier].kind
    try: before=catalog.get('module',kind)
    except HTTPException as error:
        if error.status_code!=404: raise
        before={'revision':0}
    if before['revision']!=body.expected_revision: raise HTTPException(409,'模块已变化，请刷新')
    target=engine(identifier)
    if kind=='tts':
        # A failed preview does not mutate the selected module or any settings.
        await target.synthesize(SynthesisRequest(text='你好，老师。今天也一起努力吧。',voice_id=body.voice_id))
        value,voices=voice_configuration(identifier);voice=body.voice_id or value['default_voice']
    else:
        if isinstance(target,OwnedEngine):
            async with target.lock: await target._start()
        else:
            missing=local_stt.issues(local_stt.settings())
            if missing: raise HTTPException(409,'；'.join(missing))
        voice=''
    native=local_tts.native_module() if kind=='tts' else local_stt.native_module()
    result=catalog.put('module',kind,{**native,'enabled':True,'model':identifier,'voice':voice,'timeout':300},body.expected_revision)
    previous=_instances.get(before.get('model'))
    if previous and previous is not target and isinstance(previous,OwnedEngine): await previous.close()
    return {'module':result,'health':target.health(),'note':'启用前已实际试合成（语音输出）或加载模型（语音输入）；原版本仍保留'}
