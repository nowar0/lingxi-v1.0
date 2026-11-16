"""Reusable model accounts; secrets remain exclusively in the OS keyring."""
from uuid import uuid4
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from fastapi import HTTPException
from app import catalog, config, modules, credential_lifecycle


class Profile(modules.Module):
    name: str = Field(min_length=1, max_length=100)
    api_key: SecretStr = Field(default=SecretStr(''), max_length=10000)


def public(item):
    return {k: v for k, v in item.items() if k != 'credential_ref'}


@catalog.router.get('/model-profiles')
def profiles():
    return {'items': [public(p) for p in catalog.listing('model-profile')]}


@catalog.router.post('/model-profiles')
@credential_lifecycle.atomic
def create(body: Profile):
    url = urlsplit(body.base_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise HTTPException(422, '请输入不含密钥的 HTTP(S) 基础地址')
    if body.local and url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(422, '本地免密服务只允许本机地址')
    if not body.model.strip() or not body.capabilities:
        raise HTTPException(422, '请输入具体模型名并选择能力')
    key = body.api_key.get_secret_value().strip()
    if not body.local and not key:
        raise HTTPException(422, '请直接在 API Key 输入框填写密钥')
    pid = uuid4().hex
    reference = 'profile_' + pid if key else ''
    document = body.model_dump(exclude={'api_key', 'expected_revision', 'credential_ref'})
    document.update(credential_ref=reference, enabled=True, capability_status='unverified', capability_source='manual')
    from app.generation_settings import validate,Settings as GenerationSettings
    validate(document,GenerationSettings(top_p=document.get('top_p'),stop=document.get('stop',[]),max_output_tokens=document.get('max_output_tokens'),expected_revision=0))
    if key:
        try:
            credential_lifecycle.save_key(reference, key, fresh=True)
        except Exception:
            raise HTTPException(503, '系统凭据管理器保存失败，请重试') from None
    return public(catalog.put('model-profile', pid, document, 0))


class VideoInputSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    native_video_input: bool


@catalog.router.put('/model-profiles/{pid}/video-input')
@credential_lifecycle.atomic
def save_video_input(pid: str, body: VideoInputSelection):
    item = catalog.get('model-profile', pid)
    if item.get('deleted'):
        raise HTTPException(409, '配置已删除，请刷新')
    if item['revision'] != body.expected_revision:
        raise HTTPException(409, '模型配置已变化，请刷新')
    capabilities = set(item.get('capabilities', ())) - {'video'}
    if body.native_video_input:
        capabilities.add('video')
    # This only records the user's input declaration. Protocol support remains
    # gated by CatalogModelRoutes; saving never verifies a provider or reads Key.
    document = {key: value for key, value in item.items() if key not in ('id', 'revision')}
    document.update(capabilities=sorted(capabilities), capability_status='unverified', capability_source='manual')
    saved = catalog.put('model-profile', pid, document, body.expected_revision)
    for linked in catalog.listing('module'):
        if linked.get('profile_id') != pid:
            continue
        linked_caps = set(linked.get('capabilities', ())) - {'video'}
        if body.native_video_input:
            linked_caps.add('video')
        fields = {key: value for key, value in linked.items() if key not in ('id', 'revision')}
        fields.update(capabilities=sorted(linked_caps), capability_status='unverified')
        catalog.put('module', linked['id'], fields, linked['revision'])
    return public(saved)


class Key(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: SecretStr = Field(min_length=1, max_length=10000)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    temperature: float = Field(ge=0, le=2)
    timeout: int = Field(ge=5, le=300)
    capabilities: list[modules.Capability] = Field(min_length=1, max_length=6)
    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    protocol: str | None = Field(default=None, pattern=r'^(openai|anthropic|gemini)$')


@catalog.router.put('/model-profiles/{pid}')
@credential_lifecycle.atomic
def update(pid: str, body: ProfileUpdate):
    return update_configuration(pid,body)


@credential_lifecycle.atomic
def update_configuration(pid: str, body: ProfileUpdate, *, generation_override=None):
    from app import model_references
    item = catalog.get('model-profile', pid)
    if item.get('deleted') or item['revision'] != body.expected_revision:
        raise HTTPException(409, '模型配置已变化，请刷新')
    if not body.name.strip(): raise HTTPException(422, '请输入配置名称')
    linked = [m for m in catalog.listing('module') if m.get('profile_id') == pid]
    new_url = body.base_url if body.base_url is not None else item['base_url']
    new_model = body.model if body.model is not None else item['model']
    new_protocol = body.protocol or item.get('protocol', 'openai')
    if not new_model.strip(): raise HTTPException(422, '请输入模型 ID')
    url = urlsplit(new_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise HTTPException(422, '请输入不含密钥的 HTTP(S) 基础地址')
    if item.get('local') and url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(422, '本地模型只允许本机地址')
    if item.get('local_service_id') and new_model != item['model']:
        raise HTTPException(409, '此模型来自本地服务，请从服务模型列表选择模型')
    if (item.get('key_id') or item.get('local_service_id')) and (new_url != item['base_url'] or new_protocol != item.get('protocol', 'openai')):
        raise HTTPException(409, '此模型来自已保存的服务，请在对应服务中修改连接')
    if new_model != item['model']:
        routes = {'profile_' + pid + '/' + item['model']} | {'module_' + m['id'] + '/' + item['model'] for m in linked}
        model_references.assert_unused(routes)
    for module in linked:
        if modules.KINDS.get(module['id'], 'text') not in body.capabilities:
            raise HTTPException(409, '仍有功能需要此模型能力，请先更换对应的模型分配')
    model_references.assert_idle()
    doc = {k: v for k, v in item.items() if k not in ('id', 'revision')}
    from app.generation_settings import supported
    if body.temperature!=item.get('temperature',.7) and not supported({**item,'model':new_model})['temperature']:raise HTTPException(422,'当前模型不支持温度覆盖')
    doc.update(name=body.name.strip(), temperature=body.temperature, timeout=body.timeout,
               capabilities=list(dict.fromkeys(body.capabilities)), enabled=True, capability_status='unverified',
               base_url=new_url, model=new_model.strip(), protocol=new_protocol)
    from app.generation_settings import validate,Settings as GenerationSettings
    if generation_override is not None:
        doc.update({k:generation_override[k] for k in ('top_p','stop','max_output_tokens') if k in generation_override})
    validate(doc,GenerationSettings(top_p=doc.get('top_p'),stop=doc.get('stop',[]),max_output_tokens=doc.get('max_output_tokens'),expected_revision=body.expected_revision))
    if set(item.get('capabilities',[]))-set(doc['capabilities']):
        model_references.assert_unused({'profile_'+pid+'/'+item['model']},[pid])
    saved = catalog.put('model-profile', pid, doc, body.expected_revision)
    for module in linked:
        value = {k: v for k, v in module.items() if k not in ('id', 'revision')}
        value.update(temperature=body.temperature, timeout=body.timeout, capabilities=doc['capabilities'],
                     base_url=new_url, model=doc['model'], protocol=new_protocol)
        catalog.put('module', module['id'], value, module['revision'])
    return public(saved)


@catalog.router.put('/model-profiles/{pid}/key')
@credential_lifecycle.atomic
def replace_key(pid: str, body: Key):
    key = body.api_key.get_secret_value().strip()
    if not key:
        raise HTTPException(422, 'Key 不能为空')
    item = catalog.get('model-profile', pid)
    if item.get('deleted') or not item['credential_ref']:
        raise HTTPException(409, '此配置未使用 Key，请新增模型配置')
    if item.get('key_id'):
        raise HTTPException(409, '此模型使用共享 Key，请在 Key 库更换，其他模态会同步使用新 Key')
    try:
        credential_lifecycle.save_key(item['credential_ref'], key)
    except Exception:
        raise HTTPException(503, '系统凭据管理器保存失败') from None
    credential_lifecycle.invalidate_discovery(item['credential_ref'])
    return {'saved': True}


@catalog.router.delete('/model-profiles/{pid}')
@credential_lifecycle.atomic
def remove(pid: str, expected_revision: int):
    from app import model_references
    item = catalog.get('model-profile', pid)
    if item.get('deleted'):
        raise HTTPException(409, '配置已删除，请刷新')
    if item['revision'] != expected_revision:
        raise HTTPException(409, '配置已变化，请刷新')
    model_references.assert_idle()
    model_references.assert_unused({'profile_' + pid + '/' + item.get('model', '')}, [pid])
    for m in catalog.listing('module'):
        if m.get('profile_id') == pid or (not m.get('profile_id') and item['credential_ref'] and m.get('credential_ref') == item['credential_ref']):
            raise HTTPException(409, '仍有模块使用此配置，请先为这些模块分配其他模型')
    keep_key = item['credential_ref'] and credential_lifecycle.retain_after_profile_removal(item['credential_ref'], pid)
    if item['credential_ref'] and not item.get('key_id') and not keep_key:
        import keyring
        try:
            credential_lifecycle.delete_key(item['credential_ref'])
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception:
            raise HTTPException(503, '凭据移除失败，模型配置已保留') from None
    return public(catalog.put('model-profile', pid, {**item, 'deleted': True}, expected_revision))


class Assignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str
    expected_revision: int = Field(ge=0)
    enabled: bool = True
    voice: str = Field(default='', max_length=200)


@catalog.router.put('/model-assignments/{module_id}')
@credential_lifecycle.atomic
def assign(module_id: str, body: Assignment):
    item = catalog.get('model-profile', body.profile_id)
    needed = modules.KINDS.get(module_id)
    if item.get('deleted') or not needed or needed not in item['capabilities']:
        raise HTTPException(422, '所选模型不支持该模块所需能力')
    fields = {k: item[k] for k in modules.Module.model_fields if k in item}
    fields.update(expected_revision=body.expected_revision, enabled=body.enabled, voice=body.voice, profile_id=body.profile_id)
    return modules.save_module(module_id, modules.Module(**fields))
