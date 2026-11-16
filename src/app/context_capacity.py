"""Per-service/model context budgets with explicit evidence and a safe fallback."""
import hashlib
import json
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog


def service_capacity(value):
    # Only explicit numeric metadata is evidence. Never infer from a model name.
    for key in ('context_window', 'context_length', 'max_context_tokens', 'input_token_limit', 'inputTokenLimit'):
        number = value.get(key)
        if type(number) is int and 2048 <= number <= 2000000:
            return number
    return None


def identity(provider, model):
    return hashlib.sha256(json.dumps([provider.get('base_url', '').rstrip('/'),
        provider.get('protocol', 'openai'), model], ensure_ascii=False).encode()).hexdigest()


def describe(provider, model, fallback=32768):
    key = identity(provider, model)
    try:
        configured = catalog.get('context-capacity', key)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        configured = {'revision': 0, 'tokens': None}
    confirmed = None
    for service in catalog.listing('model-key'):
        if (service.get('base_url', '').rstrip('/') != provider.get('base_url', '').rstrip('/')
                or service.get('protocol', 'openai') != provider.get('protocol', 'openai')):
            continue
        records = service.get('discovered_models', service.get('models', []) if service.get('catalog_mode', 'service') == 'service' else [])
        values = [service_capacity(record) for record in records if record.get('id') == model]
        values = [v for v in values if v is not None]
        if values:
            confirmed = min(values + ([confirmed] if confirmed else []))
    manual = configured.get('tokens')
    effective = min(manual, confirmed) if manual and confirmed else manual or confirmed or fallback
    return {'id': key, 'model': model, 'tokens': effective, 'manual_tokens': manual,
            'service_tokens': confirmed, 'revision': configured['revision'],
            'source': 'manual' if manual and effective == manual else 'service' if confirmed else 'fallback',
            'notice': '服务元数据声明容量；未通过极限请求实测' if confirmed else '服务未提供容量；使用手动值或全局后备容量，未自动确认'}


def for_route(route, fallback=32768):
    from app.llm import router
    provider, model = router.resolve(route)
    return describe(provider, model, fallback)


def settings(sid, db=None, route=None):
    from app.memory import stage7
    from app.session_options import effective
    cfg = stage7.settings(db)
    try:
        capacity = for_route(route or effective(sid)['effective']['model'], cfg['context_tokens'])
    except ValueError:
        capacity = {'tokens': cfg['context_tokens'], 'source': 'fallback', 'notice': '尚未配置模型，使用全局后备容量'}
    cfg = {**cfg, 'context_tokens': capacity['tokens'], 'context_capacity': capacity}
    names = ('reply_reserve_tokens', 'tool_reserve_tokens', 'memory_budget_tokens')
    total = sum(cfg[name] for name in names)
    # Small models must retain room for input. Persisted user defaults are unchanged.
    limit = int(cfg['context_tokens'] * .75)
    if total > limit:
        cfg.update({name: int(cfg[name] * limit / total) for name in names})
    return cfg


class Capacity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    route: str = Field(min_length=1, max_length=500)
    tokens: int | None = Field(default=None, ge=2048, le=2000000, strict=True)
    expected_revision: int = Field(ge=0)


@catalog.router.get('/context-capacities')
def listing():
    from app.llm import router
    from app.memory import stage7
    routes = sorted(set(router.list_models()) | set(router.list_roles()))
    records = []
    for route in routes:
        try:
            records.append({**for_route(route, stage7.settings()['context_tokens']), 'route': route})
        except ValueError:
            continue
    return {'items': records}


@catalog.router.put('/context-capacities')
def save(body: Capacity):
    from app.llm import router
    from app.model_references import assert_idle
    assert_idle()
    try:
        provider, model = router.resolve(body.route)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    current = describe(provider, model)
    if body.tokens and current['service_tokens'] and body.tokens > current['service_tokens']:
        raise HTTPException(422, '手动容量不能超过服务已声明的容量')
    catalog.put('context-capacity', current['id'], {'tokens': body.tokens}, body.expected_revision)
    return for_route(body.route)


@catalog.router.get('/sessions/{sid}/context-capacity')
def session_capacity(sid: int):
    value = settings(sid)
    return {key: value[key] for key in ('context_tokens', 'context_capacity', 'reply_reserve_tokens',
        'tool_reserve_tokens', 'memory_budget_tokens')}
