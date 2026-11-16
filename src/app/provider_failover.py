"""Explicit, versioned generation-only fallback chains; never replays tools."""
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

import httpx2
from openai import APIConnectionError, APIStatusError, APITimeoutError
from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, config
from app.db import connect


class ProviderHTTPError(RuntimeError):
    def __init__(self, status):
        self.status_code = status
        super().__init__(f'模型服务返回 HTTP {status}，请检查协议、模型和参数')


def retry_reason(error):
    if isinstance(error, (TimeoutError, APITimeoutError, httpx2.TimeoutException)):
        return 'timeout'
    if isinstance(error, (APIConnectionError, httpx2.NetworkError)):
        return 'connection'
    if isinstance(error, (APIStatusError, httpx2.HTTPStatusError, ProviderHTTPError)):
        status = getattr(error, 'status_code', None)
        if status is None and getattr(error, 'response', None) is not None:
            status = error.response.status_code
        if status == 429:
            return 'rate_limit'
        if status is not None and 500 <= status <= 599:
            return 'server_error'
    return None


class Chain(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    fallbacks: list[str] = Field(default_factory=list, max_length=3)
    expected_revision: int = Field(default=0, ge=0)


def settings(role):
    try:
        return catalog.get('provider-failover', role)
    except HTTPException as error:
        if error.status_code != 404:
            raise
    except sqlite3.OperationalError as error:
        # Legacy model-only callers can initialize usage without the catalog.
        # Absence means opt-out; other database failures must remain visible.
        if 'no such table: capability_documents' not in str(error):
            raise
    return {'id': role, 'revision': 0, 'enabled': False, 'fallbacks': []}


def roles():
    from app.llm import router
    return sorted(set(config.load_models().get('roles', {})) |
                  {item['id'] for item in router.module_models()} |
                  {'companion', 'work', 'orchestrator'})


def profile_routes():
    from app import db
    if not db.DB_PATH.is_file(): return []
    try:
        return [item for item in catalog.listing('model-profile')
                if item.get('enabled') and not item.get('deleted') and 'text' in item.get('capabilities', [])
                and item.get('base_url') and item.get('model')]
    except sqlite3.OperationalError:
        return []


def resolve_target(target):
    from app.llm import router
    if not isinstance(target, str) or not 1 <= len(target) <= 500 or '/' not in target:
        raise ValueError('候选必须是具体模型，不能递归引用角色')
    prefix, model = target.split('/', 1)
    if prefix.startswith('profile_'):
        item = next((p for p in profile_routes() if prefix == 'profile_' + p['id'] and model == p['model']), None)
        if item is None:
            raise ValueError('候选配置不存在、已删除或未启用文字能力')
        from app.reasoning import for_module
        return {**deepcopy(item), 'id': prefix, 'reasoning': for_module(item)}, model
    provider, resolved = router.resolve(target)
    if prefix.startswith('module_'):
        item = next((p for p in router.module_models() if prefix == 'module_' + p['id']), None)
        capabilities = item.get('capabilities', []) if item else []
    else:
        metadata = next((m for m in provider.get('models', []) if m['id'] == resolved), None)
        if metadata is None:
            raise ValueError('候选模型不在服务配置清单中')
        capabilities = metadata.get('capabilities', provider.get('capabilities', ['text']))
    if 'text' not in capabilities:
        raise ValueError('候选没有声明文字能力')
    return {**deepcopy(provider), 'capabilities': capabilities}, resolved


def eligible(provider, messages):
    capabilities = provider.get('capabilities', ['text'])
    if 'text' not in capabilities:
        return False
    protocol = provider.get('protocol', 'openai')
    for message in messages:
        if message.get('_anthropic_blocks') and protocol != 'anthropic':
            return False
        if message.get('_responses_items') and protocol != 'openai':
            return False
        content = message.get('content')
        if isinstance(content, list):
            for part in content:
                if part.get('type') not in ('text', 'input_text') and 'vision' not in capabilities:
                    return False
    return True


def candidates(role, messages):
    from app.llm import router
    # Approved review routes and explicit node model assignments are immutable.
    if '/' in role or router.review_route.get():
        return []
    from app import db
    if not db.DB_PATH.is_file():
        return []
    chain = settings(role)
    if not chain['enabled']:
        return []
    primary = router.resolve(role)
    routes = [primary]
    identities = {route_identity(*primary)}
    for target in chain['fallbacks']:
        try:
            provider, model = resolve_target(target)
        except (ValueError, HTTPException):
            continue  # Deleted/disabled routes never get revived from old settings.
        identity = route_identity(provider, model)
        if (identity not in identities and eligible(provider, messages)
                and provider.get('protocol', 'openai') == primary[0].get('protocol', 'openai')):
            routes.append((provider, model))
            identities.add(identity)
    return deepcopy(routes)


def route_identity(provider, model):
    reference = provider.get('credential_ref') or ('' if provider.get('local') else provider['id'])
    return (provider.get('base_url', '').rstrip('/'), model, reference)


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS provider_events(
            id TEXT PRIMARY KEY, role TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def record_switch(role, source, target, reason):
    # Store identifiers and stable error category only; never exception text or secrets.
    identifier = uuid4().hex
    with connect() as db:
        db.execute('INSERT INTO provider_events(id,role,source,target,reason) VALUES(?,?,?,?,?)',
                   (identifier, role, source, target, reason))
    return {'id': identifier, 'role': role, 'source': source, 'target': target, 'reason': reason}


@catalog.router.get('/provider-failover')
def listing():
    from app.llm import router
    options = []
    for target in router.list_models():
        try:
            provider, model = resolve_target(target)
        except ValueError:
            continue
        options.append({'id': target, 'label': target})
    for item in router.module_models():
        target = 'module_' + item['id'] + '/' + item['model']
        options.append({'id': target, 'label': item['id'] + ' · ' + item['model']})
    for item in profile_routes():
        options.append({'id': 'profile_' + item['id'] + '/' + item['model'],
                        'label': item.get('name', item['id']) + ' · ' + item['model']})
    items = []
    for role in roles():
        try:
            provider, model = router.resolve(role)
            primary = provider['id'] + '/' + model
        except ValueError:
            primary = ''
        items.append({**settings(role), 'primary': primary})
    return {'items': items, 'options': list({item['id']:item for item in options}.values())}


@catalog.router.put('/provider-failover/{role}')
def save(role: str, body: Chain):
    if role not in roles():
        raise HTTPException(422, '未知文字角色')
    if len(set(body.fallbacks)) != len(body.fallbacks):
        raise HTTPException(422, '候选不能重复')
    if body.enabled and not body.fallbacks:
        raise HTTPException(422, '开启前至少选择一个候选')
    from app.llm import router
    try:
        primary, _ = router.resolve(role)
    except ValueError:
        if body.enabled:
            raise HTTPException(422, '请先为此用途分配首选模型') from None
        primary = None
    for target in body.fallbacks:
        try:
            candidate, _ = resolve_target(target)
            if primary and candidate.get('protocol', 'openai') != primary.get('protocol', 'openai'):
                raise ValueError('当前版本的候选须与首选使用相同协议')
        except (ValueError, HTTPException) as error:
            raise HTTPException(422, '候选不可用或协议与首选不一致，请刷新后重新选择') from error
    return catalog.put('provider-failover', role, body.model_dump(exclude={'expected_revision'}), body.expected_revision)


@catalog.router.get('/provider-failover-events')
def events(limit: int = Query(default=30, ge=1, le=100)):
    with connect() as db:
        return {'items': [dict(row) for row in db.execute('SELECT * FROM provider_events ORDER BY rowid DESC LIMIT ?', (limit,))]}
