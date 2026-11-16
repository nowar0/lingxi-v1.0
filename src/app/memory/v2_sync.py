"""Read-only, scope-private V2 change cursors for visible-window polling.

Not an event stream or a global revision counter. Each call revalidates the actual
visible projection (including provenance) in one SQLite read snapshot. The existing
v2._all helper scans the entity table: cost is O(scanned entities + provenance), so
clients should poll only while connected/visible, about every 15 seconds, not per
frame. Before projecting, scoped SQL candidate and content-version counts enforce
MAX_VISIBLE (2000) / MAX_HISTORY (5000) budgets; the candidate set is conservatively
wider than source-effective rows. The helper still scans the whole entity table,
so these budgets do not claim to bound all SQL scanning or provenance recursion.
Only currently visible entities contribute history and history-source validity.
Cursors are process-keyed HMACs: no content/count/raw event sequence is returned;
a process restart deliberately makes an old cursor report changed. Multiple host
workers need a shared key or cursors will differ between workers.

Import opens no DB, migrates nothing and writes no file. Router registration is the
host's explicit responsibility. Caller-owned connections support synthetic tests.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.memory import v2

MAX_VISIBLE = 2000
MAX_HISTORY = 5000
_PROCESS_KEY = secrets.token_bytes(32)
_CURSOR = re.compile(r'^[0-9a-f]{64}$')
router = APIRouter(prefix='/v10/memory-sync', tags=['memory-v2-sync'])


class SyncUnavailable(v2.MemoryError):
    status_code = 503


def _filters(scope, session_id, layer, category, include_deleted, cursor):
    if not isinstance(scope, str) or scope not in v2.SCOPES:
        raise v2.MemoryError('scope必须为companion/work/shared')
    if scope == 'work':
        if type(session_id) is not int or not 1 <= session_id <= 2147483647:
            raise v2.MemoryError('工作同步必须指定有效session_id')
    elif session_id is not None:
        raise v2.MemoryError('非工作范围不接受session_id')
    if layer is not None and (type(layer) is not int or layer not in v2.LAYERS):
        raise v2.MemoryError('无效layer')
    if category is not None and (not isinstance(category, str) or not 1 <= len(category) <= 80
                                 or any(ord(char) < 32 for char in category)):
        raise v2.MemoryError('category必须为1..80字符')
    if type(include_deleted) is not bool:
        raise v2.MemoryError('include_deleted必须为布尔值')
    if cursor is not None and (not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor)):
        raise v2.MemoryError('cursor必须为64位小写十六进制')
    return {'scope':scope, 'session_id':session_id, 'layer':layer,
            'category':category, 'include_deleted':include_deleted}


@contextmanager
def _snapshot(db=None):
    if db is None:
        from app.db import connect
        with connect() as current:
            with _snapshot(current) as conn:
                yield conn
        return
    owned = not db.in_transaction
    if owned:
        db.execute('BEGIN')
    try:
        yield db
    finally:
        # A read-only transaction is ended without committing a caller's writes.
        if owned:
            db.rollback()


def _candidate_clause(filters):
    """Same lexical scope/owner/filter boundary as _all, before provenance checks.

    Source effectiveness and work ancestry can only remove these candidates. No
    unrelated scope/session is counted to decide whether this request is too large.
    """
    terms = ['scope=?', "status!='forgotten'"]
    args = [filters['scope']]
    if filters['scope'] == 'work':
        terms.append('owner_id=?')
        args.append(v2.owner('work', filters['session_id']))
    if not filters['include_deleted']:
        terms.append("status='active'")
    if filters['layer'] is not None:
        terms.append('layer=?')
        args.append(filters['layer'])
    if filters['category'] is not None:
        terms.append('category=?')
        args.append(filters['category'])
    return ' AND '.join(terms), args


def _check_projection_budget(conn, filters):
    clause, args = _candidate_clause(filters)
    count = conn.execute('SELECT COUNT(*) FROM memory_v2_entities WHERE ' + clause,
                         args).fetchone()[0]
    if count > MAX_VISIBLE:
        raise SyncUnavailable('当前范围超出同步容量，请使用手动刷新；未生成部分游标')
    # _projection itself reads an entity's old source-character documents. Bound
    # candidates' content versions before calling it, not after allocating them.
    versions = conn.execute(
        'SELECT COUNT(*) FROM memory_v2_versions WHERE entity_id IN '
        '(SELECT id FROM memory_v2_entities WHERE ' + clause + ')', args).fetchone()[0]
    if versions > MAX_HISTORY:
        raise SyncUnavailable('当前范围历史版本超出同步容量，请使用手动刷新；未生成部分游标')


def _history_projection(conn, items):
    """Hash every bounded visible entity version and its *current* source validity.

    No historical text, source identifiers or row counts leave this function's
    internal HMAC input. Histories of invisible entities are never read here.
    """
    result = []
    for item in sorted(items, key=lambda value:value['id']):
        row = conn.execute('SELECT * FROM memory_v2_entities WHERE id=?',
                           (item['id'],)).fetchone()
        if row is None:
            raise SyncUnavailable('记忆同步快照不完整，请稍后重读')
        row = dict(row)
        remaining = MAX_HISTORY - len(result)
        records = conn.execute(
            'SELECT revision,document,source_id,at,replaces_revision '
            'FROM memory_v2_versions WHERE entity_id=? ORDER BY revision LIMIT ?',
            (item['id'], remaining + 1)).fetchall()
        if len(records) > remaining:
            raise SyncUnavailable('当前范围历史版本超出同步容量，请使用手动刷新；未生成部分游标')
        for version in records:
            document = json.loads(version['document'],
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite history')))
            if not isinstance(document, dict):
                raise ValueError('invalid historical document')
            if (document.get('scope'), document.get('owner_id')) != (row['scope'], row['owner_id']):
                raise ValueError('historical scope/owner mismatch')
            stable_document = {key:value for key,value in document.items()
                               if key not in ('activation', 'relations')}
            current = version['revision'] == row['current_version']
            effective = (v2._effective(conn, row) if current else
                         v2._historical_effective(conn, row, document))
            result.append({'entity_id':row['id'], 'version_revision':version['revision'],
                'is_current':current, 'eligible':effective, 'document':stable_document,
                'source_id':version['source_id'], 'at':version['at'],
                'replaces_revision':version['replaces_revision']})
    return result


def change_state(*, scope, session_id=None, layer=None, category=None,
                 include_deleted=False, cursor=None, db=None):
    filters = _filters(scope, session_id, layer, category, include_deleted, cursor)
    try:
        with _snapshot(db) as conn:
            if not v2.ready(conn):
                raise SyncUnavailable('V2同步暂不可用；未初始化或迁移数据')
            required = {match.group(1) for statement in v2.SCHEMA
                        if (match := re.match(r'CREATE TABLE IF NOT EXISTS (memory_v2_\w+)', statement))}
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_v2_%'")}
            if not required <= tables:
                raise SyncUnavailable('V2同步配套表不完整；未初始化或迁移数据')
            _check_projection_budget(conn, filters)
            authority = conn.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
            authority = authority[0] if authority else 'unknown'
            items = v2._all(conn, scope=scope, mode='work' if scope == 'work' else scope,
                            session_id=session_id, layer=layer, category=category,
                            include_deleted=include_deleted)
            # Sharing a memory does not grant visibility into a revoked projection.
            # include_deleted may show local soft-deleted diagnostics, never bypass grants.
            if scope == 'shared':
                items = [item for item in items if item.get('eligible') is True]
            if len(items) > MAX_VISIBLE:
                raise SyncUnavailable('当前范围超出同步容量，不能用截断数据生成全量游标')
            ids = {item['id'] for item in items}
            stable = []
            for item in items:
                document = {key:value for key,value in item.items() if key not in ('activation', 'relations')}
                # v2 projections can contain a relation to a source outside the scope.
                # Never let another scope's relation payload affect this scope's cursor.
                document['relations'] = sorted(
                    [dict(edge) for edge in item.get('relations', ())
                     if edge.get('from_id') in ids and edge.get('to_id') in ids],
                    key=lambda edge:edge['id'])
                stable.append(document)
            value = {'format':'lingxi.memory-sync.v1', 'filters':filters,
                     'authority':authority, 'items':sorted(stable, key=lambda item:item['id']),
                     'history':_history_projection(conn, items)}
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')
            observed = hmac.new(_PROCESS_KEY, raw, hashlib.sha256).hexdigest()
    except (SyncUnavailable, v2.MemoryError):
        raise
    except (sqlite3.Error, TypeError, KeyError, ValueError, RecursionError) as exc:
        raise SyncUnavailable('记忆同步状态暂不可用；未写入或修改数据') from exc
    return {'cursor':observed, 'changed':cursor is None or not hmac.compare_digest(cursor, observed),
            'read_only':True}


@router.get('/change-state')
def state(request: Request, scope: Literal['companion', 'work', 'shared'],
          session_id: int | None = Query(default=None, ge=1, le=2147483647),
          layer: int | None = Query(default=None, ge=1, le=4),
          category: str | None = Query(default=None, min_length=1, max_length=80),
          include_deleted: bool = False,
          cursor: str | None = Query(default=None, pattern=r'^[0-9a-f]{64}$')):
    if set(request.query_params) - {'scope','session_id','layer','category','include_deleted','cursor'}:
        raise HTTPException(422, '同步请求包含未知参数')
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(422, '同步请求不接受重复参数')
    try:
        return change_state(scope=scope, session_id=session_id, layer=layer, category=category,
                            include_deleted=include_deleted, cursor=cursor)
    except (SyncUnavailable, v2.MemoryError) as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
