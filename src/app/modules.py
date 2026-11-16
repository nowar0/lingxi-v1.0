"""Independently configured compatible model modules; no credentials in SQLite."""
from urllib.parse import urlsplit
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app import credential_lifecycle, catalog, config
from app.credential_refs import validate_reference, looks_like_secret

KINDS = {'companion': 'text', 'work': 'text', 'orchestrator': 'text',
         'vision': 'vision', 'image': 'image', 'stt': 'stt', 'tts': 'tts'}
# video is native video INPUT, separate from image input (vision) and generation.
Capability = Literal['text', 'vision', 'video', 'image', 'stt', 'tts']

def request_timeout(module_id,default=180):
    """Standalone engine adapters also work before the catalog is initialized."""
    import sqlite3
    try:return catalog.get('module',module_id).get('timeout',default)
    except HTTPException as error:
        if error.status_code!=404:raise
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):raise
    return default


class Module(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    base_url: str = Field(default='', max_length=2000)
    model: str = Field(default='', max_length=200)
    credential_ref: str = Field(default='', max_length=100)
    local: bool = False
    capabilities: list[Capability] = Field(default_factory=list)
    voice: str = Field(default='', max_length=200)
    difficulty: int = Field(default=1, ge=1, le=3)
    timeout: int = Field(default=90, ge=5, le=300)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float|None = Field(default=None,gt=0,le=1)
    stop: list[str] = Field(default_factory=list,max_length=4)
    max_output_tokens: int|None = Field(default=None,ge=128,le=131072)
    profile_id: str = Field(default='', max_length=100)
    provider_id: str = Field(default='custom', max_length=64)
    protocol: Literal['openai', 'anthropic', 'gemini'] = 'openai'
    reasoning_effort: Literal['auto', 'none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] = 'auto'
    expected_revision: int = Field(default=0, ge=0)

    @field_validator('credential_ref')
    @classmethod
    def reference_is_not_key(cls, value):
        return validate_reference(value)


def configured(module_id):
    try:
        item = catalog.get('module', module_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            if module_id in ('tts', 'stt'):
                from app.local_tts import native_module
                if module_id == 'stt':
                    from app.local_stt import native_module
                item = native_module()
            else:
                raise HTTPException(409, '该模块尚未配置') from exc
        else:
            raise
    if module_id == 'image' and item.get('model') == 'comfyui-local':
        raise HTTPException(409, '请选择生图 API 模型；ComfyUI 请通过插件、技能或 MCP 使用')
    needed = KINDS.get(module_id)
    if not item['enabled'] or not item['base_url'] or not item['model']:
        raise HTTPException(409, '该模块待配置或未启用')
    if needed and needed not in item['capabilities']:
        raise HTTPException(409, '配置未声明此模块所需的能力')
    if looks_like_secret(item.get('credential_ref', '')):
        raise HTTPException(409, '此配置的凭据引用曾误填 Key，请在模型与 Key 中重新保存并分配')
    return item


@catalog.router.get('/modules')
def modules():
    values = {i['id']: i for i in catalog.listing('module')}
    if 'tts' not in values:
        from app.local_tts import native_module
        values['tts'] = {'id': 'tts', 'revision': 0, **native_module()}
    if 'stt' not in values:
        from app.local_stt import native_module as offline_stt
        values['stt'] = {'id': 'stt', 'revision': 0, **offline_stt()}
    if 'image' not in values:
        values['image'] = {'id': 'image', 'revision': 0, 'enabled': False, 'local': False,
                           'base_url': '', 'model': '', 'credential_ref': '', 'capabilities': ['image']}
    elif values['image'].get('model') == 'comfyui-local':
        # Retain the old record for the user; never reactivate its removed core adapter.
        values['image'] = {**values['image'], 'enabled': False, 'local': False,
                           'base_url': '', 'model': '', 'credential_ref': '',
                           'note': '请选择生图 API 模型；ComfyUI 请通过插件、技能或 MCP 使用'}
    for item in values.values():
        if looks_like_secret(item.get('credential_ref', '')):
            item['credential_ref'] = ''
            item['credential_status'] = 'invalid_reference'
    cfg = config.load_models()
    defaults = {}
    for key in ('companion', 'work', 'orchestrator'):
        target = cfg.get('roles', {}).get(key, cfg.get('roles', {}).get('chat', ''))
        provider_id, _, model_id = target.partition('/')
        provider = next((p for p in cfg.get('providers', []) if p['id'] == provider_id), None)
        if provider:
            defaults[key] = {name: provider[name] for name in Module.model_fields if name in provider}
            defaults[key].update(enabled=True, base_url=provider['base_url'], model=model_id,
                                 credential_ref=provider.get('credential_ref') or provider_id,
                                 capabilities=provider.get('capabilities', ['text']))
    result = [{**Module().model_dump(exclude={'expected_revision'}), 'id': key,
                       'revision': 0, **defaults.get(key, {}), **values.get(key, {}),
                       'required_capability': capability} for key, capability in KINDS.items()]
    from app.reasoning import for_module
    for item in result:
        item['reasoning'] = for_module(item)
    return {'items': result}


class ReasoningSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    effort: Literal['auto', 'none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']


@catalog.router.put('/modules/{module_id}/reasoning')
@credential_lifecycle.atomic
def save_reasoning(module_id: str, body: ReasoningSelection):
    if KINDS.get(module_id) not in ('text', 'vision'):
        raise HTTPException(422, '此模态没有文字推理强度设置')
    item = next((value for value in modules()['items'] if value['id'] == module_id), None)
    if body.expected_revision != item['revision']:
        raise HTTPException(409, '模态配置已变化，请刷新')
    if body.effort != 'auto' and body.effort not in item['reasoning']['levels']:
        raise HTTPException(422, '当前模型不支持此档位，请重新选择')
    try: current = catalog.get('module', module_id)
    except HTTPException as error:
        if error.status_code != 404: raise
        current = {key: item[key] for key in Module.model_fields if key in item}
    document = {key: value for key, value in current.items() if key not in ('id', 'revision', 'expected_revision')}
    document['reasoning_effort'] = body.effort
    catalog.put('module', module_id, document, body.expected_revision)
    return next(value for value in modules()['items'] if value['id'] == module_id)


def chat_role(mode):
    values = {i['id']: i for i in catalog.listing('module')}
    if mode not in values:
        roles = config.load_models().get('roles', {})
        return mode if mode in roles else 'chat'
    configured(mode)
    return mode


@catalog.router.put('/modules/{module_id}')
@credential_lifecycle.atomic
def save_module(module_id: str, body: Module):
    from app.generation_settings import validate,Settings
    validate(body.model_dump(),Settings(top_p=body.top_p,stop=body.stop,max_output_tokens=body.max_output_tokens,expected_revision=body.expected_revision))
    if module_id not in KINDS and not module_id.startswith('alternate_'):
        raise HTTPException(422, '未知模块')
    if module_id == 'image' and body.model == 'comfyui-local':
        raise HTTPException(422, '请选择生图 API 模型；ComfyUI 请通过插件、技能或 MCP 使用')
    if body.base_url:
        url = urlsplit(body.base_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise HTTPException(422, '地址必须是不含凭据或查询参数的 HTTP(S) 基础地址')
        if body.local and url.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise HTTPException(422, '免凭据本地服务只允许本机地址')
    if body.enabled and (not body.model or not body.base_url or
                         KINDS.get(module_id, 'text') not in body.capabilities):
        raise HTTPException(422, '启用前请填写地址、模型和所需能力')
    if module_id == 'tts':
        from app import local_tts
        if body.enabled and local_tts.is_native(body.model_dump()):
            local_tts.select_voice(local_tts.settings(), body.voice)
    if module_id == 'stt':
        from app import local_stt
        if body.enabled and local_stt.is_native(body.model_dump()):
            missing = local_stt.issues(local_stt.settings())
            if missing:
                raise HTTPException(409, '；'.join(missing))
    document = body.model_dump(exclude={'expected_revision'})
    document['connection_status'] = 'untested'
    document['capability_status'] = 'untested'
    return catalog.put('module', module_id, document, body.expected_revision)


class Credential(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: SecretStr = Field(min_length=1, max_length=10000)


@catalog.router.put('/credentials/{reference}')
@credential_lifecycle.atomic
def credential(reference: str, body: Credential):
    if not reference or len(reference) > 100:
        raise HTTPException(422, '凭据引用限 1～100 字')
    try:
        validate_reference(reference)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        credential_lifecycle.save_key(reference, body.value.get_secret_value())
    except Exception:
        raise HTTPException(503, '系统凭据管理器保存失败，请重试') from None
    return {'saved': True, 'reference': reference}


def headers(item):
    if item['local'] and not item['credential_ref']:
        return {}
    key = config.load_key(item['credential_ref']) if item['credential_ref'] else None
    if not key:
        raise HTTPException(409, '请在“模型与 Key”保存 API Key，再把模型分配到此模块')
    if item.get('protocol')=='anthropic': return {'x-api-key':key,'anthropic-version':'2023-06-01'}
    return {'Authorization': 'Bearer ' + key}


@catalog.router.post('/modules/{module_id}/test')
async def test_connection(module_id: str):
    import httpx2
    item = configured(module_id)
    from app import local_tts
    if module_id == 'tts' and local_tts.is_native(item):
        local_tts.select_voice(local_tts.settings(), item.get('voice', ''))
        return {'connected': True, 'capability_verified': False,
                'note': '安装目录、角色权重和参考录音检查通过；请试听验证实际音色'}
    from app import local_stt
    if module_id == 'stt' and local_stt.is_native(item):
        missing = local_stt.issues(local_stt.settings())
        return {'connected': not missing, 'capability_verified': False,
                'note': '；'.join(missing) if missing else '离线模型与运行环境齐全；请录音验证实际识别'}
    async with httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
        try:
            from app.llm.anthropic_bridge import endpoint
            address=endpoint(item['base_url'],'models') if item.get('protocol')=='anthropic' else item['base_url'].rstrip('/')+'/models'
            response = await client.get(address, headers=headers(item))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('data', payload.get('models')), list):
                raise ValueError('Not a compatible model list')
        except (httpx2.HTTPError, ValueError) as exc:
            raise HTTPException(502, '连接测试失败；检查接口地址及凭据。部分服务不提供 /models。') from exc
    return {'connected': True, 'capability_verified': False,
            'note': '服务可访问；需提交实际样例才能验证具体能力'}


class Trial(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prompt:str=Field(default='请用一句话回复：连接正常。',min_length=1,max_length=1000)


@catalog.router.post('/modules/{module_id}/trial')
async def trial_model(module_id:str,body:Trial):
    item=configured(module_id)
    if 'text' not in item.get('capabilities',[]):raise HTTPException(422,'此入口仅试用文字模型，请从相应功能试用图片或语音')
    from app.llm import router
    from contextlib import aclosing
    from app import usage
    text='';token=router.output_limit.set(128)
    try:
        with usage.scope(session_id=None,job_id='',node_id='',agent_id='',kind='model_trial'):
            async with aclosing(router.chat_stream(module_id,[{'role':'user','content':body.prompt}],tools=None)) as stream:
                async for kind,value in stream:
                    if kind=='answer':text+=value
        if not text.strip():raise HTTPException(502,'模型未返回文字，请检查配置')
        return {'text':text,'verified':'text','tools_verified':False,'vision_verified':False}
    finally:router.output_limit.reset(token)
