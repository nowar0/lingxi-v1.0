"""Model-specific effort metadata. Never probe paid completions to discover ranges."""
import re
from urllib.parse import urlsplit
from fastapi import HTTPException

EFFORTS = ('none', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
SETTINGS = ('auto', *EFFORTS)
SOURCE = 'https://developers.openai.com/api/docs/guides/reasoning'


def describe(model, metadata=None, legacy_support=False):
    metadata = metadata if isinstance(metadata, dict) else {}
    reasoning = metadata.get('reasoning', {})
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    family = family_name(model)
    declared = metadata.get('capabilities', {})
    declared = declared if isinstance(declared, dict) else {}
    effort = metadata.get('effort', {})
    effort = effort if isinstance(effort, dict) else {}
    effort_caps = declared.get('effort', {})
    effort_caps = effort_caps if isinstance(effort_caps, dict) else {}
    supported = [key for key, value in effort_caps.items() if isinstance(value, dict) and value.get('supported') is True]
    candidates = [metadata.get('supported_reasoning_efforts'), metadata.get('reasoning_efforts'),
                  metadata.get('reasoning_effort'), reasoning.get('efforts'), reasoning.get('effort')]
    if effort.get('supported_levels') is not None: candidates.insert(0, effort['supported_levels'])
    if supported: candidates.insert(0, supported)
    for candidate in candidates:
        if isinstance(candidate, dict): candidate = candidate.get('enum', candidate.get('values'))
        if isinstance(candidate, list):
            levels = [value for value in EFFORTS if value in candidate]
            if family == 'deepseek' and levels and 'none' not in levels: levels.insert(0, 'none')
            # An explicit empty list is a real no-effort declaration.
            if not candidate or levels:
                return {'levels': levels, 'source': 'service', 'note': '服务返回的档位；仍需实际调用确认',
                        'family': family, 'parameter': 'effort'}
    if reasoning.get('supported') is False or metadata.get('supports_reasoning') is False:
        return {'levels': [], 'source': 'service', 'note': '服务声明此模型不支持可调思考强度', 'family': 'compatible'}
    # Retain only sanitized discovery metadata; no raw model records or credentials.
    if metadata.get('source') in ('service', 'rule', 'legacy', 'manual') and isinstance(metadata.get('levels'), list):
        return {'levels': [value for value in EFFORTS if value in metadata['levels']], 'source': metadata['source'],
                'note': str(metadata.get('note', ''))[:200], 'family': family,
                'parameter': metadata.get('parameter', 'effort') if metadata.get('parameter') in ('effort','thinking') else 'effort'}
    name = str(model).lower().split('/')[-1]
    name = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', name)
    levels = None
    if name in ('gpt-6-astra', 'gpt-6.1-sol'):
        levels = ['low', 'medium', 'high', 'xhigh', 'max']
    elif name in ('gpt-6-sol', 'gpt-6-luna', 'gpt-5.6', 'gpt-5.6-sol'):
        levels = ['none', 'low', 'medium', 'high', 'xhigh', 'max']
    elif name == 'gpt-5': levels = ['minimal', 'low', 'medium', 'high']
    elif name == 'gpt-5.1': levels = ['none', 'low', 'medium', 'high']
    elif name in ('gpt-5.2', 'gpt-5.4', 'gpt-5.5'): levels = ['none', 'low', 'medium', 'high', 'xhigh']
    elif name == 'gpt-5.3-codex': levels = ['low', 'medium', 'high', 'xhigh']
    elif name.startswith(('gpt-4', 'gpt-image', 'dall-e', 'whisper')): levels = []
    if levels is not None:
        return {'levels': levels, 'source': 'rule', 'note': '按模型规则匹配；兼容服务可用其元数据覆盖', 'family': 'openai', 'documentation': SOURCE}
    parameter='effort'
    if name.startswith(('grok-4.7','grok-4.6')): levels=['low','medium','high','xhigh']
    elif name.startswith('grok-4.5'): levels=['low','medium','high']
    elif name.startswith('gemini-4-argon'): levels=['low','medium','high']
    elif name.startswith(('gemini-3.8-flash','gemini-3.6-flash','gemini-3.5-flash-lite','gemini-3.1-flash-lite','gemini-3-flash')) and not any(s in name for s in ('tts','image','live')): levels=['minimal','low','medium','high']
    elif name.startswith('gemini-2.5-pro'): levels=['minimal','low','medium','high']
    elif name.startswith('gemini-2.5-flash') and not any(s in name for s in ('tts','image','live')): levels=['none','minimal','low','medium','high']
    elif name=='deepseek-flash' or name.startswith('deepseek-v4'): levels=['none','low','high','max']
    elif name.startswith('kimi-k3.1-pro'): levels=['low','high','max']
    elif name.startswith('kimi-k2.6'): levels=['none','enabled']; parameter='thinking'
    elif name.startswith('glm-5.3'): levels=['low','high','max']
    elif name.startswith('glm-5.2'): levels=['none','high','max']
    elif name.startswith(('glm-5.1','glm-5v-turbo','glm-4.7','glm-4.6','glm-4.5')): levels=['none','enabled']; parameter='thinking'
    elif name.startswith(('claude-opus-5','claude-sonnet-5','claude-haiku-5-5','claude-fable-5','claude-mythos-5','claude-opus-4-7','claude-opus-4-8')): levels=['low','medium','high','xhigh','max']
    elif name.startswith(('claude-opus-4-6','claude-sonnet-4-6')): levels=['low','medium','high','max']
    elif name.startswith('claude-opus-4-5'): levels=['low','medium','high']
    if levels is not None:
        from app.provider_catalog import preset
        provider = {'grok':'xai','claude':'anthropic','gemini':'gemini','deepseek':'deepseek','kimi':'kimi','glm':'glm'}.get(family,'custom')
        return {'levels':levels,'source':'rule','note':'官方文档规则；服务元数据优先，实际调用仍需确认',
                'family':family,'parameter':parameter,'documentation':preset(provider)['documentation']}
    if legacy_support:
        return {'levels': ['none', 'minimal', 'low', 'medium', 'high', 'max'], 'source': 'legacy',
                'note': '沿用项目已有的服务支持声明', 'family': 'compatible'}
    return {'levels': [], 'source': 'unknown', 'note': '服务未返回可用档位，保留自动以避免发送未知参数', 'family': family}


def openai_model(model):
    return bool(re.match(r'^(gpt-[56]|o[134](?:-|$))', str(model).lower().split('/')[-1]))


def family_name(model):
    name=str(model).lower().split('/')[-1]
    if openai_model(name): return 'openai'
    for prefix, family in [('grok-','grok'),('gemini-','gemini'),('deepseek-','deepseek'),('kimi-','kimi'),('claude-','claude'),('glm-','glm')]:
        if name.startswith(prefix): return family
    return 'compatible'


def for_module(item):
    from app import catalog
    metadata = {}
    try:
        if not item.get('profile_id'):
            raise HTTPException(404, '未关联配置')
        profile = catalog.get('model-profile', item.get('profile_id', ''))
        if not profile.get('key_id'):
            raise HTTPException(404, '未关联 Key 清单')
        key = catalog.get('model-key', profile.get('key_id', ''))
        entry = next((value for value in key.get('models', []) if value.get('id') == item.get('model')), {})
        metadata = entry.get('reasoning', {})
    except HTTPException as error:
        if error.status_code != 404: raise
    legacy = item.get('supports_effort', False) or urlsplit(item.get('base_url', '')).hostname == 'api.deepseek.com'
    return describe(item.get('model', ''), metadata, legacy)


def coerce(value, capability):
    return value if value in capability.get('levels', []) else 'auto'


def parameters(value, capability, protocol='openai'):
    value = coerce(value, capability)
    if value == 'auto': return {}
    if capability.get('parameter') == 'thinking' or capability.get('family') in ('deepseek','glm') and value == 'none':
        return {'thinking': {'type': 'disabled' if value=='none' else 'enabled'}}
    if protocol == 'anthropic':
        return {'output_config': {'effort': value}}
    extra={'reasoning_effort':value}
    if capability.get('family') == 'glm': extra['thinking']={'type':'enabled'}
    return extra
