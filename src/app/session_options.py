"""Versioned session overrides, resolved above presets and global modules."""
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, sessions

temperature = ContextVar('lingxi_session_temperature', default=None)


@contextmanager
def generation_scope(sid):
    from app.llm import router
    from app.extensions import optional
    selected=get(sid);preset=optional('agent-binding',str(sid)).get('preset') or {}
    value={'temperature':selected['temperature'] if selected['temperature'] is not None else preset.get('temperature'),
           'effort':selected['effort'] if selected['effort']!='auto' else preset.get('effort')}
    if value['effort']=='auto':value['effort']=None
    temp_token=temperature.set(value['temperature']);effort_token=router.effort_level.set(value['effort'])
    from app import usage
    try:
        with usage.scope(session_id=sid):yield
    finally:temperature.reset(temp_token);router.effort_level.reset(effort_token)


class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(default='', max_length=500)
    temperature: float | None = Field(default=None, ge=0, le=2)
    effort: Literal['auto', 'none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] = 'auto'
    tool_groups: list[str] | None = Field(default=None, max_length=20)
    voice_engine: Literal['', 'gpt-sovits-v2', 'cosyvoice2', 'indextts2', 'indextts2.5'] = ''
    auto_read: bool = False
    expected_revision: int = Field(default=0, ge=0)


def get(sid):
    if not sessions.get_session(sid):
        raise HTTPException(404, '会话不存在')
    try:
        return {**Options().model_dump(exclude={'expected_revision'}), **catalog.get('session-options', f'session_{sid}')}
    except HTTPException as error:
        if error.status_code != 404:
            raise
    from app.customer_management import conversation_defaults
    defaults=Options().model_dump(exclude={'expected_revision'})
    defaults.update({k:v for k,v in conversation_defaults(sessions.get_session(sid)['mode']).items() if k in ('voice_engine','auto_read')})
    return {**defaults, 'id': f'session_{sid}', 'revision': 0}


def effective(sid):
    from app import config, modules
    from app.extensions import optional
    session = sessions.get_session(sid)
    options = get(sid)
    preset = (optional('agent-binding', str(sid)).get('preset') or {}) if session['mode'] == 'work' else {}
    from app.llm import router
    configured_roles=router.list_roles()
    role=session['mode'] if session['mode'] in configured_roles else 'chat'
    try:
        base, _ = router.resolve(role)
    except ValueError:
        base = {}
    selected = options['model'] or preset.get('model') or role
    effort = options['effort'] if options['effort'] != 'auto' else preset.get('effort', base.get('reasoning_effort', 'auto'))
    temperature_supported=True
    resolved_model=selected
    try:
        from app import reasoning
        provider,model=router.resolve(selected)
        resolved_model=provider.get('id','')+'/'+model
        capability=provider.get('reasoning') or reasoning.describe(model,provider,provider.get('supports_effort',False))
        effort=reasoning.coerce(effort,capability)
        parameters=reasoning.parameters(effort,capability,provider.get('protocol','openai'))
        temperature_supported=capability.get('family') not in ('gemini','kimi') and (
            not (capability.get('family')=='openai' and reasoning.openai_model(model)) or parameters.get('reasoning_effort')=='none')
    except ValueError:
        capability={'levels':[]}
    return {'settings': options, 'effective': {'model': selected, 'resolved_model':resolved_model,'effort': effort,
        'temperature': (options['temperature'] if options['temperature'] is not None else preset.get('temperature') if preset.get('temperature') is not None else base.get('temperature', .7)) if temperature_supported else None,
        'tool_groups': options['tool_groups'], 'voice_engine': options['voice_engine'], 'auto_read': options['auto_read']},
        'parameter_support':{'temperature':temperature_supported,'effort_levels':capability.get('levels',[])},
        'sources': {'model': 'session' if options['model'] else 'preset' if preset.get('model') else 'module',
                    'effort': 'session' if options['effort'] != 'auto' else 'preset' if preset.get('effort') else 'module',
                    'temperature': 'session' if options['temperature'] is not None else 'preset' if preset.get('temperature') is not None else 'module',
                    'tool_groups': 'session' if options['tool_groups'] is not None else 'preset/module',
                    'voice_engine': 'session' if options['voice_engine'] else 'module', 'auto_read': 'session'}}


@catalog.router.get('/sessions/{sid}/options')
def read_options(sid: int):
    return effective(sid)


@catalog.router.put('/sessions/{sid}/options')
def save(sid: int, body: Options):
    current=get(sid)
    from app.db import connect
    from app.extensions import busy
    generation_fields=('model','temperature','effort','tool_groups')
    with connect() as db:
        if any(getattr(body,key)!=current[key] for key in generation_fields) and busy(db,sid):
            raise HTTPException(409,'请先停止当前会话与任务，再修改生成配置')
    if body.model:
        from app.llm import router
        allowed = set(router.list_models()) | set(router.list_roles())
        allowed.update('module_' + item['id'] + '/' + item['model'] for item in router.module_models())
        if body.model not in allowed:
            raise HTTPException(422, '模型不在当前可用清单中，请刷新后选择')
        router.resolve(body.model)
    if body.tool_groups is not None:
        from app.tools.registry import registry
        groups = {item.group for item in registry.entries.values() if item.kind == 'tool'}
        if len(set(body.tool_groups)) != len(body.tool_groups) or not set(body.tool_groups) <= groups:
            raise HTTPException(422, '工具组不可用或重复')
    if body.effort!='auto':
        from app.llm import router
        from app import reasoning
        from app.extensions import optional
        session=sessions.get_session(sid)
        preset=(optional('agent-binding',str(sid)).get('preset') or {}) if session['mode']=='work' else {}
        role=body.model or preset.get('model') or (session['mode'] if session['mode'] in router.list_roles() else 'chat')
        try:
            provider,model=router.resolve(role)
            capability=provider.get('reasoning') or reasoning.describe(model,provider,provider.get('supports_effort',False))
            if reasoning.coerce(body.effort,capability)!=body.effort:raise ValueError('不支持')
        except ValueError:raise HTTPException(422,'当前模型不支持该思考档位；请选择自动或保存新模型后刷新可用参数') from None
    if body.voice_engine:
        from app.speech.registry import ENGINES
        if body.voice_engine not in ENGINES or ENGINES[body.voice_engine].kind!='tts':raise HTTPException(422,'语音引擎尚未登记')
    catalog.put('session-options', f'session_{sid}', body.model_dump(exclude={'expected_revision'}), body.expected_revision)
    return effective(sid)


def tool_context(ctx):
    from app.evolution_api import settings
    sid = ctx.get('session_id')
    if sid:
        value = get(sid)
        from app.composer_controls import get_state
        controls=get_state(sid)
        ctx = {**ctx, 'tool_groups': value['tool_groups'], 'read_only': controls['plan_mode'] or controls['permission']=='readonly'}
    return {**ctx, 'domain_tools': settings()['domain_tools']}


@catalog.router.get('/sessions/{sid}/options/catalog')
def options_catalog(sid:int):
    get(sid)
    from app.llm import router
    from app.tools.registry import registry
    values=list(dict.fromkeys([*router.list_roles(),*router.list_models()]))
    return {'models':values,'groups':sorted({item.group for item in registry.entries.values() if item.kind=='tool'})}
