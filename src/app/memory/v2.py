"""Four-layer memory V2; explicit, single-database, offline SQLite authority.

Importing this module neither imports configuration nor opens a database. Call
init_table() explicitly, then migrate_legacy()/sync() explicitly. Production
connections always come from app.db.connect (and therefore its APPDATA policy).
An optional caller-owned SQLite connection supports atomic integration and tests.
No model, credential, filesystem backup, scheduler or automatic extraction lives
here. Old proposal/extraction bounds remain the caller's responsibility.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import uuid

LAYERS = (1, 2, 3, 4)
SCOPES = ("companion", "work", "shared")
WORK_LONG_TERM = frozenset(("habit", "project"))
REINFORCEMENT_ACTIONS = frozenset(("user_remention", "user_confirm"))
MAX_PAGE = 200


class MemoryError(ValueError):
    status_code = 422


class Conflict(MemoryError):
    status_code = 409


class NotFound(MemoryError):
    status_code = 404


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


def content_hash(value):
    return fingerprint(" ".join(str(value).casefold().split()))


def _id(prefix):
    return prefix + uuid.uuid4().hex


@contextmanager
def _connection(db=None, write=False):
    if db is None:
        # Deliberately lazy: config imports optional dependencies in this app.
        from app.db import connect
        with connect() as conn:
            with _connection(conn, write=write) as current:
                yield current
        return
    if not write:
        yield db
        return
    # Savepoints also make each helper atomic inside a caller's transaction.
    outer = not db.in_transaction
    if outer:
        db.execute("BEGIN IMMEDIATE")
    marker = "memory_v2_" + uuid.uuid4().hex
    db.execute("SAVEPOINT " + marker)
    try:
        yield db
        db.execute("RELEASE SAVEPOINT " + marker)
        if outer:
            db.commit()
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT " + marker)
        db.execute("RELEASE SAVEPOINT " + marker)
        if outer:
            db.rollback()
        raise


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memory_v2_meta(
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_entities(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner_id TEXT NOT NULL,
        key TEXT NOT NULL, layer INTEGER NOT NULL CHECK(layer BETWEEN 1 AND 4),
        category TEXT NOT NULL, importance REAL NOT NULL, pinned INTEGER NOT NULL,
        status TEXT NOT NULL, revision INTEGER NOT NULL, current_version INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        reinforced_at TEXT, reinforcement_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE(scope,owner_id,key))""",
    """CREATE TABLE IF NOT EXISTS memory_v2_sources(
        id TEXT PRIMARY KEY, ref TEXT NOT NULL, document TEXT NOT NULL,
        known INTEGER NOT NULL, character_id TEXT, session_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_versions(
        entity_id TEXT NOT NULL REFERENCES memory_v2_entities(id),
        revision INTEGER NOT NULL, document TEXT NOT NULL, source_id TEXT NOT NULL,
        at TEXT NOT NULL, replaces_revision INTEGER,
        PRIMARY KEY(entity_id,revision))""",
    """CREATE TABLE IF NOT EXISTS memory_v2_dependencies(
        parent_id TEXT NOT NULL, child_id TEXT NOT NULL, kind TEXT NOT NULL,
        PRIMARY KEY(parent_id,child_id,kind))""",
    """CREATE TABLE IF NOT EXISTS memory_v2_relations(
        id TEXT PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('confirmed','similarity')),
        label TEXT NOT NULL, score REAL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_events(
        id TEXT PRIMARY KEY, entity_id TEXT, action TEXT NOT NULL,
        document TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_grants(
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
        source_revision INTEGER NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_vectors(
        entity_id TEXT NOT NULL, revision INTEGER NOT NULL, adapter TEXT NOT NULL,
        vector TEXT NOT NULL, PRIMARY KEY(entity_id,adapter))""",
    """CREATE TABLE IF NOT EXISTS memory_v2_blocks(
        kind TEXT NOT NULL, value TEXT NOT NULL, operation_id TEXT NOT NULL,
        at TEXT NOT NULL, PRIMARY KEY(kind,value))""",
    """CREATE TABLE IF NOT EXISTS memory_v2_forgets(
        id TEXT PRIMARY KEY, document TEXT NOT NULL, token TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_migrations(
        source_ref TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, status TEXT NOT NULL, document TEXT NOT NULL,
        entity_id TEXT, error TEXT NOT NULL, attempts INTEGER NOT NULL,
        revision INTEGER NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_v2_legacy_baselines(
        source_ref TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, document TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS memory_v2_scope ON memory_v2_entities(scope,owner_id,status)",
    "CREATE INDEX IF NOT EXISTS memory_v2_parent ON memory_v2_dependencies(parent_id)",
    "CREATE INDEX IF NOT EXISTS memory_v2_migration_status ON memory_v2_migrations(status,source_ref)",
)


def init_table(db=None):
    """Create V2 schema only. Never scans old memory/chat, migrates or backs up."""
    with _connection(db, write=True) as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT OR IGNORE INTO memory_v2_meta VALUES('schema','2')")
        conn.execute("INSERT OR IGNORE INTO memory_v2_meta VALUES('authority','legacy')")
    return {"schema": 2, "migration_started": False}


def ready(db=None):
    with _connection(db) as conn:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_v2_entities'").fetchone())


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _columns(db, name):
    # Only module-owned literal table names reach this helper.
    return {r[1] for r in db.execute("PRAGMA table_info(" + name + ")")}


def owner(scope, session_id=None):
    if scope == "companion":
        return "companion-global"  # All characters share it; provenance retains character.
    if scope == "shared":
        return "global"
    if scope != "work" or session_id is None or not str(session_id).strip():
        raise MemoryError("工作记忆需要工作会话ID；scope与layer彼此独立")
    return str(session_id)


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryError("日期必须是ISO字符串或null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryError("无效日期") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _validate(data, core_authorized=False):
    if data["scope"] not in SCOPES:
        raise MemoryError("无效记忆范围")
    if not isinstance(data["key"], str) or not 1 <= len(data["key"].strip()) <= 160:
        raise MemoryError("key限1～160字")
    if not isinstance(data["value"], str) or not 1 <= len(data["value"].strip()) <= 16000:
        raise MemoryError("value限1～16000字")
    if type(data["layer"]) is not int or data["layer"] not in LAYERS:
        raise MemoryError("layer必须是1～4整数")
    if data["layer"] == 4 and not core_authorized:
        raise MemoryError("核心仅可通过用户手动确认接口建立")
    if not isinstance(data["category"], str) or not 1 <= len(data["category"]) <= 80:
        raise MemoryError("无效category")
    if (type(data["importance"]) not in (int, float) or
            not math.isfinite(data["importance"]) or not 0 <= data["importance"] <= 1):
        raise MemoryError("importance必须为0～1有限数值")
    if type(data["pinned"]) is not bool:
        raise MemoryError("pinned必须是布尔值；置顶不等于核心")
    if data["scope"] == "work" and data["layer"] >= 3 and data["category"] not in WORK_LONG_TERM:
        raise MemoryError("工作长期记忆仅允许操作习惯(habit)与项目名册(project)")
    tags=data.get("tags", [])
    if not isinstance(tags,list) or len(tags)>12 or any(not isinstance(tag,str) or not 1<=len(tag.strip())<=30 for tag in tags):
        raise MemoryError("最多12个标签，每个标签为1～30字")
    data={**data,"tags":list(dict.fromkeys(tag.strip() for tag in tags))}
    for field in ("valence","arousal"):
        value=data.get(field)
        if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1):
            raise MemoryError(field+"必须为0～1有限数值或空值")
        data[field]=value
    _timestamp(data.get("occurred_at"))
    _timestamp(data.get("confirmed_at"))
    return {**data, "key": data["key"].strip(), "value": data["value"].strip()}


def _block(conn, kind, value):
    return value is not None and bool(conn.execute(
        "SELECT 1 FROM memory_v2_blocks WHERE kind=? AND value=?", (kind, str(value))).fetchone())


def _source_refs(source):
    refs = set()
    for key in ("ref", "source_ref", "legacy_ref", "source_id", "legacy_memory_id"):
        if source.get(key) is not None:
            refs.add(str(source[key]))
    for value in source.get("source_refs", ()):
        refs.add(str(value))
    return refs


def _blocked(conn, item):
    if not isinstance(item, dict):
        return True
    source = item.get("source") or {}
    if not isinstance(source, dict):
        return True
    for value in (item.get("entity_id"), item.get("memory_id"), item.get("id")):
        if _block(conn, "entity", value) or _block(conn, "source_ref", value):
            return True
    for value in _source_refs(source) | _source_refs(item):
        if _block(conn, "source_ref", value) or _block(conn, "source", value):
            return True
    messages = list(source.get("message_ids", ())) + list(item.get("message_ids", ()))
    if source.get("source_memory_version_id") is not None and _block(
            conn, "source_ref", "legacy:fact_versions:" + str(source["source_memory_version_id"])):
        return True
    for obj in (item, source):
        if obj.get("message_id") is not None:
            messages.append(obj["message_id"])
        for key in ("parent_ids", "source_memory_ids"):
            if any(_block(conn, "entity", value) for value in obj.get(key, ())):
                return True
    if source.get("source_memory_id") and _block(conn, "entity", source["source_memory_id"]):
        return True
    if any(_block(conn, "message", value) for value in messages):
        return True
    # Recognize the existing local-vector/history source ID spelling too.
    source_id = str(item.get("source_id", ""))
    if source_id.startswith("message-") and _block(conn, "message", source_id[8:]):
        return True
    value = item.get("value", item.get("content", item.get("text")))
    return value is not None and _block(conn, "content", content_hash(value))


def forget_blocked(item, db=None):
    """Fail-closed provenance/content tombstone check for writers and retrieval."""
    with _connection(db) as conn:
        return _blocked(conn, item)


def _record_source(conn, source, default_manual=False, persist=True):
    if source is None and default_manual:
        source = {"kind": "manual", "actor": "user", "operation_id": _id("manual-")}
    if not isinstance(source, dict):
        raise MemoryError("必须提供真实来源；未知旧来源应明确标记legacy/unknown")
    source = dict(source)
    kind = source.get("kind", "unknown")
    if kind == "manual" and not source.get("operation_id"):
        source["operation_id"] = _id("manual-")
    if kind not in ("manual", "message", "legacy", "memory", "unknown"):
        raise MemoryError("无效来源类型")
    known = kind == "manual" and source.get("actor") == "user"
    messages = source.get("message_ids", [])
    parents = source.get("parent_ids", [])
    if not isinstance(messages, list) or not isinstance(parents, list):
        raise MemoryError("来源message_ids/parent_ids必须为列表")
    messages, parents = list(messages), list(parents)
    if _blocked(conn, {"source": source}):
        raise Conflict("来源已被彻底忘记；禁止重新提炼")
    if kind == "message" and not messages:
        raise MemoryError("message来源必须有真实message_ids")
    if kind == "memory" and not parents:
        raise MemoryError("memory来源必须有真实parent_ids")
    message_fingerprints = {}
    message_scopes = set()
    message_sessions = set()
    if messages:
        if not _table(conn, "messages"):
            raise MemoryError("无法验证聊天来源；不制造伪来源")
        columns = _columns(conn, "messages")
        for mid in messages:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if not row:
                raise MemoryError("聊天来源不存在")
            row = dict(row)
            if source.get("session_id") is not None and str(row.get("session_id")) != str(source["session_id"]):
                raise MemoryError("聊天来源会话不匹配")
            if ("archived" in columns and row.get("archived")) or (
                    "delivery_status" in columns and row.get("delivery_status") not in ("complete", "retained")):
                raise Conflict("来源聊天已撤回或未完成")
            message_fingerprints[str(mid)] = content_hash(row.get("content", ""))
            message_sessions.add(str(row.get("session_id")))
            if _table(conn, "sessions") and "mode" in _columns(conn, "sessions"):
                session = conn.execute("SELECT mode FROM sessions WHERE id=?", (row.get("session_id"),)).fetchone()
                if session:
                    message_scopes.add(session[0])
        if source.get("message_fingerprints") is not None and source["message_fingerprints"] != message_fingerprints:
            raise Conflict("聊天来源内容已变化")
        source["message_fingerprints"] = message_fingerprints
        source["message_sessions"] = sorted(message_sessions)
        if message_scopes:
            source["message_scopes"] = sorted(message_scopes)
        known = True
    parent_versions = []
    for parent in parents:
        row = conn.execute("SELECT * FROM memory_v2_entities WHERE id=?", (parent,)).fetchone()
        if not row or not _effective(conn, dict(row)):
            raise Conflict("派生来源不可用")
        parent_versions.append({"id": parent, "revision": row["current_version"]})
        original = _document(conn, dict(row))
        for mid in original["source"].get("message_ids", ()):
            if mid not in messages:
                messages.append(mid)
        for key, value in original["source"].get("message_fingerprints", {}).items():
            message_fingerprints[key] = value
        source["source_refs"] = sorted(set(source.get("source_refs", ())) | _source_refs(original["source"]))
        source.setdefault("source_character_id", original["source"].get("character_id", original["source"].get("source_character_id")))
    if messages:
        source["message_ids"] = messages
        source["message_fingerprints"] = message_fingerprints
    if parents:
        known = True
        supplied = source.get("parent_versions")
        if supplied is not None and supplied != parent_versions:
            raise Conflict("派生来源版本已变化")
        source["parent_versions"] = parent_versions
    if kind in ("legacy", "unknown"):
        # A legacy table locator is real provenance, not a fabricated chat/user confirmation.
        known = bool(messages or parents)
        source.setdefault("origin", "旧记录，来源未知")
    source["kind"] = kind
    source["known"] = known
    sid = "src-" + fingerprint(source)
    ref = str(source.get("ref") or source.get("legacy_ref") or sid)
    if persist:
        conn.execute("INSERT OR IGNORE INTO memory_v2_sources VALUES(?,?,?,?,?,?)", (
            sid, ref, dump(source), int(known), source.get("character_id", source.get("source_character_id")),
            str(source["session_id"]) if source.get("session_id") is not None else None))
    return sid, source


_UNSET = object()


def _row(conn, entity_id, expected_revision=_UNSET):
    row = conn.execute("SELECT * FROM memory_v2_entities WHERE id=?", (entity_id,)).fetchone()
    if not row:
        raise NotFound("记忆不存在")
    row = dict(row)
    if expected_revision is not _UNSET and (type(expected_revision) is not int or row["revision"] != expected_revision):
        raise Conflict("版本已变化；请保留输入并刷新比较")
    return row


def _document(conn, row, revision=None):
    result = conn.execute("SELECT * FROM memory_v2_versions WHERE entity_id=? AND revision=?", (
        row["id"], row["current_version"] if revision is None else revision)).fetchone()
    if not result:
        return None
    return json.loads(result["document"])


def _fact_ancestor(conn, row, scope, oid, seen=None):
    seen = set(seen or ())
    if row["id"] in seen:
        return False
    seen.add(row["id"])
    if row["scope"] != scope or row["owner_id"] != oid:
        return True
    return any(_fact_ancestor(conn, _row(conn, dep[0]), scope, oid, seen) for dep in conn.execute(
        "SELECT parent_id FROM memory_v2_dependencies WHERE child_id=?", (row["id"],)))


def _source_effective(conn, row, document, seen=None):
    """Revalidate a version's own provenance for both current and history reads."""
    if not document or document.get("legacy_status") == "withdrawn" or _blocked(conn, {**document, "id": row["id"]}):
        return False
    seen = set(seen or ())
    if row["id"] in seen:
        return False
    seen.add(row["id"])
    source = document.get("source") or {}
    legacy_ref = source.get("legacy_ref")
    if legacy_ref and legacy_ref.startswith(("legacy:fact_versions:", "legacy:events:", "legacy:memory7_event_versions:", "legacy:facts:", "legacy:work_facts:")):
        raw = next((item for item in _read_legacy(conn, 2147483647, include_migrated=True) if item["source_ref"] == legacy_ref), None)
        baseline = conn.execute("SELECT fingerprint FROM memory_v2_legacy_baselines WHERE source_ref=?", (legacy_ref,)).fetchone()
        if raw is None or raw.get("status") == "withdrawn" or baseline and fingerprint(raw) != baseline[0]:
            return False
    if source.get("batch_id"):
        if not all(_table(conn, name) for name in ("messages", "stage6_batches", "stage6_inputs")):
            return False
        batch = conn.execute("SELECT status,session_id FROM stage6_batches WHERE id=?", (source["batch_id"],)).fetchone()
        if not batch or batch["status"] != "complete" or str(batch["session_id"]) != str(source.get("session_id")):
            return False
        inputs = conn.execute("SELECT m.id,m.content,m.session_id FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.batch_id=? ORDER BY m.id", (source["batch_id"],)).fetchall()
        encoded = json.dumps([(r["id"], r["content"], r["session_id"]) for r in inputs], ensure_ascii=False, sort_keys=True)
        if source.get("fingerprint") != hashlib.sha256(encoded.encode()).hexdigest() or source.get("message_ids") != [r["id"] for r in inputs]:
            return False
        if conn.execute("SELECT 1 FROM messages WHERE batch_id=? AND (delivery_status!='complete' OR archived=1) LIMIT 1", (source["batch_id"],)).fetchone():
            return False
    for mid in source.get("message_ids", ()):
        if not _table(conn, "messages"):
            return False
        original = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not original:
            return False
        original = dict(original)
        if original.get("archived") or ("delivery_status" in original and original["delivery_status"] not in ("complete", "retained")):
            return False
        if source.get("session_id") is not None and str(original.get("session_id")) != str(source["session_id"]):
            return False
        expected = source.get("message_fingerprints", {}).get(str(mid))
        if expected is not None and content_hash(original.get("content", "")) != expected:
            return False
    versions = {parent["id"]: parent["revision"] for parent in source.get("parent_versions", ())}
    parent_ids = set(source.get("parent_ids", ())) | set(versions)
    # Legacy migrated dependencies also live in the normalized dependency table.
    parent_kinds = {dep["parent_id"]: dep["kind"] for dep in conn.execute(
        "SELECT parent_id,kind FROM memory_v2_dependencies WHERE child_id=?", (row["id"],))}
    parent_ids.update(parent_kinds)
    for parent_id in parent_ids:
        parent = conn.execute("SELECT * FROM memory_v2_entities WHERE id=?", (parent_id,)).fetchone()
        if not parent or (parent_id in versions and parent["current_version"] != versions[parent_id]):
            return False
        if not _effective(conn, dict(parent), seen):
            return False
        cross_scope = (parent["scope"], parent["owner_id"]) != (row["scope"], row["owner_id"])
        if (parent_kinds.get(parent_id) == "share" or cross_scope) and not conn.execute(
            "SELECT 1 FROM memory_v2_grants WHERE source_id=? AND target_id=? AND status='active'",
            (parent_id, row["id"])).fetchone():
            return False
    return True


def _effective(conn, row, seen=None):
    return row["status"] == "active" and _source_effective(conn, row, _document(conn, row), seen)


def activation(item, at=None, rules=None):
    """Read-only activation; importance, pinning and layer are separate dimensions."""
    if item["layer"] in (3, 4):
        return 1.0
    clock = _timestamp(at or now())
    base = _timestamp(item.get("reinforced_at") or item.get("confirmed_at") or item.get("created_at"))
    if base is None:
        return 1.0
    days = max(0.0, (clock - base).total_seconds() / 86400)
    rules=rules or {}
    half_life = rules.get("short_days",1.0) if item["layer"] == 1 else rules.get("important_days",180.0) if item["importance"] >= rules.get("important_threshold",.7) else rules.get("long_days",30.0)
    return math.exp(-math.log(2) * days / half_life)


def _historical_effective(conn, row, document, seen=None):
    """History keeps manual superseded values but never bypasses source revocation."""
    return _source_effective(conn, row, document, seen)


def _projection(conn, row, at=None, revision=None):
    doc = _document(conn, row, revision)
    if doc is None:
        return {"id": row["id"], "revision": row["revision"], "status": row["status"], "redacted": True}
    current = revision is None or revision == row["current_version"]
    result = {**doc, "id": row["id"], "revision": row["revision"] if revision is None else revision,
              "version_id": row["id"] + ":" + str(row["current_version"] if current else revision),
              "current_revision": row["revision"], "is_current": current,
              "status": row["status"] if current else doc.get("legacy_status", "superseded"),
              "created_at": row["created_at"], "updated_at": row["updated_at"],
              "reinforced_at": row["reinforced_at"], "reinforcement_count": row["reinforcement_count"]}
    result.setdefault("tags",[])
    result.setdefault("valence",None);result.setdefault("arousal",None)
    result['reinforcement_events'] = [dict(event) for event in conn.execute(
        "SELECT id,action,at FROM memory_v2_events WHERE entity_id=? AND action IN ('user_confirm','user_remention') ORDER BY at,id",
        (row['id'],))]
    rule_row=conn.execute("SELECT value FROM memory_v2_meta WHERE key='decay-settings'").fetchone()
    rules=json.loads(rule_row[0]) if rule_row else {}
    result["activation"] = activation(result, at, rules)
    result["eligible"] = _effective(conn, row) if current else _historical_effective(conn, row, doc)
    result["half_life_days"] = (None if doc["layer"] in (3, 4) else
                                rules.get("short_days",1) if doc["layer"] == 1 else rules.get("important_days",180) if doc["importance"] >= rules.get("important_threshold",.7) else rules.get("long_days",30))
    result["source_character_id"] = doc["source"].get("character_id", doc["source"].get("source_character_id"))
    result["character_sources"] = sorted({str(character) for version in conn.execute(
        "SELECT document FROM memory_v2_versions WHERE entity_id=?", (row["id"],))
        for source in (json.loads(version[0])["source"],)
        for character in (source.get("character_id", source.get("source_character_id")),) if character is not None})
    result["relations"] = []
    for relation in conn.execute(
        "SELECT id,from_id,to_id,kind,label,score,revision FROM memory_v2_relations "
        "WHERE status='active' AND (from_id=? OR to_id=?) ORDER BY id", (row["id"], row["id"])):
        other_id = relation["to_id"] if relation["from_id"] == row["id"] else relation["from_id"]
        other = conn.execute("SELECT * FROM memory_v2_entities WHERE id=?", (other_id,)).fetchone()
        if other and _effective(conn, dict(other)):
            result["relations"].append(dict(relation))
    for relation in result["relations"]:
        relation["asserted"] = relation["kind"] == "confirmed"
    return result


def _event(conn, entity_id, action, document, event_id=None):
    eid = event_id or _id("event-")
    conn.execute("INSERT INTO memory_v2_events VALUES(?,?,?,?,?)", (eid, entity_id, action, dump(document), now()))
    return eid


def _write_version(conn, row, document, source_id, action):
    revision = row["revision"] + 1
    stamp = now()
    conn.execute("INSERT INTO memory_v2_versions VALUES(?,?,?,?,?,?)", (
        row["id"], revision, dump(document), source_id, stamp, row["current_version"]))
    conn.execute("""UPDATE memory_v2_entities SET key=?,layer=?,category=?,importance=?,pinned=?,
        revision=?,current_version=?,updated_at=? WHERE id=? AND revision=?""", (
        document["key"], document["layer"], document["category"], document["importance"], int(document["pinned"]),
        revision, revision, stamp, row["id"], row["revision"]))
    conn.execute("DELETE FROM memory_v2_vectors WHERE entity_id=?", (row["id"],))
    _event(conn, row["id"], action, {"revision": revision})
    return _projection(conn, _row(conn, row["id"]))


def create(body, db=None, *, _core_authorized=False):
    """Create a current entity. source=None means an explicit manual user CRUD operation."""
    if not isinstance(body, dict):
        raise MemoryError("请求必须为对象")
    if body.get("core_confirmation") is not None and not _core_authorized:
        raise MemoryError("核心确认只能由独立用户确认接口写入")
    if set(body) - {"scope", "session_id", "key", "value", "layer", "category", "importance", "pinned",
                    "occurred_at", "confirmed_at", "source", "id", "core_confirmation", "tags", "valence", "arousal"}:
        raise MemoryError("包含未知字段")
    scope = body.get("scope", "companion")
    data = _validate({"scope": scope, "owner_id": owner(scope, body.get("session_id")),
        "key": body.get("key"), "value": body.get("value"), "layer": body.get("layer", 2),
        "category": body.get("category", "experience"), "importance": body.get("importance", .5),
        "pinned": body.get("pinned", False), "occurred_at": body.get("occurred_at"),
        "confirmed_at": body.get("confirmed_at"), "core_confirmation": body.get("core_confirmation"),
        "tags": body.get("tags",[]), "valence":body.get("valence"), "arousal":body.get("arousal")}, _core_authorized)
    with _connection(db, write=True) as conn:
        if _blocked(conn, {**data, "source": body.get("source") or {}}):
            raise Conflict("内容或来源已被彻底忘记")
        source_id, source = _record_source(conn, body.get("source"), default_manual=True)
        if any(value != scope for value in source.get("message_scopes", ())):
            raise MemoryError("跨范围聊天来源必须先在私域建立记忆再显式share")
        if scope == "work" and any(value != data["owner_id"] for value in source.get("message_sessions", ())):
            raise MemoryError("工作聊天来源必须属于同一工作会话")
        for parent in source.get("parent_ids", ()):
            parent_row = _row(conn, parent)
            if scope != parent_row["scope"] or data["owner_id"] != parent_row["owner_id"]:
                raise MemoryError("跨范围派生必须经显式share授权")
        data["source"] = source
        stamp = now()
        entity_id = body.get("id") or _id("mem-")
        if not isinstance(entity_id, str) or len(entity_id) > 200:
            raise MemoryError("无效ID")
        try:
            conn.execute("INSERT INTO memory_v2_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                entity_id, scope, data["owner_id"], data["key"], data["layer"], data["category"],
                data["importance"], int(data["pinned"]), "active", 1, 1, stamp, stamp, None, 0))
        except sqlite3.IntegrityError as exc:
            raise Conflict("同范围key或ID已存在，请使用CAS更新/恢复") from exc
        conn.execute("INSERT INTO memory_v2_versions VALUES(?,?,?,?,?,NULL)", (entity_id, 1, dump(data), source_id, stamp))
        for parent in source.get("parent_ids", ()):
            conn.execute("INSERT INTO memory_v2_dependencies VALUES(?,?,?)", (parent, entity_id, "derived"))
        _event(conn, entity_id, "create", {"revision": 1})
        return _projection(conn, _row(conn, entity_id))


def get(entity_id, db=None, revision=None):
    with _connection(db) as conn:
        row = _row(conn, entity_id)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise MemoryError("无效历史版本")
        result = _projection(conn, row, revision=revision)
        if revision is not None and result.get("redacted") and row["status"] != "forgotten":
            raise NotFound("历史版本不存在")
        return result


def update(entity_id, body, db=None, *, _core_authorized=False):
    allowed = {"expected_revision", "key", "value", "layer", "category", "importance", "pinned",
               "occurred_at", "confirmed_at", "source", "core_confirmation", "tags", "valence", "arousal"}
    if not isinstance(body, dict) or set(body) - allowed or "expected_revision" not in body:
        raise MemoryError("需要expected_revision，且不允许修改scope/owner")
    if "core_confirmation" in body and not _core_authorized:
        raise MemoryError("核心确认不能由普通CRUD伪造")
    with _connection(db, write=True) as conn:
        row = _row(conn, entity_id, body["expected_revision"])
        if row["status"] != "active":
            raise Conflict("请先恢复已删除记忆；彻底忘记不可恢复")
        doc = _document(conn, row)
        data = {**doc, **{k: v for k, v in body.items() if k not in ("expected_revision", "source")}}
        # Editing core content is a new core version and requires a new user confirmation.
        authorized = _core_authorized or (doc["layer"] == 4 and
            not any(k in body for k in ("value", "key", "layer", "category", "core_confirmation", "tags", "valence", "arousal")))
        data = _validate(data, authorized)
        if _blocked(conn, {**data, "source": body.get("source", doc["source"])}):
            raise Conflict("内容或来源已被彻底忘记")
        original_source = doc["source"]
        shared_projection = bool(original_source.get("kind") == "memory" and
            conn.execute("SELECT 1 FROM memory_v2_dependencies WHERE child_id=? AND kind='share'", (row["id"],)).fetchone())
        if shared_projection:
            if "source" in body and body["source"] != original_source:
                raise MemoryError("共享投影不能借授权更换来源；请重新显式share")
            metadata = {"expected_revision", "pinned", "importance", "occurred_at", "confirmed_at", "source"}
            if set(body) - metadata:
                raise MemoryError("共享投影仅可编辑元数据；内容、层级或来源变更须重新显式share")
        source_id, source = _record_source(conn, body.get("source", original_source))
        has_authorized_parent = bool(shared_projection and _source_effective(conn, row, doc))
        if any(value != row["scope"] for value in source.get("message_scopes", ())) and not has_authorized_parent:
            raise MemoryError("跨范围聊天来源必须经显式share")
        if row["scope"] == "work" and any(value != row["owner_id"] for value in source.get("message_sessions", ())):
            raise MemoryError("工作聊天来源必须属于同一工作会话")
        if source.get("parent_ids", []) != doc["source"].get("parent_ids", []):
            raise MemoryError("不允许在编辑时改写派生链；请新建真实来源记录")
        data["source"] = source
        result = _write_version(conn, row, data, source_id, "update")
        # A source edit invalidates an old authorization to expose its old value.
        conn.execute("UPDATE memory_v2_grants SET status='revoked' WHERE source_id=?", (entity_id,))
        return result


def restore_content_version(entity_id, expected_revision, version_revision, *,
                            user_confirmed=False, operation_id=None, db=None):
    """Explicitly copy eligible historical content into a NEW current version.

    This is neither deletion undo nor entity-revision rewind. Preserve current
    ordinary layer/importance/pin and reinforcement history; never revive core
    confirmation, sharing grants, withdrawn provenance, or forgotten content.
    """
    if not isinstance(entity_id, str) or not 1 <= len(entity_id) <= 200:
        raise MemoryError("无效记忆ID")
    for value in (expected_revision, version_revision):
        if type(value) is not int or not 1 <= value <= 2147483647:
            raise MemoryError("版本必须是1～2147483647整数；历史版本指内容版本")
    if user_confirmed is not True:
        raise MemoryError("恢复历史内容必须由用户单独明确确认")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", operation_id):
        raise MemoryError("operation_id需8～128位ASCII字母数字、下划线或短横线")
    intent = {"entity_id": entity_id, "expected_revision": expected_revision,
              "version_revision": version_revision}
    intent_hash = fingerprint(intent)
    event_id = "restore-version-" + operation_id
    with _connection(db, write=True) as conn:
        if not _v2_authority(conn):
            raise Conflict("仅在V2已明确激活后恢复历史内容")
        existing = conn.execute("SELECT * FROM memory_v2_events WHERE id=?", (event_id,)).fetchone()
        if existing:
            event = json.loads(existing["document"])
            if (existing["entity_id"] != entity_id or existing["action"] != "restore_content_version"
                    or event.get("intent_fingerprint") != intent_hash):
                raise Conflict("operation_id已用于不同的历史恢复请求")
            row = _row(conn, entity_id)
            if (row["revision"] != event.get("result_revision")
                    or row["current_version"] != event.get("result_version") or not _effective(conn, row)):
                raise Conflict("历史恢复结果已变化或来源失效；不能重放旧操作")
            return _projection(conn, row)
        row = _row(conn, entity_id, expected_revision)
        if row["status"] != "active":
            raise Conflict("请先恢复普通删除；彻底忘记的历史内容不可恢复")
        current = _document(conn, row)
        if row["scope"] == "shared" or conn.execute(
                "SELECT 1 FROM memory_v2_dependencies WHERE child_id=? AND kind='share'", (entity_id,)).fetchone():
            raise Conflict("共享投影不能恢复历史内容；须从来源重新明确授权共享")
        if row["layer"] == 4:
            raise Conflict("核心内容必须通过独立核心确认修改，不能由历史恢复绕过")
        historical = _document(conn, row, version_revision)
        if historical is None:
            raise NotFound("历史内容版本不存在；请使用内容version_id中的版本号")
        if version_revision == row["current_version"]:
            raise Conflict("选择的是当前内容版本，不是历史版本")
        if (not isinstance(current, dict) or not isinstance(historical, dict)
                or not isinstance(current.get("source"), dict) or not isinstance(historical.get("source"), dict)
                or not all(key in historical for key in ("key", "value", "category", "scope", "owner_id"))):
            raise Conflict("历史或当前内容格式异常，不能恢复")
        try:
            # Old/imported documents must still be finite JSON; do not propagate
            # NaN/Infinity or malformed provenance through a new authoritative edit.
            json.dumps({"current": current, "historical": historical}, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise Conflict("历史或当前内容不是有限JSON，不能恢复") from exc
        if historical.get("layer") == 4 or historical.get("core_confirmation") is not None or current.get("core_confirmation") is not None:
            raise Conflict("历史核心内容必须经独立核心确认，不可直接恢复")
        if (not _effective(conn, row) or not _historical_effective(conn, row, historical)
                or not historical.get("source", {}).get("known")):
            raise Conflict("当前或历史来源不可验证、已撤回或被彻底忘记，禁止恢复")
        if (historical.get("scope"), historical.get("owner_id")) != (row["scope"], row["owner_id"]):
            raise Conflict("历史内容归属不匹配，禁止跨范围恢复")
        if historical["source"].get("parent_ids", []) != current["source"].get("parent_ids", []):
            raise Conflict("历史派生链已不同；请新建经验证的来源记录，不改写依赖")
        body = {"expected_revision": expected_revision,
                "key": historical["key"], "value": historical["value"],
                "category": historical["category"], "occurred_at": historical.get("occurred_at"),
                "tags":historical.get("tags",[]), "valence":historical.get("valence"), "arousal":historical.get("arousal"),
                "confirmed_at": None, "source": historical["source"]}
        try:
            # Reuse source verification, dependency/privacy rules, version creation,
            # vector invalidation, and outgoing-share revocation from normal editing.
            result = update(entity_id, body, conn)
        except sqlite3.IntegrityError as exc:
            raise Conflict("恢复后的同范围标题已被其他记忆占用；未修改当前内容") from exc
        _event(conn, entity_id, "restore_content_version", {
            "intent_fingerprint": intent_hash, "from_content_version": version_revision,
            "result_revision": result["revision"], "result_version": result["revision"],
            "sharing_restored": False, "reinforced": False}, event_id)
        return result


# Explicit alias; deletion undo remains restore(), never dispatches here.
restore_version = restore_content_version


def confirm_core(entity_id, expected_revision, *, user_confirmed=False, confirmation_id=None, value=None, db=None):
    if user_confirmed is not True or not isinstance(confirmation_id, str) or not confirmation_id.strip():
        raise MemoryError("核心必须由用户手动确认并提供confirmation_id")
    with _connection(db, write=True) as conn:
        existing = conn.execute("SELECT * FROM memory_v2_events WHERE id=?", ("core-" + confirmation_id,)).fetchone()
        if existing:
            if existing["entity_id"] != entity_id:
                raise Conflict("confirmation_id已用于其他记忆")
            return get(entity_id, conn)
        row = _row(conn, entity_id, expected_revision)
        body = {"expected_revision": expected_revision, "layer": 4, "confirmed_at": now(),
                "core_confirmation": {"actor": "user", "id": confirmation_id, "at": now()}}
        if value is not None:
            body["value"] = value
        result = update(entity_id, body, conn, _core_authorized=True)
        _event(conn, entity_id, "user_core_confirm", {"revision": result["revision"]}, "core-" + confirmation_id)
        return result


def _revoke(conn, entity_id):
    conn.execute("UPDATE memory_v2_grants SET status='revoked' WHERE source_id=? OR target_id=?", (entity_id, entity_id))
    conn.execute("DELETE FROM memory_v2_vectors WHERE entity_id=?", (entity_id,))


def delete(entity_id, expected_revision, db=None):
    with _connection(db, write=True) as conn:
        row = _row(conn, entity_id, expected_revision)
        if row["status"] != "active":
            raise Conflict("仅可删除有效记忆")
        conn.execute("UPDATE memory_v2_entities SET status='deleted',revision=revision+1,updated_at=? WHERE id=?", (now(), entity_id))
        _revoke(conn, entity_id)
        _event(conn, entity_id, "delete", {"restorable": True})
        return get(entity_id, conn)


def restore(entity_id, expected_revision, db=None):
    with _connection(db, write=True) as conn:
        row = _row(conn, entity_id, expected_revision)
        if row["status"] != "deleted":
            raise Conflict("只能恢复普通删除；彻底忘记不可恢复")
        if _blocked(conn, {**(_document(conn, row) or {}), "id": entity_id}):
            raise Conflict("来源已被彻底忘记")
        conn.execute("UPDATE memory_v2_entities SET status='active',revision=revision+1,updated_at=? WHERE id=?", (now(), entity_id))
        _revoke(conn, entity_id)  # Never reanimate sharing after source restoration.
        _event(conn, entity_id, "restore", {"sharing_restored": False})
        return get(entity_id, conn)


def reinforce(entity_id, expected_revision, action, event_id, *, user_initiated=False, db=None):
    if action not in REINFORCEMENT_ACTIONS or user_initiated is not True:
        raise MemoryError("只有用户主动重提/确认可以强化；浏览、扫描、检索不会强化")
    if not isinstance(event_id, str) or not event_id.strip():
        raise MemoryError("强化需要幂等event_id")
    with _connection(db, write=True) as conn:
        existing = conn.execute("SELECT * FROM memory_v2_events WHERE id=?", ("reinforce-" + event_id,)).fetchone()
        if existing:
            if existing["entity_id"] != entity_id or existing["action"] != action:
                raise Conflict("强化event_id冲突")
            return get(entity_id, conn)
        row = _row(conn, entity_id, expected_revision)
        if not _effective(conn, row):
            raise Conflict("不能强化已删除、失效来源或已忘记记忆")
        stamp = now()
        conn.execute("UPDATE memory_v2_entities SET reinforced_at=?,reinforcement_count=reinforcement_count+1,"
                     "revision=revision+1,updated_at=? WHERE id=?", (stamp, stamp, entity_id))
        _event(conn, entity_id, action, {"actor": "user"}, "reinforce-" + event_id)
        return get(entity_id, conn)


def _visible(row, mode, session_id):
    if mode is None:
        return True
    if mode not in SCOPES:
        raise MemoryError("无效mode")
    if mode == "companion":
        return row["scope"] in ("companion", "shared")
    if mode == "shared":
        return row["scope"] == "shared"
    # Work may consume only explicitly shared, non-private sources. Long-term
    # work/shared memory is restricted to operation habits and the project roster.
    if row["scope"] == "shared":
        return row["layer"] < 3 or row["category"] in WORK_LONG_TERM
    return row["scope"] == "work" and (
        row["owner_id"] == owner("work", session_id)
        or row["layer"] >= 3 and row["category"] in WORK_LONG_TERM
    )


def _all(conn, *, scope=None, mode=None, session_id=None, layer=None, category=None,
         include_deleted=False, at=None, pinned=None, query="", before=None):
    if pinned is not None and type(pinned) is not bool:raise MemoryError("pinned必须为布尔值")
    if not isinstance(query,str) or len(query)>16000:raise MemoryError("query限16000字")
    cutoff=_timestamp(before) if before else None
    if scope is not None and scope not in SCOPES:
        raise MemoryError("无效scope")
    if layer is not None and layer not in LAYERS:
        raise MemoryError("无效layer")
    if mode == "work":
        owner(mode, session_id)
    # Narrow candidates using the existing scope/owner index. Visibility, source
    # ancestry and effective-state checks below remain the authority.
    clauses, parameters = [], []
    for field, value in [("scope", scope), ("layer", layer), ("category", category)]:
        if value is not None:
            clauses.append(field + "=?")
            parameters.append(value)
    if scope == "work":
        clauses.append("owner_id=?")
        parameters.append(owner(scope, session_id))
    statement = "SELECT * FROM memory_v2_entities"
    if clauses:
        statement += " WHERE " + " AND ".join(clauses)
    statement += " ORDER BY id"
    result = []
    for raw in conn.execute(statement, parameters):
        row = dict(raw)
        if scope is not None and row["scope"] != scope:
            continue
        if scope == "work" and row["owner_id"] != owner(scope, session_id):
            continue
        if not _visible(row, mode, session_id) or (layer is not None and row["layer"] != layer):
            continue
        if mode == "work" and row["scope"] == "work" and _fact_ancestor(conn, row, "work", row["owner_id"]):
            continue
        if category is not None and row["category"] != category:
            continue
        if row["status"] == "forgotten" or (not include_deleted and not _effective(conn, row)):
            continue
        item=_projection(conn,row,at)
        if pinned is not None and item["pinned"] is not pinned:continue
        if cutoff and _timestamp(item.get("occurred_at") or item["created_at"])>cutoff:continue
        text=" ".join([item["key"],item["value"],*item.get("tags",[])]).casefold()
        if any(term not in text for term in query.casefold().split()):continue
        result.append(item)
    # Pin is a display priority only; it never changes activation/core status.
    return sorted(result, key=lambda i: (not i["pinned"], -i["layer"], -i["importance"], i["id"]))


def list_items(*, scope=None, mode=None, session_id=None, layer=None, category=None,
               offset=0, limit=100, include_deleted=False, at=None, db=None, pinned=None, query="", before=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise MemoryError("offset>=0，limit为1～200整数")
    with _connection(db) as conn:
        items = _all(conn, scope=scope, mode=mode, session_id=session_id, layer=layer, category=category,
                     include_deleted=include_deleted, at=at,pinned=pinned,query=query,before=before)
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
                "projection": "memory_v2", "read_only": True}


def graph(**kwargs):
    """Exactly the same paginated projection as list; no invented graph particles."""
    conn_arg = kwargs.get("db")
    page = list_items(**kwargs)
    ids = {item["id"] for item in page["items"]}
    with _connection(conn_arg) as conn:
        edges = [dict(r) for r in conn.execute("SELECT * FROM memory_v2_relations WHERE status='active' ORDER BY id")
                 if r["from_id"] in ids and r["to_id"] in ids]
    for edge in edges:
        edge["asserted"] = edge["kind"] == "confirmed"
    return {**page, "nodes": page["items"], "edges": edges,
            "edge_policy": "confirmed=fact_with_provenance; similarity=non_asserted_candidate"}


def versions(entity_id, *, offset=0, limit=100, db=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise MemoryError("无效分页")
    with _connection(db) as conn:
        row = _row(conn, entity_id)
        count = conn.execute("SELECT COUNT(*) FROM memory_v2_versions WHERE entity_id=?", (entity_id,)).fetchone()[0]
        revisions = conn.execute("SELECT revision FROM memory_v2_versions WHERE entity_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                                 (entity_id, limit, offset)).fetchall()
        return {"items": [_projection(conn, row, revision=r[0]) for r in revisions], "total": count,
                "offset": offset, "limit": limit, "redacted": row["status"] == "forgotten"}


def add_relation(from_id, to_id, kind, label, *, score=None, source=None, user_confirmed=False,
                 expected_from_revision=None, expected_to_revision=None, db=None):
    if kind not in ("confirmed", "similarity") or not isinstance(label, str) or not 1 <= len(label) <= 160:
        raise MemoryError("关系必须区分confirmed与similarity")
    if from_id == to_id:
        raise MemoryError("不允许自环关系")
    if kind == "confirmed" and user_confirmed is not True:
        raise MemoryError("事实关系需用户明确确认；相似关系不能升级成事实")
    if score is not None and (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
        raise MemoryError("无效相似度")
    with _connection(db, write=True) as conn:
        a = _row(conn, from_id, expected_from_revision)
        b = _row(conn, to_id, expected_to_revision)
        if not _effective(conn, a) or not _effective(conn, b):
            raise Conflict("关系端点不可用")
        if (a["scope"], a["owner_id"]) != (b["scope"], b["owner_id"]):
            raise MemoryError("关系不可跨越隐私范围")
        sid, _ = _record_source(conn, source or {"kind": "manual", "actor": "user"} if kind == "confirmed" else
                                source or {"kind": "unknown", "origin": "本地相似度候选，非事实"})
        rid = _id("rel-")
        conn.execute("INSERT INTO memory_v2_relations VALUES(?,?,?,?,?,?,?,?,?,?)", (
            rid, from_id, to_id, kind, label, score, sid, 1, "active", now()))
        _event(conn, from_id, "relation_" + kind, {"relation_id": rid, "to_id": to_id})
        return {"id": rid, "from_id": from_id, "to_id": to_id, "kind": kind, "label": label,
                "score": score, "revision": 1, "asserted": kind == "confirmed"}


def update_relation(relation_id, expected_revision, *, label=None, score=None, deleted=False, db=None):
    """CAS metadata/delete only; a similarity edge cannot be promoted to a fact."""
    if type(expected_revision) is not int or type(deleted) is not bool:
        raise MemoryError("关系修改需要expected_revision与布尔deleted")
    with _connection(db, write=True) as conn:
        row = conn.execute("SELECT * FROM memory_v2_relations WHERE id=?", (relation_id,)).fetchone()
        if not row:
            raise NotFound("关系不存在")
        if row["revision"] != expected_revision:
            raise Conflict("关系版本已变化")
        if label is not None and (not isinstance(label, str) or not 1 <= len(label) <= 160):
            raise MemoryError("关系label限1～160字")
        if score is not None and (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
            raise MemoryError("无效关系score")
        if row["kind"] == "confirmed" and label is not None:
            raise MemoryError("更改事实关系含义需重新用户确认新关系，不可普通编辑")
        conn.execute("UPDATE memory_v2_relations SET label=?,score=?,status=?,revision=revision+1 WHERE id=?", (
            row["label"] if label is None else label, row["score"] if score is None else score,
            "deleted" if deleted else row["status"], relation_id))
        result = dict(conn.execute("SELECT * FROM memory_v2_relations WHERE id=?", (relation_id,)).fetchone())
        result["asserted"] = result["kind"] == "confirmed"
        return result


def share(entity_id, expected_revision, *, user_confirmed=False, target_key=None, db=None):
    """Explicit grant creates a same-DB projection; restoration never revives it."""
    if user_confirmed is not True:
        raise MemoryError("共享需要用户重新明确授权")
    with _connection(db, write=True) as conn:
        row = _row(conn, entity_id, expected_revision)
        if row["scope"] == "shared" or not _effective(conn, row):
            raise Conflict("仅可共享有效私域记忆")
        doc = _document(conn, row)
        if doc["layer"] == 4:
            raise MemoryError("核心不能借共享绕过目标范围手动确认；请单独建立并确认")
        key = target_key or doc["key"]
        target = conn.execute("SELECT * FROM memory_v2_entities WHERE scope='shared' AND owner_id='global' AND key=?", (key,)).fetchone()
        grant_source = {"kind": "memory", "parent_ids": [entity_id], "actor": "user", "authorized": True}
        # Create in a same-scope temporary source, then establish the explicitly
        # authorized cross-scope dependency inside this atomic transaction.
        body = {k: doc[k] for k in ("key", "value", "layer", "category", "importance", "pinned", "occurred_at", "confirmed_at")}
        body.update(tags=doc.get("tags",[]),valence=doc.get("valence"),arousal=doc.get("arousal"))
        body.update(scope="shared", key=key, source={"kind": "manual", "actor": "user"})
        if target:
            trow = dict(target)
            if trow["status"] != "active":
                raise Conflict("共享目标已删除或忘记；不隐式恢复")
            # Never overwrite a separately authored shared memory silently.
            if not conn.execute("SELECT 1 FROM memory_v2_grants WHERE source_id=? AND target_id=?", (entity_id, trow["id"])).fetchone():
                raise Conflict("共享目标已存在，需选择不同target_key")
            source_id, normalized = _record_source(conn, grant_source)
            data = {**_document(conn, trow), **{k: v for k, v in body.items() if k not in ("source", "scope")}, "source": normalized}
            _write_version(conn, trow, data, source_id, "share_reauthorize")
            target_id = trow["id"]
        else:
            result = create(body, conn)
            target_id = result["id"]
            source_id, normalized = _record_source(conn, grant_source)
            data = {**_document(conn, _row(conn, target_id)), "source": normalized}
            conn.execute("UPDATE memory_v2_versions SET document=?,source_id=? WHERE entity_id=? AND revision=1",
                         (dump(data), source_id, target_id))
        conn.execute("INSERT OR IGNORE INTO memory_v2_dependencies VALUES(?,?,?)", (entity_id, target_id, "share"))
        conn.execute("UPDATE memory_v2_grants SET status='revoked' WHERE target_id=?", (target_id,))
        conn.execute("INSERT INTO memory_v2_grants VALUES(?,?,?,?,?,?)", (_id("grant-"), entity_id, target_id, row["revision"], "active", now()))
        _event(conn, entity_id, "share_authorized", {"target_id": target_id})
        return get(target_id, conn)


def candidate_filter(candidates, *, mode=None, session_id=None, require_provenance=True, db=None):
    """Apply before AND after local ranking/extraction; never strengthen memories.

    Automatic candidates without provenance fail closed. Manual user CRUD uses
    create/update instead. Similarity results remain evidence candidates, not facts.
    """
    with _connection(db) as conn:
        result = []
        for raw in candidates:
            item = dict(raw)
            if _blocked(conn, item):
                continue
            eid = item.get("entity_id", item.get("id"))
            entity = conn.execute("SELECT * FROM memory_v2_entities WHERE id=?", (eid,)).fetchone() if eid else None
            if entity:
                row = dict(entity)
                if not _effective(conn, row) or not _visible(row, mode, session_id):
                    continue
                if mode == "work" and row["scope"] == "work" and _fact_ancestor(conn, row, "work", row["owner_id"]):
                    continue
                if item.get("revision") is not None and item["revision"] != row["revision"]:
                    continue
                item = {**item, **_projection(conn, row)}
            else:
                source = item.get("source") or {}
                proof_messages = list(source.get("message_ids", ())) + list(item.get("message_ids", ()))
                if item.get("message_id") is not None:
                    proof_messages.append(item["message_id"])
                if source.get("message_id") is not None:
                    proof_messages.append(source["message_id"])
                if proof_messages:
                    try:
                        _, verified_source = _record_source(conn, {**source, "kind": "message", "message_ids": proof_messages}, persist=False)
                    except MemoryError:
                        continue
                    if mode == "work" and (any(s != "work" for s in verified_source.get("message_scopes", ())) or
                            any(s != str(session_id) for s in verified_source.get("message_sessions", ()))):
                        continue
                    if mode == "companion" and any(s != "companion" for s in verified_source.get("message_scopes", ())):
                        continue
                if source.get("parent_ids"):
                    try:
                        if any(not _visible(_row(conn, parent), mode, session_id) for parent in source["parent_ids"]):
                            continue
                        _record_source(conn, {**source, "kind": "memory"}, persist=False)
                    except MemoryError:
                        continue
                refs = _source_refs(source) | _source_refs(item)
                trusted_ref = any(conn.execute("SELECT 1 FROM memory_v2_sources WHERE ref=? OR id=?", (ref, ref)).fetchone() or
                    conn.execute("SELECT 1 FROM memory_v2_migrations WHERE source_ref=? AND status='complete'", (ref,)).fetchone()
                    for ref in refs)
                if require_provenance and not (proof_messages or source.get("parent_ids") or trusted_ref):
                    continue
                if mode == "work" and (item.get("scope") != "work" or str(item.get("owner_id", item.get("session_id"))) != str(session_id)):
                    # Shared automatic candidates require an actual V2 entity/grant.
                    continue
                if mode == "companion" and item.get("scope") == "work":
                    continue
            result.append(item)
        return result


def filter_history(documents, *, mode=None, session_id=None, db=None):
    """Filter raw recent history, RAG history and old source-backed summaries.

    Preserve actual message IDs in callers: bare role/content history cannot prove
    that a paraphrased forgotten source was removed. Fail closed on absent IDs.
    """
    return candidate_filter(documents, mode=mode, session_id=session_id, require_provenance=True, db=db)


def put_vector(entity_id, expected_revision, vector, *, adapter="local", db=None):
    if not isinstance(adapter, str) or not 1 <= len(adapter) <= 160:
        raise MemoryError("无效本地向量adapter")
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= 8192 or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise MemoryError("向量必须为有限数值列表")
    if not any(x != 0 for x in vector):
        raise MemoryError("不允许零向量")
    with _connection(db, write=True) as conn:
        row = _row(conn, entity_id, expected_revision)
        if not _effective(conn, row):
            raise Conflict("向量来源不可用")
        conn.execute("INSERT OR REPLACE INTO memory_v2_vectors VALUES(?,?,?,?)", (entity_id, expected_revision, adapter, dump(vector)))
        return {"entity_id": entity_id, "revision": expected_revision, "adapter": adapter, "dimensions": len(vector)}


def _cosine(a, b):
    if len(a) != len(b):
        return None
    denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else None


def collect_context(mode, session_id=None, query="", *, history=False, limit=20,
                    vector_candidates=None, query_vector=None, adapter="local", at=None, db=None):
    """Keyword + optional offline-vector candidates, revalidated against authority.

    Returns ALL accessible current project names separately from ranked top-k.
    history=True searches V2 superseded versions, explicitly labelled non-current.
    This never calls an embedding/model adapter; the caller supplies local vectors.
    """
    if mode not in SCOPES or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise MemoryError("无效上下文范围或limit")
    if not isinstance(query, str) or len(query) > 16000:
        raise MemoryError("query限16000字")
    if query_vector is not None and (not isinstance(query_vector, (list, tuple)) or not query_vector or
            any(type(x) not in (int, float) or not math.isfinite(x) for x in query_vector)):
        raise MemoryError("无效查询向量")
    with _connection(db) as conn:
        current = _all(conn, mode=mode, session_id=session_id, at=at)
        projects = [{k: item[k] for k in ("id", "key", "value", "scope", "owner_id", "revision", "source_character_id")}
                    for item in current if item["category"] == "project"]
        documents = list(current)
        if history:
            for item in current:
                row = _row(conn, item["id"])
                for rev in conn.execute("SELECT revision FROM memory_v2_versions WHERE entity_id=? AND revision!=?",
                                        (item["id"], row["current_version"])):
                    historical = _projection(conn, row, at, rev[0])
                    if historical["eligible"] and not _blocked(conn, historical):
                        documents.append(historical)
        vectors = {}
        if vector_candidates is not None:
            for candidate in candidate_filter(vector_candidates, mode=mode, session_id=session_id, db=conn):
                score = candidate.get("score", candidate.get("vector_score"))
                if type(score) in (int, float) and math.isfinite(score):
                    vectors[candidate["id"]] = max(-1.0, min(1.0, score))
        if query_vector is not None:
            for row in conn.execute("SELECT * FROM memory_v2_vectors WHERE adapter=?", (adapter,)):
                item = next((i for i in current if i["id"] == row["entity_id"] and i["revision"] == row["revision"]), None)
                if item:
                    score = _cosine(query_vector, json.loads(row["vector"]))
                    if score is not None:
                        vectors[item["id"]] = score
        terms = re.findall(r"[\w]+", query.casefold())
        ranked = []
        for item in documents:
            haystack = (item["key"] + " " + item["value"]+" "+" ".join(item.get("tags",[]))).casefold()
            keyword = sum(1 for term in terms if term in haystack) / max(1, len(terms))
            if query and query.casefold() in haystack:
                keyword = 1.0
            semantic = vectors.get(item["id"], 0) if item["is_current"] else 0
            if query and keyword == 0 and semantic <= 0:
                continue
            score = keyword * .6 + max(0, semantic) * .3 + item["importance"] * .06 + item["activation"] * .04
            # L3/L4 never disappear due to low activation. No activation threshold
            # is applied to L1/L2 either; decay influences ranking, not deletion.
            ranked.append({**item, "score": round(score, 9), "keyword_score": keyword,
                           "vector_score": semantic, "evidence_kind": "memory" if item["is_current"] else "historical_memory"})
        ranked.sort(key=lambda item: (-item["score"], not item["is_current"], item["id"], -item["revision"]))
        return {"items": ranked[:limit], "total": len(ranked), "projects": projects,
                "project_roster_complete": True, "history": history, "read_only": True,
                "method": "keyword+local_vector_candidates", "facts_policy": "similarity_is_not_fact"}


def _closure(conn, roots):
    found = set(roots)
    pending = list(roots)
    while pending:
        parent = pending.pop()
        for row in conn.execute("SELECT child_id FROM memory_v2_dependencies WHERE parent_id=?", (parent,)):
            if row[0] not in found:
                found.add(row[0])
                pending.append(row[0])
    return sorted(found)


def _forget_plan(conn, entity_id):
    _row(conn, entity_id)
    ids = set(_closure(conn, [entity_id]))
    # A single old message/batch can produce several independent entities. They
    # are all derived evidence, even if an old writer omitted a parent edge.
    # Expand transitively and expose the entire impact in preview, not silently
    # leave same-source vectors/relations retrievable behind a sibling entity.
    while True:
        selected_sources, selected_messages, selected_refs = set(), set(), set()
        for eid in ids:
            for version in conn.execute("SELECT source_id,document FROM memory_v2_versions WHERE entity_id=?", (eid,)):
                selected_sources.add(version[0])
                source = json.loads(version[1])["source"]
                selected_messages.update(str(mid) for mid in source.get("message_ids", ()))
                selected_refs.update(_source_refs(source))
        expanded = set(ids)
        for version in conn.execute("SELECT entity_id,source_id,document FROM memory_v2_versions"):
            if version[0] in ids:
                continue
            source = json.loads(version[2])["source"]
            if (version[1] in selected_sources or selected_messages.intersection(str(mid) for mid in source.get("message_ids", ())) or
                    selected_refs.intersection(_source_refs(source))):
                expanded.update(_closure(conn, [version[0]]))
        if expanded == ids:
            break
        ids = expanded
    ids = sorted(ids)
    plan = {"root": entity_id, "entities": [], "sources": [], "messages": [], "source_refs": [], "contents": [], "relations": []}
    sources, messages, refs, contents = set(), set(), set(), set()
    for eid in ids:
        row = _row(conn, eid)
        plan["entities"].append({"id": eid, "revision": row["revision"], "status": row["status"]})
        for version in conn.execute("SELECT * FROM memory_v2_versions WHERE entity_id=?", (eid,)):
            sources.add(version["source_id"])
            doc = json.loads(version["document"])
            contents.add(content_hash(doc["value"]))
            contents.add(content_hash(doc["key"] + "：" + doc["value"]))
            contents.add(content_hash(doc["key"] + ": " + doc["value"]))
            source = doc["source"]
            messages.update(str(mid) for mid in source.get("message_ids", ()))
            refs.update(_source_refs(source))
        for migration in conn.execute("SELECT source_ref FROM memory_v2_migrations WHERE entity_id=?", (eid,)):
            refs.add(migration[0])
    for relation in conn.execute("SELECT * FROM memory_v2_relations ORDER BY id"):
        if relation["from_id"] in ids or relation["to_id"] in ids or relation["source_id"] in sources:
            plan["relations"].append({"id": relation["id"], "revision": relation["revision"]})
            sources.add(relation["source_id"])
    for sid in sources:
        row = conn.execute("SELECT * FROM memory_v2_sources WHERE id=?", (sid,)).fetchone()
        if row:
            refs.add(row["ref"])
    plan.update(sources=sorted(sources), messages=sorted(messages), source_refs=sorted(refs), contents=sorted(contents))
    return plan


def preview_forget(entity_id, db=None):
    with _connection(db, write=True) as conn:
        if _row(conn, entity_id)["status"] == "forgotten":
            raise Conflict("记忆已经彻底忘记")
        plan = _forget_plan(conn, entity_id)
        operation_id = _id("forget-")
        token = fingerprint({"operation_id": operation_id, "plan": plan})
        conn.execute("INSERT INTO memory_v2_forgets VALUES(?,?,?,?,?,?)", (operation_id, dump(plan), token, "preview", None, now()))
        return {"operation_id": operation_id, "token": token, "entity_ids": [r["id"] for r in plan["entities"]],
                "derived_count": len(plan["entities"]) - 1, "source_message_ids": plan["messages"],
                "scope": ["v2_current_and_versions", "derived_memories", "relations", "local_indexes", "history_rag_blocks", "re_extraction_blocks"],
                "legacy_memory_archive_deleted": False, "legacy_memory_archive_blocked": True,
                "raw_chat_deleted": False, "backups_deleted": False,
                "warning": "不会自动删除原聊天或备份，不能抹除外部备份；恢复源后共享仍须重新授权"}


def confirm_forget(operation_id, token, *, user_confirmed=False, db=None):
    if user_confirmed is not True:
        raise MemoryError("彻底忘记需要用户单独明确确认")
    with _connection(db, write=True) as conn:
        operation = conn.execute("SELECT * FROM memory_v2_forgets WHERE id=?", (operation_id,)).fetchone()
        if not operation:
            raise NotFound("忘记预览不存在")
        if operation["token"] != token:
            raise Conflict("忘记确认token不匹配")
        if operation["status"] == "complete":
            return json.loads(operation["result"])
        plan = json.loads(operation["document"])
        if _forget_plan(conn, plan["root"]) != plan:
            raise Conflict("预览后记忆/派生关系已变化，请重新预览")
        stamp = now()
        for kind, values in (("entity", [r["id"] for r in plan["entities"]]), ("source", plan["sources"]),
                             ("source_ref", plan["source_refs"]), ("message", plan["messages"]), ("content", plan["contents"])):
            for value in values:
                conn.execute("INSERT OR IGNORE INTO memory_v2_blocks VALUES(?,?,?,?)", (kind, str(value), operation_id, stamp))
        for relation in plan["relations"]:
            conn.execute("DELETE FROM memory_v2_relations WHERE id=?", (relation["id"],))
        for item in plan["entities"]:
            eid = item["id"]
            conn.execute("DELETE FROM memory_v2_versions WHERE entity_id=?", (eid,))
            conn.execute("DELETE FROM memory_v2_relations WHERE from_id=? OR to_id=?", (eid, eid))
            conn.execute("DELETE FROM memory_v2_events WHERE entity_id=?", (eid,))
            conn.execute("UPDATE memory_v2_entities SET key=?,category='forgotten',status='forgotten',revision=revision+1,"
                         "updated_at=?,reinforced_at=NULL,reinforcement_count=0 WHERE id=?", ("forgotten:" + fingerprint(eid), stamp, eid))
            conn.execute("UPDATE memory_v2_migrations SET document='{}',status='forgotten',error='',revision=revision+1 WHERE entity_id=?", (eid,))
            _revoke(conn, eid)
        # Purge only sources no longer referenced by an unrelated entity/relation.
        for sid in plan["sources"]:
            if not conn.execute("SELECT 1 FROM memory_v2_versions WHERE source_id=?", (sid,)).fetchone() and not conn.execute(
                    "SELECT 1 FROM memory_v2_relations WHERE source_id=?", (sid,)).fetchone():
                conn.execute("DELETE FROM memory_v2_sources WHERE id=?", (sid,))
        ids = {r["id"] for r in plan["entities"]}
        cache_purged = 0
        if _table(conn, "rag_vectors") and {"source_id", "document"} <= _columns(conn, "rag_vectors"):
            for cache in conn.execute("SELECT rowid,source_id,document FROM rag_vectors").fetchall():
                try:
                    doc = json.loads(cache[2])
                except (ValueError, TypeError):
                    doc = {}
                if cache[1] in ids or _blocked(conn, {**doc, "source_id": cache[1]}):
                    conn.execute("DELETE FROM rag_vectors WHERE rowid=?", (cache[0],))
                    cache_purged += 1
        result = {"operation_id": operation_id, "status": "complete", "forgotten_entity_ids": sorted(ids),
                  "blocked_message_ids": plan["messages"], "legacy_vector_chunks_purged": cache_purged,
                  "legacy_memory_archive_deleted": False, "legacy_memory_archive_blocked": True,
                  "raw_chat_deleted": False, "backups_deleted": False,
                  "external_backups_erased": False, "sharing_requires_new_authorization": True,
                  "warning": "仅清除本主库V2派生记忆及索引；旧记忆迁移归档、原聊天和备份保留但来源被阻断，无法抹除外部备份。禁用旧库fallback；历史/提炼入口必须调用filter_history/candidate_filter。"}
        conn.execute("UPDATE memory_v2_forgets SET status='complete',result=? WHERE id=?", (dump(result), operation_id))
        _event(conn, None, "forget_complete", {"operation_id": operation_id, "entity_count": len(ids)})
        return result


def _legacy_category(value):
    return {"communication_preference": "habit", "writing_preference": "habit"}.get(value, value or "legacy")


def _normalize_legacy(record):
    """A source-table locator is not a claimed user message or confirmation."""
    if record.get("status", "active") not in ("active", "deleted", "withdrawn"):
        raise MemoryError("未知旧状态，需异常台明确处理")
    scope = record.get("scope", record.get("mode", "companion"))
    sid = record.get("session_id")
    if sid is None and scope == "work":
        sid = record.get("owner_id")
    category = _legacy_category(record.get("category"))
    layer = record.get("layer", 2 if record.get("kind") == "event" else 3)
    if scope == "work" and layer >= 3 and category not in WORK_LONG_TERM:
        layer = 2  # Preserve content without promoting arbitrary legacy work facts to long-term.
    if layer == 4:
        raise MemoryError("旧数据不能自动成为核心，请人工确认")
    raw_source = record.get("source") or {}
    if isinstance(raw_source, str):
        try:
            raw_source = json.loads(raw_source)
        except ValueError:
            raw_source = {"origin": raw_source}
    if not isinstance(raw_source, dict):
        raise MemoryError("旧来源格式异常")
    if raw_source.get("previous_version_id") is not None:
        raw_source["source_refs"] = sorted(set(raw_source.get("source_refs", ())) |
            {"legacy:fact_versions:" + str(raw_source["previous_version_id"])})
    raw_source["source_refs"] = sorted(set(raw_source.get("source_refs", ())) | {
        str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["lingxi-memory", scope, owner(scope, sid), record.get("key")], ensure_ascii=False)))})
    source = {**raw_source, "kind": "legacy", "ref": record["source_ref"],
              "legacy_ref": record["source_ref"], "origin": raw_source.get("origin", "旧记录，来源未知")}
    # Preserve real speaker provenance without inventing character or timestamps.
    if record.get("source_memory_id"):
        source["source_refs"] = sorted(set(source.get("source_refs", ())) | {str(record["source_memory_id"])})
    return {"scope": scope, "session_id": sid, "key": record.get("key"),
            "value": record.get("value", record.get("content")), "layer": layer,
            "category": category, "importance": record.get("importance", .5),
            "pinned": record.get("pinned", False) if type(record.get("pinned", False)) is bool else
                bool(record["pinned"]) if type(record.get("pinned")) is int and record["pinned"] in (0, 1) else record.get("pinned"),
            "occurred_at": record.get("occurred_at"),
            "confirmed_at": record.get("confirmed_at"), "source": source}


def _sync_one(conn, record, batch_id, force=False):
    if not isinstance(record, dict) or not isinstance(record.get("source_ref"), str) or not record["source_ref"]:
        raise MemoryError("迁移必须提供稳定真实source_ref")
    ref = record["source_ref"]
    digest = fingerprint(record)
    old = conn.execute("SELECT * FROM memory_v2_migrations WHERE source_ref=?", (ref,)).fetchone()
    authority = conn.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
    if authority and authority[0] == 'v2':
        if old and old["fingerprint"] == digest and not force:
            return {"source_ref": ref, "status": old["status"], "entity_id": old["entity_id"], "idempotent": True}
        raise Conflict("V2已激活；迁移写入/强制重放被拒绝，请直接编辑V2")
    if old and old["fingerprint"] == digest and not force:
        return {"source_ref": ref, "status": old["status"], "entity_id": old["entity_id"], "idempotent": True}
    if old and old["status"] == "forgotten":
        return {"source_ref": ref, "status": "forgotten", "entity_id": old["entity_id"], "idempotent": True}
    if old and not force:
        conn.execute("UPDATE memory_v2_migrations SET fingerprint=?,document=?,status='anomaly',"
            "error='同一旧来源已改变，需异常台显式retry/confirm',attempts=attempts+1,revision=revision+1,updated_at=? WHERE source_ref=?",
            (digest, dump(record), now(), ref))
        return {"source_ref": ref, "status": "anomaly", "entity_id": old["entity_id"],
                "error": "同一旧来源已改变，需异常台显式retry/confirm", "idempotent": False}
    raw = next((item for item in _read_legacy(conn, 2147483647, include_migrated=True)
                if item["source_ref"] == ref), None)
    if raw is not None:
        conn.execute("INSERT OR IGNORE INTO memory_v2_legacy_baselines VALUES(?,?,?)", (ref, fingerprint(raw), "{}"))
    stamp = now()
    conn.execute("""INSERT INTO memory_v2_migrations VALUES(?,?,?,?,?,NULL,'',1,1,?)
        ON CONFLICT(source_ref) DO UPDATE SET batch_id=excluded.batch_id,fingerprint=excluded.fingerprint,
        status='pending',document=excluded.document,attempts=attempts+1,revision=revision+1,updated_at=excluded.updated_at""",
        (ref, batch_id, digest, "pending", dump(record), stamp))
    try:
        # Failed items roll back their writes, but the outer transaction keeps the anomaly ledger.
        with _connection(conn, write=True):
            if _blocked(conn, {**record, "source": {"ref": ref}}):
                conn.execute("UPDATE memory_v2_migrations SET status='blocked',error='彻底忘记来源阻断' WHERE source_ref=?", (ref,))
                return {"source_ref": ref, "status": "blocked", "entity_id": None, "idempotent": False}
            body = _normalize_legacy(record)
            linked_version = body["source"].get("source_memory_version_id")
            legacy_parent = None
            if linked_version is not None:
                parent_ref = "legacy:fact_versions:" + str(linked_version)
                mapped = conn.execute("SELECT entity_id,status FROM memory_v2_migrations WHERE source_ref=?", (parent_ref,)).fetchone()
                if not mapped or mapped["status"] != "complete" or not mapped["entity_id"]:
                    raise Conflict("旧共享来源尚未迁移或来源异常，请先处理来源再retry")
                legacy_parent = _row(conn, mapped["entity_id"])
                body["source"]["source_refs"] = sorted(set(body["source"].get("source_refs", ())) | {parent_ref})
            target = conn.execute("SELECT * FROM memory_v2_entities WHERE scope=? AND owner_id=? AND key=?", (
                body["scope"], owner(body["scope"], body.get("session_id")), body["key"])).fetchone()
            if record.get("status") == "withdrawn":
                # A rollback-withdrawn version is historical evidence only. It
                # must neither replace an effective prior value nor be returned
                # by historical RAG as an asserted memory.
                sid, normalized = _record_source(conn, body["source"])
                data = {**body, "owner_id": owner(body["scope"], body.get("session_id")),
                        "source": normalized, "legacy_status": "withdrawn"}
                data.pop("session_id", None)
                data = _validate(data)
                if target:
                    row = dict(target)
                    revision = row["revision"] + 1
                    conn.execute("INSERT INTO memory_v2_versions VALUES(?,?,?,?,?,?)", (
                        row["id"], revision, dump(data), sid, now(), row["current_version"]))
                    conn.execute("UPDATE memory_v2_entities SET revision=?,updated_at=? WHERE id=?", (revision, now(), row["id"]))
                    result = {"id": row["id"]}
                else:
                    result = create(body, conn)
                    conn.execute("UPDATE memory_v2_versions SET document=? WHERE entity_id=? AND revision=1", (dump(data), result["id"]))
                    conn.execute("UPDATE memory_v2_entities SET status='withdrawn' WHERE id=?", (result["id"],))
                conn.execute("UPDATE memory_v2_migrations SET status='complete',entity_id=?,error='' WHERE source_ref=?", (result["id"], ref))
                return {"source_ref": ref, "status": "complete", "entity_id": result["id"], "idempotent": False}
            if target:
                row = dict(target)
                if row["status"] == "forgotten":
                    raise Conflict("目标已彻底忘记")
                if row["status"] in ("deleted", "withdrawn"):
                    conn.execute("UPDATE memory_v2_entities SET status='active',revision=revision+1,updated_at=? WHERE id=?", (now(), row["id"]))
                    _revoke(conn, row["id"])
                    row = _row(conn, row["id"])
                patch = {k: body[k] for k in ("key", "value", "layer", "category", "importance", "pinned", "occurred_at", "confirmed_at", "source")}
                result = update(row["id"], {**patch, "expected_revision": row["revision"]}, conn)
            else:
                result = create(body, conn)
            if legacy_parent is not None:
                dependency_kind = "share" if body["scope"] != legacy_parent["scope"] else "derived"
                conn.execute("INSERT OR IGNORE INTO memory_v2_dependencies VALUES(?,?,?)", (legacy_parent["id"], result["id"], dependency_kind))
                if dependency_kind == "share":
                    conn.execute("UPDATE memory_v2_grants SET status='revoked' WHERE target_id=?", (result["id"],))
                    valid_grant = (_effective(conn, legacy_parent) and
                        _document(conn, legacy_parent)["value"] == body["value"])
                    conn.execute("INSERT INTO memory_v2_grants VALUES(?,?,?,?,?,?)", (_id("grant-"),
                        legacy_parent["id"], result["id"], legacy_parent["revision"], "active" if valid_grant else "revoked", now()))
            if record.get("status", "active") == "deleted":
                result = delete(result["id"], result["revision"], conn)
            conn.execute("UPDATE memory_v2_migrations SET status='complete',entity_id=?,error='' WHERE source_ref=?", (result["id"], ref))
            return {"source_ref": ref, "status": "complete", "entity_id": result["id"], "idempotent": False}
    except (MemoryError, sqlite3.IntegrityError) as exc:
        conn.execute("UPDATE memory_v2_migrations SET status='anomaly',error=? WHERE source_ref=?", (str(exc), ref))
        return {"source_ref": ref, "status": "anomaly", "entity_id": None, "error": str(exc), "idempotent": False}


def sync(records, *, batch_id="stage7-explicit", db=None):
    """Explicit one-way legacy→V2 sync; NEVER writes facts/work_facts/stage7.

    Use for bounded migration/cutover only. Once authority is V2, write V2 only;
    keeping permanent dual writes is deliberately not implemented.
    """
    records = [records] if isinstance(records, dict) else list(records)
    if not 1 <= len(records) <= MAX_PAGE:
        raise MemoryError("单批迁移1～200条")
    with _connection(db, write=True) as conn:
        if _v2_authority(conn):
            for record in records:
                if not isinstance(record, dict) or not record.get("source_ref"):
                    raise Conflict("V2已激活；迁移写入已关闭")
                old = conn.execute("SELECT fingerprint FROM memory_v2_migrations WHERE source_ref=?", (record["source_ref"],)).fetchone()
                if not old or old[0] != fingerprint(record):
                    raise Conflict("V2已激活；仅允许相同来源的幂等查询")
        result = []
        for index, record in enumerate(records):
            if isinstance(record, dict) and isinstance(record.get("source_ref"), str) and record["source_ref"]:
                result.append(_sync_one(conn, record, batch_id))
                continue
            # Ledger an envelope error without inventing provenance for an entity.
            ref = "invalid-envelope:" + fingerprint([batch_id, index, record])
            conn.execute("INSERT OR IGNORE INTO memory_v2_migrations VALUES(?,?,?,?,?,NULL,?,1,1,?)", (
                ref, batch_id, fingerprint(record), "anomaly", dump(record), "缺少稳定真实source_ref；未创建实体或来源", now()))
            result.append({"source_ref": ref, "status": "anomaly", "entity_id": None,
                           "error": "缺少稳定真实source_ref；未创建实体或来源", "idempotent": False})
        return {"batch_id": batch_id, "items": result, "total": len(result), "writes_legacy": False}


def _read_legacy(conn, limit, include_migrated=False):
    result = []
    if _table(conn, "fact_versions"):
        rows = conn.execute("""SELECT f.* FROM fact_versions f WHERE (? OR NOT EXISTS(
            SELECT 1 FROM memory_v2_migrations m WHERE m.source_ref='legacy:fact_versions:' || f.id))
            ORDER BY f.id LIMIT ?""", (int(include_migrated), limit))
        for raw in rows:
            row = dict(raw)
            if row.get("status") == "deleted":
                previous = conn.execute("SELECT source FROM fact_versions WHERE scope=? AND owner_id=? AND key=? "
                    "AND id<? AND status='active' ORDER BY id DESC LIMIT 1", (
                        row["scope"], row["owner_id"], row["key"], row["id"])).fetchone()
                if previous:
                    try:
                        original = json.loads(previous[0])
                        deletion = json.loads(row["source"])
                        row["source"] = {**original, **deletion, "origin": deletion.get("origin", "用户删除；保留真实先前来源")}
                    except (ValueError, TypeError):
                        pass
            row.update(source_ref="legacy:fact_versions:" + str(row["id"]), kind="fact")
            result.append(row)
    if len(result) < limit and _table(conn, "events"):
        event_rows = conn.execute("""SELECT e.* FROM events e WHERE (? OR NOT EXISTS(
            SELECT 1 FROM memory_v2_migrations m WHERE m.source_ref='legacy:events:' || e.id))
            ORDER BY e.id""", (int(include_migrated),))
        for raw in event_rows:
            row = dict(raw)
            if _table(conn, "memory7_event_versions") and conn.execute(
                    "SELECT 1 FROM memory7_event_versions WHERE event_id=?", (row["id"],)).fetchone():
                continue
            # A preserved save date is not a fabricated event occurrence date.
            row.update(source_ref="legacy:events:" + str(row["id"]), key="旧经历-" + str(row["id"]),
                       value=row.get("content"), scope=row.get("mode", "companion"), kind="event",
                       confirmed_at=None, source={"origin": "旧事件，来源未知", "saved_at": row.get("at")})
            row["source"]["source_refs"] = ["event-" + str(row["id"])]
            result.append(row)
            if len(result) == limit:
                break
    if len(result) < limit and _table(conn, "memory7_event_versions"):
        for raw in conn.execute("""SELECT v.* FROM memory7_event_versions v WHERE (? OR NOT EXISTS(
            SELECT 1 FROM memory_v2_migrations m WHERE m.source_ref=
                'legacy:memory7_event_versions:' || v.event_id || ':' || v.revision))
            ORDER BY v.event_id,v.revision LIMIT ?""", (int(include_migrated), limit - len(result))):
            row = dict(raw)
            try:
                document = json.loads(row["document"])
            except (ValueError, TypeError):
                document = {"value": None}
            event = conn.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone() if _table(conn, "events") else None
            mode = dict(event).get("mode", "companion") if event else document.get("scope", "companion")
            source = document.get("source") or {"origin": "旧事件版本，来源未知"}
            if isinstance(source, dict):
                source = {**source, "source_refs": sorted(set(source.get("source_refs", ())) |
                    {"event-" + str(row["event_id"]), "legacy:events:" + str(row["event_id"])})}
            result.append({**document, "source_ref": "legacy:memory7_event_versions:" + str(row["event_id"]) + ":" + str(row["revision"]),
                "scope": mode, "key": "旧经历-" + str(row["event_id"]), "kind": "event", "source": source})
    for table in ("facts", "work_facts"):
        if len(result) >= limit or not _table(conn, table):
            continue
        for raw in conn.execute("SELECT * FROM " + table + " ORDER BY key"):
            row = dict(raw)
            scope = "work" if table == "work_facts" else row.get("mode", "companion")
            sid = row.get("session_id")
            oid = owner(scope, sid)
            if _table(conn, "fact_versions") and conn.execute(
                    "SELECT 1 FROM fact_versions WHERE scope=? AND owner_id=? AND key=?", (scope, oid, row["key"])).fetchone():
                continue
            ref = "legacy:" + table + ":" + fingerprint([scope, oid, row["key"]])
            if not include_migrated and conn.execute("SELECT 1 FROM memory_v2_migrations WHERE source_ref=?", (ref,)).fetchone():
                continue
            row.update(source_ref=ref, scope=scope, session_id=sid, kind="fact",
                       source={"origin": "旧记录，来源未知", "saved_at": row.get("updated_at")})
            result.append(row)
            if len(result) == limit:
                break
    return result


def migrate_legacy(*, batch_id="legacy-v2", limit=100, db=None):
    """Explicit bounded, restart-safe same-database migration. Chat is untouched."""
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise MemoryError("迁移批大小1～200")
    with _connection(db, write=True) as conn:
        if _v2_authority(conn):
            raise Conflict("V2已激活；旧库迁移已关闭")
        records = _read_legacy(conn, limit)
        items = [_sync_one(conn, row, batch_id) for row in records]
        return {"batch_id": batch_id, "items": items, "processed": len(items),
                "has_more": bool(_read_legacy(conn, 1)), "chat_modified": False, "writes_legacy": False}


def _v2_authority(conn):
    row = conn.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
    return bool(row and row[0] == "v2")


def _legacy_changes(conn):
    """Read-only final recheck of real legacy inputs, not corrected migration bodies."""
    current = {item["source_ref"]: item for item in _read_legacy(conn, 2147483647, include_migrated=True)}
    changed = {}
    for row in conn.execute("SELECT * FROM memory_v2_migrations WHERE status='complete'"):
        ref = row["source_ref"]
        if not ref.startswith(("legacy:fact_versions:", "legacy:events:", "legacy:memory7_event_versions:", "legacy:facts:", "legacy:work_facts:")):
            continue
        baseline = conn.execute("SELECT fingerprint FROM memory_v2_legacy_baselines WHERE source_ref=?", (ref,)).fetchone()
        expected = baseline[0] if baseline else row["fingerprint"]
        raw = current.get(ref)
        if raw is None or fingerprint(raw) != expected:
            changed[ref] = raw
    return changed


def migration_status(*, status=None, offset=0, limit=100, db=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise MemoryError("无效分页")
    with _connection(db) as conn:
        changed = {} if _v2_authority(conn) else _legacy_changes(conn)
        records = []
        counts = {}
        for raw in conn.execute("SELECT * FROM memory_v2_migrations ORDER BY source_ref"):
            row = dict(raw)
            if row["source_ref"] in changed:
                row.update(status="anomaly", error="真实旧来源已变化或删除；请重试当前来源或明确skip", source_changed=True,
                           current_source=None if changed[row["source_ref"]] is None else changed[row["source_ref"]])
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if status is None or row["status"] == status:
                records.append(row)
        pending_scan = _read_legacy(conn, 1)
        return {"items": records[offset:offset + limit], "total": len(records), "counts": counts,
                "has_more": bool(pending_scan), "remaining": None if pending_scan else 0,
                "remaining_exact": not bool(pending_scan), "offset": offset, "limit": limit,
                "authority": conn.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()[0]}


def resolve_anomaly(source_ref, expected_revision, action, *, user_confirmed=False, corrected=None, db=None):
    if action not in ("confirm", "skip", "retry"):
        raise MemoryError("异常操作仅允许confirm/skip/retry")
    if action == "confirm" and user_confirmed is not True:
        raise MemoryError("异常确认需要用户明确确认")
    with _connection(db, write=True) as conn:
        if _v2_authority(conn):
            raise Conflict("V2已激活；迁移重试/确认/跳过均已关闭")
        row = conn.execute("SELECT * FROM memory_v2_migrations WHERE source_ref=?", (source_ref,)).fetchone()
        if not row:
            raise NotFound("迁移异常不存在")
        if type(expected_revision) is not int or row["revision"] != expected_revision:
            raise Conflict("异常记录版本已变化")
        changed = _legacy_changes(conn)
        source_changed = source_ref in changed
        if row["status"] not in ("anomaly", "skipped", "blocked") and not source_changed:
            raise Conflict("仅可处理未解决异常")
        if action == "skip":
            conn.execute("UPDATE memory_v2_migrations SET status='skipped',revision=revision+1,updated_at=? WHERE source_ref=?", (now(), source_ref))
            # A skipped stale current value cannot remain an effective V2 assertion.
            if source_changed and row["entity_id"]:
                entity = _row(conn, row["entity_id"])
                doc = _document(conn, entity)
                if doc and doc.get("source", {}).get("legacy_ref") == source_ref:
                    conn.execute("UPDATE memory_v2_entities SET status='withdrawn',revision=revision+1,updated_at=? WHERE id=?", (now(), entity["id"]))
                    _revoke(conn, entity["id"])
            return {"source_ref": source_ref, "status": "skipped", "revision": expected_revision + 1}
        record = changed[source_ref] if source_changed else json.loads(row["document"])
        if record is None:
            raise Conflict("真实来源已删除，只能明确skip；不能用修正内容制造来源")
        if source_ref.startswith("invalid-envelope:"):
            raise MemoryError("缺失来源的批次项只能skip；请通过新批次重新提交可验证真实source_ref")
        original = dict(record)
        if corrected is not None:
            if not isinstance(corrected, dict) or "source_ref" in corrected and corrected["source_ref"] != source_ref:
                raise MemoryError("不允许伪造或更换旧source_ref")
            record = {**record, **corrected, "source_ref": source_ref}
        result = _sync_one(conn, record, row["batch_id"], force=True)
        if result["status"] == "complete":
            # Baseline follows the real raw source, never the user's corrected body.
            actual = next((item for item in _read_legacy(conn, 2147483647, include_migrated=True) if item["source_ref"] == source_ref), None)
            if actual is not None:
                conn.execute("INSERT OR REPLACE INTO memory_v2_legacy_baselines VALUES(?,?,?)", (source_ref, fingerprint(actual), "{}"))
            # Retracting a formerly active legacy version must retract its old V2 value.
            if original.get("status") == "withdrawn" and row["entity_id"]:
                entity = _row(conn, row["entity_id"])
                doc = _document(conn, entity)
                if doc and doc.get("source", {}).get("legacy_ref") == source_ref:
                    conn.execute("UPDATE memory_v2_entities SET status='withdrawn',revision=revision+1,updated_at=? WHERE id=?", (now(), entity["id"]))
                    _revoke(conn, entity["id"])
        _event(conn, result.get("entity_id"), "migration_" + action, {"source_ref": source_ref, "status": result["status"]})
        return result


def activate(*, user_confirmed=False, db=None):
    """Atomic authority cutover, including a final recheck of already migrated sources."""
    if user_confirmed is not True:
        raise MemoryError("切换主记忆库需要用户/集成流程明确确认")
    with _connection(db, write=True) as conn:
        if _v2_authority(conn):
            return {"authority": "v2", "legacy_writes_allowed": False, "single_database": True, "idempotent": True}
        if _legacy_changes(conn):
            raise Conflict("已迁移的真实旧来源变化或删除；请打开异常台retry/confirm或明确skip")
        if _read_legacy(conn, 1) or conn.execute("SELECT 1 FROM memory_v2_migrations WHERE status IN ('pending','anomaly','blocked')").fetchone():
            raise Conflict("请先完成剩余迁移并处理异常，或明确skip")
        conn.execute("UPDATE memory_v2_meta SET value='v2' WHERE key='authority'")
        return {"authority": "v2", "legacy_writes_allowed": False, "single_database": True}
