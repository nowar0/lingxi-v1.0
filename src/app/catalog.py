"""Versioned skill and plugin catalog. Skill text is fetched only on assignment."""
import json
from contextlib import contextmanager
from contextvars import ContextVar
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from app.credential_refs import validate_reference

from app.db import connect

router = APIRouter(prefix='/v6')
_active_db = ContextVar('catalog_transaction', default=None)


@contextmanager
def transaction():
    current = _active_db.get()
    if current is not None:
        yield current
        return
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        token = _active_db.set(db)
        try:
            yield db
        finally:
            _active_db.reset(token)


@contextmanager
def read_connection():
    current = _active_db.get()
    if current is not None:
        yield current
    else:
        with connect() as db:
            yield db


class Skill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=100000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True
    expected_revision: int = Field(default=0, ge=0)


class Plugin(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    transport: Literal['stdio', 'http']
    command: str = Field(default='', max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=30)
    url: str = Field(default='', max_length=2000)
    credential_ref: str = Field(default='', max_length=100)
    enabled: bool = False
    expected_revision: int = Field(default=0, ge=0)

    @field_validator('credential_ref')
    @classmethod
    def reference_is_not_key(cls, value):
        return validate_reference(value)


class PluginSave(Plugin):
    """Save a connection and a fresh secret reference as one compensated mutation."""
    secret: SecretStr | None = Field(default=None, max_length=10000)
    plugin_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,100}$')


@router.post('/mcp-connections/save')
def save_mcp_connection(body: PluginSave):
    from app import credential_lifecycle
    validate_plugin(body)
    with credential_lifecycle.mutation():
        if body.plugin_id:
            current = get('plugin', body.plugin_id)
            if current['revision'] != body.expected_revision or current.get('deleted'):
                raise HTTPException(409, 'MCP 配置已变化，请重新加载；凭据未修改')
        elif body.expected_revision:
            raise HTTPException(422, '新 MCP 配置版本应为0')
        document = body.model_dump(exclude={'secret', 'plugin_id', 'expected_revision'})
        if body.secret is not None:
            secret = body.secret.get_secret_value()
            if not secret.strip():
                raise HTTPException(422, '新凭据不能为空')
            # Never overwrite a reference potentially shared by another connection.
            reference = 'mcp_' + uuid4().hex
            credential_lifecycle.save_key(reference, secret, fresh=True)
            document['credential_ref'] = reference
        return put('plugin', body.plugin_id or uuid4().hex, document, body.expected_revision)


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS capability_documents(
            kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
            document TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(kind,id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS capability_versions(
            kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
            document TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(kind,id,revision))''')
        db.execute('CREATE INDEX IF NOT EXISTS capability_documents_kind_updated ON capability_documents(kind,updated_at DESC,id DESC)')


def get(kind, item_id):
    with read_connection() as db:
        row = db.execute('SELECT * FROM capability_documents WHERE kind=? AND id=?', (kind, item_id)).fetchone()
    if not row:
        raise HTTPException(404, '记录不存在')
    return {**json.loads(row['document']), 'id': row['id'], 'revision': row['revision']}


def listing(kind):
    with read_connection() as db:
        rows = db.execute('SELECT * FROM capability_documents WHERE kind=? ORDER BY updated_at,id', (kind,)).fetchall()
    return [{**json.loads(r['document']), 'id': r['id'], 'revision': r['revision']}
            for r in rows if not json.loads(r['document']).get('deleted')]


def put(kind, item_id, document, expected):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', item_id):
        raise HTTPException(422, '编号格式错误')
    from app.credential_lifecycle import mutation
    with mutation():
        return put_in_transaction(_active_db.get(), kind, item_id, document, expected)


def put_in_transaction(db, kind, item_id, document, expected):
    """Participate in the caller's transaction; never commit a partial aggregate."""
    if kind == 'skill' and item_id.startswith('pkgskill_'):
        raise HTTPException(409, '插件技能为只读包投影，请在插件管理中更新或停用')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', item_id):
        raise HTTPException(422, '编号格式错误')
    from app.credential_lifecycle import assert_live_reference
    if kind in ('module', 'model-profile', 'model-key', 'plugin') and not document.get('deleted'):
        assert_live_reference(db, document.get('credential_ref', ''))
    value = json.dumps(document, ensure_ascii=False)
    at = datetime.now(timezone.utc).isoformat()
    old = db.execute('SELECT revision FROM capability_documents WHERE kind=? AND id=?', (kind, item_id)).fetchone()
    if (old['revision'] if old else 0) != expected:
        raise HTTPException(409, '记录已修改，请刷新后再保存')
    revision = expected + 1
    db.execute('INSERT INTO capability_documents VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,document=excluded.document,updated_at=excluded.updated_at', (kind, item_id, revision, value, at))
    db.execute('INSERT INTO capability_versions VALUES(?,?,?,?,?)', (kind, item_id, revision, value, at))
    return {**document, 'id': item_id, 'revision': revision}


def skill_metadata(tags=None):
    from app import package_capabilities
    plugins = {p['id']: p for p in listing('plugin')}
    package_dependencies = {p['namespace']: p['available'] for p in package_capabilities.package_metadata() if 'namespace' in p}
    results = []
    for item in listing('skill'):
        missing = [pid for pid in item['dependencies'] if not (
            package_dependencies.get(pid, False) if pid.startswith('pkg:') else plugins.get(pid, {}).get('enabled'))]
        meta = {k: v for k, v in item.items() if k != 'body'}
        meta.update(missing_dependencies=missing, available=item['enabled'] and not missing)
        if tags is None or set(tags).intersection(item['tags']):
            results.append(meta)
    return results + package_capabilities.skill_metadata(tags)


def assigned_skill(item_id):
    from app import package_capabilities
    if item_id.startswith(package_capabilities.PREFIX):
        return package_capabilities.assigned_skill(item_id)
    item = get('skill', item_id)
    metadata = next((s for s in skill_metadata() if s['id'] == item_id), None)
    if metadata is None:
        raise HTTPException(409, '技能已删除')
    if not metadata['available']:
        raise HTTPException(409, '技能未启用或缺少依赖插件')
    return item


@router.get('/skills')
def skills():
    return {'items': skill_metadata()}


@router.get('/skills/{item_id}')
def skill(item_id: str):
    from app import package_capabilities
    return package_capabilities.assigned_skill(item_id) if item_id.startswith(package_capabilities.PREFIX) else get('skill', item_id)


@router.post('/skills')
def create_skill(body: Skill):
    if body.expected_revision:
        raise HTTPException(422, '新技能版本应为0')
    return put('skill', uuid4().hex, body.model_dump(exclude={'expected_revision'}), 0)


@router.put('/skills/{item_id}')
def update_skill(item_id: str, body: Skill):
    get('skill', item_id)
    return put('skill', item_id, body.model_dump(exclude={'expected_revision'}), body.expected_revision)


@router.get('/plugins')
def plugins():
    return {'items': listing('plugin')}


@router.post('/plugins')
def create_plugin(body: Plugin):
    if body.expected_revision:
        raise HTTPException(422, '新插件版本应为0')
    validate_plugin(body)
    return put('plugin', uuid4().hex, body.model_dump(exclude={'expected_revision'}), 0)


def validate_plugin(body):
    from urllib.parse import urlsplit
    if body.transport == 'stdio' and not body.command.strip():
        raise HTTPException(422, '需要可执行程序路径')
    if body.transport == 'http':
        url = urlsplit(body.url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise HTTPException(422, '需要不含凭据、查询参数或片段的 HTTP(S) 地址')


@router.put('/plugins/{item_id}')
def update_plugin(item_id: str, body: Plugin):
    get('plugin', item_id)
    validate_plugin(body)
    return put('plugin', item_id, body.model_dump(exclude={'expected_revision'}), body.expected_revision)


@router.delete('/skills/{item_id}')
def delete_skill(item_id: str, expected_revision: int):
    item = get('skill', item_id)
    return put('skill', item_id, {k: v for k, v in {**item, 'enabled': False, 'deleted': True}.items()
                                 if k not in ('id', 'revision')}, expected_revision)


@router.delete('/plugins/{item_id}')
def delete_plugin(item_id: str, expected_revision: int):
    item = get('plugin', item_id)
    return put('plugin', item_id, {k: v for k, v in {**item, 'enabled': False, 'deleted': True}.items()
                                  if k not in ('id', 'revision')}, expected_revision)
