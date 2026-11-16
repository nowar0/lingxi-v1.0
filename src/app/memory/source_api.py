"""Read actual messages for a currently effective private-domain V2 source only.

Import has no DB/config side effects. Sharing a memory never grants its entire chat.
The client supplies a memory identity/CAS, never a message or source-session selector.
"""
from contextlib import contextmanager
import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.memory import v2

router = APIRouter(prefix='/v10/memory-sources', tags=['memory-source-messages'])
MAX_SOURCE_IDS = 200
MAX_PAGE = 20
MAX_RESPONSE_BYTES = 512 * 1024


@contextmanager
def _read_connection(db=None):
    if db is not None:
        yield db
        return
    from app.config import DB_PATH
    from pathlib import Path
    conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')  # One read snapshot binds revision/provenance/message rows.
        yield conn
    finally:
        conn.close()


def _deny(message, status=409):
    raise HTTPException(status, message)


def _bounded_private_chain(conn, row, remaining=None, seen=None):
    """Refuse legacy locators and excessive parent graphs before V2 projection.

    V2 may revalidate legacy table families for general queries. Source-message
    navigation intentionally permits only bounded message/private-memory chains.
    """
    remaining = [32] if remaining is None else remaining
    seen = set() if seen is None else seen
    if row['id'] in seen:
        _deny('来源派生链存在循环')
    remaining[0] -= 1
    if remaining[0] < 0:
        _deny('来源派生链超过有界反查上限', 422)
    document = v2._document(conn, row)
    source = (document or {}).get('source') or {}
    if source.get('legacy_ref') or source.get('kind') not in ('message', 'memory', 'manual'):
        _deny('旧或未知来源需先重新验证，不使用旧定位符扫描聊天')
    ids = source.get('message_ids', [])
    parents = source.get('parent_ids', [])
    versions = source.get('parent_versions', [])
    if not isinstance(ids, list) or len(ids) > MAX_SOURCE_IDS or not isinstance(parents, list) or len(parents) > 32 or not isinstance(versions, list) or len(versions) > 32:
        _deny('来源派生链或消息数量超过反查上限', 422)
    dependencies = conn.execute('SELECT parent_id FROM memory_v2_dependencies WHERE child_id=? LIMIT 33', (row['id'],)).fetchall()
    if len(dependencies) > 32:
        _deny('来源依赖超过反查上限', 422)
    if any(not isinstance(parent, str) or not 1 <= len(parent) <= 200 for parent in parents):
        _deny('来源派生标识无效')
    parent_ids = set(parents) | {dependency[0] for dependency in dependencies}
    if source.get('batch_id'):
        if not v2._table(conn, 'stage6_inputs'):
            _deny('来源批次不存在')
        batch_messages = conn.execute('SELECT message_id FROM stage6_inputs WHERE batch_id=? LIMIT 201', (source['batch_id'],)).fetchall()
        if len(batch_messages) > MAX_SOURCE_IDS:
            _deny('来源批次超过反查上限', 422)
    for version in versions:
        if not isinstance(version, dict) or not isinstance(version.get('id'), str):
            _deny('来源版本链无效')
        parent_ids.add(version['id'])
    if len(parent_ids) > 32:
        _deny('来源派生链超过反查上限', 422)
    for parent_id in parent_ids:
        parent = v2._row(conn, parent_id)
        if (parent['scope'], parent['owner_id']) != (row['scope'], row['owner_id']):
            _deny('原消息派生链不可跨越私域或工作会话', 403)
        _bounded_private_chain(conn, parent, remaining, seen | {row['id']})


def source_messages(entity_id, expected_revision, mode, session_id=None, offset=0, limit=10, *, db=None):
    if (not isinstance(entity_id, str) or not 1 <= len(entity_id) <= 200
            or type(expected_revision) is not int or not 1 <= expected_revision <= 2147483647
            or mode not in ('companion', 'work') or type(offset) is not int or not 0 <= offset <= MAX_SOURCE_IDS
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE
            or session_id is not None and (type(session_id) is not int or session_id < 1)):
        _deny('原消息请求参数无效', 422)
    if mode == 'work' and session_id is None:
        _deny('工作原消息反查必须绑定当前工作会话', 422)
    with _read_connection(db) as conn:
        row = v2._row(conn, entity_id)
        # Do not call get/projection before this privacy check.
        if row['scope'] == 'shared':
            _deny('只共享了记忆内容，未授权整条聊天原文；共享投影不能反查原消息', 403)
        if row['scope'] != mode or mode == 'work' and row['owner_id'] != str(session_id):
            _deny('当前模式或工作会话无权读取此原消息', 403)
        if row['revision'] != expected_revision:
            _deny('记忆修订已变化，请关闭原消息并刷新记忆后重新打开')
        document = v2._document(conn, row)
        source = (document or {}).get('source') or {}
        ids = source.get('message_ids')
        if source.get('kind') not in ('message', 'memory') or source.get('known') is not True or not ids:
            _deny('此记忆没有可验证的原消息；手动录入或未知旧来源不会制造聊天原文', 404)
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SOURCE_IDS:
            _deny('原消息来源数量无效或超过有界反查上限', 422)
        if any(type(mid) not in (int, str) or not str(mid).isascii() or not str(mid).isdigit()
               or int(mid) < 1 or int(mid) > 9223372036854775807 for mid in ids):
            _deny('原消息来源标识无效', 422)
        message_ids = sorted({int(mid) for mid in ids})
        fingerprints = source.get('message_fingerprints')
        if not isinstance(fingerprints, dict) or any(str(mid) not in fingerprints for mid in message_ids):
            _deny('原消息缺少已记录内容指纹，不返回未验证原文')
        # Avoid legacy table scans: this endpoint does not turn unknown old locators
        # into chat access. Normal message/memory sources are checked by V2 itself.
        if source.get('legacy_ref'):
            _deny('旧来源需要先重新验证，不使用旧定位符直接读取聊天', 409)
        _bounded_private_chain(conn, row)
        item = v2.get(entity_id, db=conn)
        if item.get('status') != 'active' or item.get('redacted') or not item.get('eligible') or not v2._effective(conn, row):
            _deny('来源已变化、撤回、删除或被彻底忘记，不能显示原消息')
        if not v2._table(conn, 'sessions') or not v2._table(conn, 'messages'):
            _deny('原消息或来源会话不存在')
        # A bounded indexed lookup of server-derived IDs. Validate the WHOLE source
        # before paging so an archived off-page source cannot revive partial text.
        placeholders = ','.join('?' for _ in message_ids)
        messages = conn.execute('SELECT * FROM messages WHERE id IN (' + placeholders + ') ORDER BY id', message_ids).fetchall()
        if len(messages) != len(message_ids):
            _deny('来源消息已不存在')
        sessions = {}
        results = []
        for raw in messages:
            message = dict(raw)
            sid = message.get('session_id')
            if sid not in sessions:
                found = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
                sessions[sid] = dict(found) if found else None
            session = sessions[sid]
            if (not session or session.get('archived') or session.get('mode') != row['scope']
                    or mode == 'work' and str(sid) != str(session_id)
                    or source.get('session_id') is not None and str(source['session_id']) != str(sid)):
                _deny('来源会话已失效、归档或不属于当前私域')
            if (message.get('archived') or message.get('delivery_status', 'complete') not in ('complete', 'retained')
                    or not isinstance(message.get('content'), str)
                    or v2.content_hash(message['content']) != fingerprints[str(message['id'])]):
                _deny('原消息内容已变化或撤回，请刷新来源')
            if not isinstance(message.get('role'), str) or not message['role'] or message.get('at') is not None and not isinstance(message['at'], str):
                _deny('原消息元数据无效，未显示不完整内容')
            speaker = None
            if message.get('speaker_snapshot'):
                try:
                    speaker = json.loads(message['speaker_snapshot'])
                except (TypeError, ValueError):
                    _deny('原消息人物快照无效')
                if not isinstance(speaker, dict):
                    _deny('原消息人物快照格式无效')
            results.append({'id': message['id'], 'session_id': sid, 'session_mode': session['mode'],
                            'role': message['role'], 'content': message['content'], 'at': message.get('at'), 'speaker': speaker})
        page = {'entity_id': entity_id, 'revision': row['revision'], 'version_id': item['version_id'],
                'source_status': 'verified', 'read_only': True, 'total': len(results),
                'offset': offset, 'limit': limit, 'items': results[offset:offset + limit]}
        try:
            encoded = json.dumps(page, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, RecursionError):
            _deny('原消息人物元数据不是有效有限JSON，未显示原文')
        if len(encoded) > MAX_RESPONSE_BYTES:
            _deny('原消息页过大，请减小每页数量；不截断或伪造原文', 413)
        return page


@router.get('/{entity_id}/messages')
def messages_route(request: Request, entity_id: str, expected_revision: int = Query(ge=1, le=2147483647),
                   mode: Literal['companion', 'work'] = Query(), session_id: int | None = Query(None, ge=1),
                   offset: int = Query(0, ge=0, le=MAX_SOURCE_IDS), limit: int = Query(10, ge=1, le=MAX_PAGE)):
    if (set(request.query_params) - {'expected_revision', 'mode', 'session_id', 'offset', 'limit'}
            or len(request.query_params.multi_items()) != len(request.query_params)):
        _deny('原消息反查不接受重复参数、任意消息ID或来源会话选择器', 422)
    try:
        return source_messages(entity_id, expected_revision, mode, session_id, offset, limit)
    except v2.MemoryError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
