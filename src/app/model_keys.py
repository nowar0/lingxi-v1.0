"""Multiple reusable API credentials and provider model discovery; secrets stay in keyring."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlsplit
from uuid import uuid4
from typing import Literal
import httpx2
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from app import catalog, config, modules, model_profiles, credential_lifecycle, provider_catalog

_lock = asyncio.Lock()
CAPABILITIES = {'text', 'vision', 'video', 'image', 'stt', 'tts'}


def clean(item):
    return {'provider_id':'custom','protocol':'openai','catalog_mode':'service',
            **{key: value for key, value in item.items() if key != 'credential_ref'}}


def normalized_model(value, source='service'):
    from app.reasoning import describe
    identifier=value.get('id',value.get('name','')).strip()
    documented=provider_catalog.documented(identifier)
    metadata_modalities=any(k in value for k in ('capabilities','input_modalities','output_modalities'))
    declared=suggested({**value,'id':identifier},use_name=not metadata_modalities)
    fallback_inputs = ['text'] if 'text' in declared else []
    fallback_outputs = ['text'] if 'text' in declared else []
    if 'vision' in declared: fallback_inputs.append('image')
    if 'video' in declared: fallback_inputs.append('video')
    if 'image' in declared: fallback_outputs.append('image')
    if 'stt' in declared: fallback_inputs.append('audio')
    if 'tts' in declared: fallback_outputs.append('audio')
    inputs=value.get('input_modalities', fallback_inputs if metadata_modalities else documented['input_modalities'])
    outputs=value.get('output_modalities', fallback_outputs if metadata_modalities else documented['output_modalities'])
    modalities={'text','image','audio','video','document'}
    inputs=[v for v in inputs if v in modalities] if isinstance(inputs,list) else []
    outputs=[v for v in outputs if v in modalities] if isinstance(outputs,list) else []
    if not metadata_modalities:
        # A model-name/documentation rule must not advertise native video input.
        inputs=[value for value in inputs if value!='video']
    capabilities=value.get('capabilities')
    if isinstance(capabilities,dict):
        vision=capabilities.get('image_input',capabilities.get('vision',{}))
        if isinstance(vision,dict) and vision.get('supported') is True:
            inputs=list(dict.fromkeys([*inputs,'image']));declared=list(set(declared)|{'vision'})
        elif vision is False or isinstance(vision,dict) and vision.get('supported') is False:
            inputs=[value for value in inputs if value!='image']; declared=[value for value in declared if value!='vision']
        video=capabilities.get('video_input', {})
        if video is True or isinstance(video,dict) and video.get('supported') is True:
            inputs=list(dict.fromkeys([*inputs,'video']))
        elif video is False or isinstance(video,dict) and video.get('supported') is False:
            inputs=[value for value in inputs if value!='video']
    if 'text' in outputs: declared=list(set(declared)|{'text'})
    if 'image' in inputs and 'text' in outputs: declared=list(set(declared)|{'vision'})
    if source=='manual' and isinstance(capabilities,list): declared=capabilities
    elif not any(key in value for key in ('capabilities','input_modalities','output_modalities')):
        declared=list(set(declared)|set(documented['capabilities']))
    # Discovery/documentation is evidence only. Native-video routing is a manual
    # input declaration; neither model names nor output video may enable it.
    if source != 'manual': declared=[value for value in declared if value!='video']
    elif 'video' in declared: inputs=list(dict.fromkeys([*inputs,'video']))
    result={'id':identifier,'display_name':str(value.get('display_name') or value.get('name') or identifier)[:100],
            'capabilities':sorted(set(declared)&CAPABILITIES),'capability_status':'unverified',
            'input_modalities':inputs,'output_modalities':outputs,'capability_source':source if source=='manual' or any(k in value for k in ('capabilities','input_modalities','output_modalities')) else documented['capability_source'],
            'documentation':documented['documentation'],'reasoning':describe(identifier,value)}
    if source=='manual' and value.get('reasoning') is not None:
        result['reasoning']=describe(identifier,{'supported_reasoning_efforts':value['reasoning'].get('levels',[])})
        result['reasoning']['parameter']=value['reasoning'].get('parameter','effort')
        result['reasoning'].update(source='manual',note='用户自定义的思考档位，尚未实际验证')
    from app.context_capacity import service_capacity
    capacity = service_capacity(value)
    if capacity is not None:
        result['context_window'] = capacity
    return result


def request_headers(item, key):
    if item.get('protocol')=='anthropic':
        return {'x-api-key':key,'anthropic-version':'2023-06-01'}
    return {'Authorization':'Bearer '+key}


def valid_url(value):
    url=urlsplit(value)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise HTTPException(422,'请输入不含凭据或查询参数的 HTTP(S) 基础地址')
    return value.rstrip('/')


@catalog.router.post('/model-metadata')
def model_metadata(value: dict):
    identifier=value.get('id','')
    if not isinstance(identifier,str) or not identifier.strip() or len(identifier)>200:
        raise HTTPException(422,'请输入有效的模型 ID')
    return normalized_model({'id':identifier.strip()})


def entries():
    saved = catalog.listing('model-key')
    known = {(item['credential_ref'], item['base_url']) for item in saved}
    # Existing profiles remain usable without moving or duplicating their credentials.
    for profile in catalog.listing('model-profile'):
        reference = profile.get('credential_ref', '')
        pair = (reference, profile.get('base_url', ''))
        if not reference or pair in known or modules.looks_like_secret(reference):
            continue
        known.add(pair)
        identifier = 'existing-' + hashlib.sha256('\n'.join(pair).encode()).hexdigest()[:24]
        saved.append({'id': identifier, 'revision': 0, 'name': profile['name'], 'base_url': pair[1],
                      'credential_ref': reference, 'owned': False, 'models': [], 'connected': None,
                      'provider_id': profile.get('provider_id', 'custom'), 'protocol': profile.get('protocol', 'openai'),
                      'note': '已有配置的 Key；点击识别读取服务模型清单', 'discovered_at': ''})
    return saved


def get(key_id):
    item = next((entry for entry in entries() if entry['id'] == key_id), None)
    if not item:
        raise HTTPException(404, 'Key 记录不存在或已移除')
    return item


def suggested(model, use_name=True):
    caps = set()
    aliases = {'chat': 'text', 'chat_completion': 'text', 'image_generation': 'image',
               'speech_to_text': 'stt', 'transcription': 'stt', 'text_to_speech': 'tts'}
    declared = model.get('capabilities', [])
    if isinstance(declared, dict):
        declared = [key for key, enabled in declared.items() if enabled is True]
    if isinstance(declared, list):
        caps.update(aliases.get(value, value) for value in declared if isinstance(value, str))
    inputs, outputs = model.get('input_modalities', []), model.get('output_modalities', [])
    if isinstance(inputs, list) and 'image' in inputs: caps.add('vision')
    if isinstance(outputs, list) and 'image' in outputs: caps.add('image')
    if isinstance(inputs, list) and 'audio' in inputs: caps.add('stt')
    if isinstance(outputs, list) and 'audio' in outputs: caps.add('tts')
    if not use_name: return sorted(caps & CAPABILITIES)
    name = model['id'].lower()
    # Names are suggestions only. /models does not establish working capabilities.
    if 'gpt-image' in name or 'chatgpt-image' in name or 'dall-e' in name: caps.add('image')
    elif 'whisper' in name or 'transcribe' in name: caps.add('stt')
    elif 'tts' in name: caps.add('tts')
    elif any(part in name for part in ('gpt', 'claude', 'gemini', 'qwen', 'deepseek', 'llama', 'mistral')):
        if not any(part in name for part in ('embed', 'rerank', 'moderation', 'realtime')): caps.add('text')
        if any(part in name for part in ('gpt-4o', 'gpt-4.1', 'gpt-5', 'gpt-6', 'vision', '-vl', 'claude', 'gemini')): caps.add('vision')
    return sorted(caps & CAPABILITIES)


async def discover(item, transient_key=None):
    try:
        key = transient_key if transient_key is not None else config.load_key(item['credential_ref'])
    except Exception:
        return {**item, 'connected': False, 'note': '系统凭据暂不可读，请重试'}
    if not key:
        return {**item, 'connected': False, 'note': '凭据管理器中没有此 Key，请重新保存'}
    try:
        async with httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
            from app.llm.anthropic_bridge import endpoint
            address=endpoint(item['base_url'],'models') if item.get('protocol')=='anthropic' else item['base_url'].rstrip('/')+'/models'
            async with client.stream('GET', address, headers=request_headers(item,key)) as response:
                if not response.is_success:
                    return {**item, 'connected': False, 'note': f'模型列表返回 HTTP {response.status_code}；请检查地址、Key 权限或服务是否提供 /models，原清单已保留'}
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2 * 1024 * 1024: raise ValueError('Model listing too large')
                    chunks.append(chunk)
        data = json.loads(b''.join(chunks))
        records = data.get('data', data.get('models'))
        if not isinstance(records, list) or len(records) > 3000: raise ValueError('Invalid model listing')
        models = {}
        for value in records:
            if not isinstance(value, dict): continue
            identifier = value.get('id', value.get('name'))
            if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200: continue
            value = {**value, 'id': identifier.strip()}
            models[value['id']] = normalized_model(value)
        discovered=sorted(models.values(), key=lambda model: model['id'])
        visible=discovered if item.get('catalog_mode','service')=='service' else item.get('custom_models',[])
        return {**item, 'models':visible, 'discovered_models':discovered, 'connected': True,
                'discovered_at': datetime.now(timezone.utc).isoformat(),
                'note': '已读取此 Key 可见的模型；能力为服务元数据或模型名称建议，需实际调用确认' if models else '服务返回空模型列表；Key 可能没有模型权限'}
    except (httpx2.HTTPError, ValueError, TypeError, AttributeError):
        return {**item, 'connected': False, 'note': '模型列表读取失败；原清单已保留，可检查服务地址或使用手动配置'}


class NewKey(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(default='https://api.openai.com/v1', min_length=1, max_length=2000)
    api_key: SecretStr = Field(min_length=1, max_length=10000)
    provider_id: str = Field(default='custom',max_length=64)
    protocol: Literal['openai','anthropic','gemini'] = 'openai'


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)


class ReplaceKey(Revision):
    api_key: SecretStr = Field(min_length=1, max_length=10000)


@catalog.router.get('/model-keys')
def listing():
    return {'items': [clean(item) for item in entries()]}


@catalog.router.post('/model-key-preview')
async def preview_unsaved(body: NewKey):
    base=valid_url(body.base_url)
    ephemeral={'base_url':base,'protocol':body.protocol,'credential_ref':'','models':[]}
    result=await discover(ephemeral,body.api_key.get_secret_value())
    return {k:result.get(k) for k in ('models','connected','note')}


@catalog.router.post('/model-keys')
async def create(body: NewKey):
    url = urlsplit(body.base_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise HTTPException(422, '请输入不含凭据或查询参数的 HTTP(S) 基础地址')
    key = body.api_key.get_secret_value().strip()
    if not key: raise HTTPException(422, 'Key 不能为空')
    async with _lock:
        with credential_lifecycle.mutation():
            key_id = uuid4().hex
            reference = 'modelkey_' + key_id
            try: credential_lifecycle.save_key(reference, key, fresh=True)
            except Exception: raise HTTPException(503, '系统凭据管理器保存失败，未创建记录') from None
            item = {'name': body.name.strip(), 'base_url': body.base_url.rstrip('/'), 'credential_ref': reference,
                    'owned': True, 'models': [], 'connected': None, 'note': '', 'discovered_at': '',
                    'provider_id':body.provider_id,'protocol':body.protocol,'catalog_mode':'service'}
            # Persist immediately: network failure must not lose a successfully stored credential.
            saved = catalog.put('model-key', key_id, item, 0)
        detected = await discover(saved)
        with credential_lifecycle.mutation():
            return clean(catalog.put('model-key', key_id, detected, saved['revision']))


class CatalogReasoning(BaseModel):
    model_config=ConfigDict(extra='forbid')
    levels: list[Literal['none','enabled','minimal','low','medium','high','xhigh','max']]=Field(default_factory=list,max_length=8)
    parameter: Literal['effort','thinking']='effort'


class CatalogModel(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str=Field(min_length=1,max_length=200)
    display_name: str=Field(default='',max_length=100)
    capabilities: list[modules.Capability]=Field(default_factory=list,max_length=6)
    input_modalities: list[Literal['text','image','audio','video','document']]=Field(default_factory=list,max_length=5)
    output_modalities: list[Literal['text','image','audio','video','document']]=Field(default_factory=list,max_length=5)
    reasoning: CatalogReasoning | None=None


class KeyConfiguration(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision: int=Field(ge=0)
    name: str=Field(min_length=1,max_length=100)
    base_url: str=Field(min_length=1,max_length=2000)
    provider_id: str=Field(default='custom',max_length=64)
    protocol: Literal['openai','anthropic','gemini']='openai'
    catalog_mode: Literal['service','custom','preset']='service'
    models: list[CatalogModel]=Field(default_factory=list,max_length=3000)
    api_key: SecretStr=Field(default=SecretStr(''),max_length=10000)


@catalog.router.put('/model-keys/{key_id}')
async def configure(key_id: str, body: KeyConfiguration):
    async with _lock:
        with credential_lifecycle.mutation():
            item=get(key_id)
            if item['revision']!=body.expected_revision: raise HTTPException(409,'Key 配置已变化，请重新读取')
            base=valid_url(body.base_url)
            old_base=item['base_url']
            custom=[normalized_model(value.model_dump(exclude_none=True),'manual') for value in body.models]
            if any(not value['id'] for value in custom): raise HTTPException(422,'模型 ID 不可为空')
            if len({m['id'] for m in custom})!=len(custom): raise HTTPException(422,'模型 ID 不可重复')
            secret=body.api_key.get_secret_value().strip()
            if secret:
                try: credential_lifecycle.save_key(item['credential_ref'],secret)
                except Exception: raise HTTPException(503,'凭据保存失败，原配置已保留') from None
                credential_lifecycle.invalidate_discovery(item['credential_ref'],excluding_key=key_id)
                item.update(models=[],discovered_models=[],connected=None,discovered_at='')
            changed=base!=old_base or body.protocol!=item.get('protocol','openai')
            if changed: item.update(models=[],discovered_models=[],connected=None,discovered_at='')
            item.update(name=body.name.strip(),base_url=base,provider_id=body.provider_id,protocol=body.protocol,catalog_mode=body.catalog_mode,custom_models=custom)
            if body.catalog_mode=='preset':
                from app.reasoning import describe
                item['custom_models']=[{'id':mid,'display_name':mid,**provider_catalog.documented(mid),'capability_status':'unverified','reasoning':describe(mid)} for mid in provider_catalog.preset(body.provider_id)['models']]
            item['models']=item.get('discovered_models',item['models']) if body.catalog_mode=='service' else item['custom_models']
            item['note']='自定义模型目录；能力与档位以你的声明为准' if body.catalog_mode=='custom' else '官方资料预设目录；不代表当前 Key 可访问全部模型' if body.catalog_mode=='preset' else item.get('note','')
            saved=catalog.put('model-key',key_id,item,body.expected_revision)
            if changed:
                for kind in ('model-profile','module'):
                    for linked in catalog.listing(kind):
                        if linked.get('credential_ref')==item['credential_ref'] and linked.get('base_url')==old_base:
                            document={k:v for k,v in linked.items() if k not in ('id','revision')}
                            document.update(base_url=base,protocol=body.protocol,provider_id=body.provider_id)
                            catalog.put(kind,linked['id'],document,linked['revision'])
        if secret:
            detected = await discover(saved)
            with credential_lifecycle.mutation():
                saved=catalog.put('model-key',key_id,detected,saved['revision'])
        return clean(saved)


@catalog.router.post('/model-keys/{key_id}/refresh')
async def refresh(key_id: str, body: Revision):
    async with _lock:
        item = get(key_id)
        if item['revision'] != body.expected_revision: raise HTTPException(409, 'Key 记录已变化，请刷新后再识别')
        detected = await discover(item)
        with credential_lifecycle.mutation():
            return clean(catalog.put('model-key', key_id, detected, body.expected_revision))


@catalog.router.put('/model-keys/{key_id}/key')
async def replace(key_id: str, body: ReplaceKey):
    key = body.api_key.get_secret_value().strip()
    if not key: raise HTTPException(422, 'Key 不能为空')
    async with _lock:
        with credential_lifecycle.mutation():
            item = get(key_id)
            if item['revision'] != body.expected_revision: raise HTTPException(409, 'Key 记录已变化，请刷新')
            try: credential_lifecycle.save_key(item['credential_ref'], key)
            except Exception: raise HTTPException(503, '系统凭据管理器保存失败') from None
            credential_lifecycle.invalidate_discovery(item['credential_ref'], excluding_key=key_id)
            # Old model access may no longer apply to a replacement key.
            item.update(models=[], discovered_models=[], connected=None, note='', discovered_at='')
            saved = catalog.put('model-key', key_id, item, body.expected_revision)
        detected = await discover(saved)
        with credential_lifecycle.mutation():
            return clean(catalog.put('model-key', key_id, detected, saved['revision']))


@catalog.router.delete('/model-keys/{key_id}')
async def remove(key_id: str, expected_revision: int):
    async with _lock:
        with credential_lifecycle.mutation():
            item = get(key_id)
            if item['revision'] != expected_revision: raise HTTPException(409, 'Key 记录已变化，请刷新')
            reference = item['credential_ref']
            if any(profile.get('credential_ref') == reference for profile in catalog.listing('model-profile')) or any(value.get('credential_ref') == reference for kind in ('module', 'plugin') for value in catalog.listing(kind)):
                raise HTTPException(409, '仍有模型配置、模态或插件使用此 Key，请先切换并删除关联配置')
            if not item.get('owned'): raise HTTPException(409, '此 Key 来自旧配置，请在旧模型配置中管理')
            import keyring
            try: credential_lifecycle.delete_key(reference)
            except keyring.errors.PasswordDeleteError: pass
            except Exception: raise HTTPException(503, '凭据移除失败，记录已保留') from None
            catalog.put('model-key', key_id, {**item, 'deleted': True}, expected_revision)
            return {'deleted': True}


class Selection(Revision):
    model: str = Field(min_length=1, max_length=200)
    module_id: str = Field(max_length=100)
    voice: str = Field(default='', max_length=200)
    allow_unverified: bool = False
    # An explicit input declaration, never derived from service/name metadata.
    native_video_input: bool = False


@catalog.router.post('/model-keys/{key_id}/assign')
async def select(key_id: str, body: Selection):
    async with _lock:
        with credential_lifecycle.mutation():
            item = get(key_id)
            needed = modules.KINDS.get(body.module_id)
            model = next((value for value in item['models'] if value['id'] == body.model), None)
            if not needed or not model: raise HTTPException(422, '请选择清单中的模型与有效模态；无法发现的服务可使用手动配置')
            if needed not in model['capabilities'] and not body.allow_unverified:
                raise HTTPException(409, '该模型的此项能力未知，需要明确选择按此模态使用')
            # Check module revision before creating/updating a shared profile.
            existing = next((module for module in modules.modules()['items'] if module['id'] == body.module_id), None)
            if existing['revision'] != body.expected_revision: raise HTTPException(409, '模态配置已变化，请刷新')
            pid = 'keymodel-' + hashlib.sha256((key_id + '\n' + body.model).encode()).hexdigest()[:32]
            try: profile = catalog.get('model-profile', pid)
            except HTTPException as error:
                if error.status_code != 404: raise
                profile = {'revision': 0, 'capabilities': []}
            capabilities = set(model['capabilities']) - {'video'}
            # Only a saved manual declaration or this explicit checkbox may add
            # native video. Reassignments must retain existing profile opt-ins.
            if model.get('capability_source') == 'manual' and 'video' in model['capabilities']:
                capabilities.add('video')
            if body.native_video_input:
                capabilities.add('video')
            capabilities.update(profile['capabilities'])
            document = modules.Module(base_url=item['base_url'], model=body.model, credential_ref=item['credential_ref'],
                protocol=item.get('protocol','openai'),provider_id=item.get('provider_id','custom'),
                capabilities=sorted(capabilities | {needed}),
                enabled=True, timeout=180 if needed in ('image', 'tts', 'stt') else 90).model_dump(exclude={'expected_revision'})
            document.update(name=(item['name'] + ' · ' + body.model)[:100], key_id=key_id,
                            capability_status='unverified', capability_source='manual')
            catalog.put('model-profile', pid, document, profile['revision'])
            return model_profiles.assign(body.module_id, model_profiles.Assignment(profile_id=pid,
                expected_revision=body.expected_revision, voice=body.voice))


class NewConfiguredKey(NewKey):
    models:list[CatalogModel]=Field(default_factory=list,max_length=3000)
    catalog_mode:Literal['service','custom','preset']='custom'
    creation_id:str=Field(pattern=r'^[a-f0-9]{32}$')


@catalog.router.post('/model-key-configured')
async def create_configured(body:NewConfiguredKey):
    base=valid_url(body.base_url)
    custom=[normalized_model(value.model_dump(exclude_none=True),'manual') for value in body.models]
    if len({value['id'] for value in custom})!=len(custom) or any(not value['id'] for value in custom):raise HTTPException(422,'模型 ID 不可重复或为空')
    key=body.api_key.get_secret_value().strip()
    if not key:raise HTTPException(422,'Key 不能为空')
    async with _lock:
        with credential_lifecycle.mutation():
            try:
                prior=catalog.get('model-key',body.creation_id)
            except HTTPException as error:
                if error.status_code!=404:raise
            else:
                import hmac
                if prior['base_url']!=base or not hmac.compare_digest(config.load_key(prior['credential_ref']) or '',key):raise HTTPException(409,'连接创建编号冲突，请重新打开窗口')
                return clean(prior)
            reference='modelkey_'+body.creation_id
            credential_lifecycle.save_key(reference,key,fresh=True)
            item={'name':body.name.strip(),'base_url':base,'credential_ref':reference,'owned':True,'models':custom,'custom_models':custom,'connected':None,'note':'模型目录已保存，调用能力待试用','discovered_at':'','provider_id':body.provider_id,'protocol':body.protocol,'catalog_mode':'custom'}
            return clean(catalog.put('model-key',body.creation_id,item,0))
