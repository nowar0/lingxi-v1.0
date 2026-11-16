"""阶段7接口；沿用事实ID、事实版本与当前值，不维护第二份事实库。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException

from app import migrations, sessions, stage5
from app.db import connect
from app.memory.keys import normalize_key

router = APIRouter(prefix="/v4")
try:
    TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows未安装IANA时区库时，当前自然日使用上海现行UTC+8，不阻止启动。
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
SHARED_CATEGORIES = {"communication_preference", "writing_preference"}
CATEGORIES = SHARED_CATEGORIES | {"fact", "preference", "event", "task", "legacy"}
DEFAULTS = {
    "auto_memory_enabled": True,
    "summarize_every_turns": 20,
    "keep_recent_turns": 10,
    "autoload_days": 7,
    "shared_auto_categories": [],
    "semantic_enabled": True,
    "semantic_max_chunks": 8,
    "context_tokens": 32768,
    "reply_reserve_tokens": 4096,
    "tool_reserve_tokens": 4096,
    "memory_budget_tokens": 6000,
    "rag_enabled": True,
    "rag_top_k": 5,
    "rag_min_score": 0.4,
}


def now():
    return datetime.now(TZ).isoformat(timespec="microseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ready(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='memory7_settings'").fetchone())


def init_table():
    marker = stage5.DATA_DIR / "backups/stage7-schema-backup.json"
    if not marker.exists():
        backup = migrations.backup(stage5.DATA_DIR)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(dump({"backup": backup}), encoding="utf-8")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        columns = {r["name"] for r in db.execute("PRAGMA table_info(fact_versions)")}
        for name, spec in [
            ("category", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("occurred_at", "TEXT"),
            ("confirmed_at", "TEXT"),
            ("pinned", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if name not in columns:
                db.execute(f"ALTER TABLE fact_versions ADD COLUMN {name} {spec}")
        if "confirmed_at" not in columns:
            # 兼容旧记录保存日期；不填发生日期，来源仍保留旧版本的真实说明。
            db.execute("UPDATE fact_versions SET confirmed_at=at")
        for sql in [
            "CREATE TABLE IF NOT EXISTS memory7_settings(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL, revision INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory7_attempts(batch_id TEXT PRIMARY KEY REFERENCES stage6_batches(id), attempt_id TEXT NOT NULL, source TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory7_candidates(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, attempt_id TEXT NOT NULL, key TEXT NOT NULL, document TEXT NOT NULL, expected_revision INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, UNIQUE(batch_id,attempt_id,key))",
            "CREATE TABLE IF NOT EXISTS memory7_event_versions(event_id INTEGER NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL, PRIMARY KEY(event_id,revision))",
            "CREATE TABLE IF NOT EXISTS memory7_summaries(id TEXT PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), boundary INTEGER NOT NULL, previous_id TEXT, source TEXT NOT NULL, document TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory7_jobs(id TEXT PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), source TEXT NOT NULL, document TEXT, status TEXT NOT NULL, error TEXT NOT NULL, at TEXT NOT NULL)",
        ]:
            db.execute(sql)
        db.execute("INSERT OR IGNORE INTO memory7_settings VALUES(1,?,1)", (dump(DEFAULTS),))
        db.execute(
            "UPDATE memory7_jobs SET status='interrupted',error='服务重启中断；不会自动再次调用模型' WHERE status IN ('pending','running')"
        )
        db.execute(
            "UPDATE memory7_candidates SET status='discarded',reason='服务重启或来源批次中断' WHERE status='pending' AND batch_id NOT IN (SELECT id FROM stage6_batches WHERE status='generating')"
        )
        # 仅补实际存在的旧当前值；来源未知，不将升级时间写成发生时间。
        rows = [
            (r["mode"], None, r["key"], r["value"], r["updated_at"])
            for r in db.execute("SELECT * FROM facts WHERE mode IN ('companion','shared')")
        ]
        rows += [
            ("work", r["session_id"], r["key"], r["value"], r["updated_at"])
            for r in db.execute("SELECT * FROM work_facts")
        ]
        for scope, sid, key, value, at in rows:
            # The legacy table permits NULL; fact_versions does not. Preserve the
            # original row for V2's anomaly ledger instead of blocking startup or
            # manufacturing a replacement value during compatibility bootstrap.
            if value is None:
                continue
            oid = owner(scope, sid)
            if not db.execute(
                "SELECT 1 FROM fact_versions WHERE scope=? AND owner_id=? AND key=?",
                (scope, oid, key),
            ).fetchone():
                db.execute(
                    "INSERT INTO fact_versions(scope,owner_id,key,value,source,at,confirmed_at) VALUES(?,?,?,?,?,?,?)",
                    (scope, oid, key, value, dump({"origin": "旧记录，来源未知"}), at or now(), at),
                )
        for event in db.execute("SELECT * FROM events WHERE mode IN ('shared','companion')"):
            doc = {
                "value": event["content"],
                "category": "legacy",
                "pinned": False,
                "occurred_at": None,
                "confirmed_at": event["at"],
                "source": {"origin": "旧事件，来源未知；日期沿用旧保存时间"},
                "status": "active",
            }
            db.execute(
                "INSERT OR IGNORE INTO memory7_event_versions VALUES(?,1,?)",
                (event["id"], dump(doc)),
            )


def owner(scope, sid=None):
    if scope == "shared":
        return "global"
    if scope == "companion":
        return "companion-global"
    if scope != "work" or sid is None:
        raise HTTPException(422, "工作记忆需要工作会话ID")
    return str(sid)


def check_scope(scope, sid=None):
    owner(scope, sid)
    if scope == "work":
        row = sessions.get_session(sid)
        if not row or row["mode"] != "work":
            raise HTTPException(409, "需要有效工作会话")


def settings(db=None):
    if db is None:
        with connect() as conn:
            return settings(conn)
    if not ready(db):
        return {**DEFAULTS, "auto_memory_enabled": False, "revision": 0}
    row = db.execute("SELECT * FROM memory7_settings WHERE id=1").fetchone()
    return {**DEFAULTS, **json.loads(row["document"]), "revision": row["revision"]}


@router.get("/memory/settings")
def settings_api():
    return settings()


@router.put("/memory/settings")
def save_settings(body: dict):
    if set(body) - set(DEFAULTS) - {"expected_revision"}:
        raise HTTPException(422, "包含未知设置")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = settings(db)
        if (
            type(body.get("expected_revision")) is not int
            or body["expected_revision"] != old["revision"]
        ):
            raise HTTPException(409, "设置已变化；请保留本地输入并刷新比较")
        data = {k: body.get(k, old[k]) for k in DEFAULTS}
        for k in ("auto_memory_enabled", "semantic_enabled", "rag_enabled"):
            if type(data[k]) is not bool:
                raise HTTPException(422, k + " 必须是布尔值")
        bounds = {
            "summarize_every_turns": (1, 1000),
            "keep_recent_turns": (1, 1000),
            "autoload_days": (1, 36500),
            "semantic_max_chunks": (1, 32),
            "context_tokens": (2048, 2000000),
            "reply_reserve_tokens": (256, 100000),
            "tool_reserve_tokens": (0, 100000),
            "memory_budget_tokens": (256, 100000),
            "rag_top_k": (1, 20),
        }
        for k, (low, high) in bounds.items():
            if type(data[k]) is not int or not low <= data[k] <= high:
                raise HTTPException(422, f"{k} 必须是{low}～{high}整数")
        if type(data["rag_min_score"]) not in (int, float) or not 0 <= data["rag_min_score"] <= 1:
            raise HTTPException(422, "rag_min_score 必须是0～1的数值")
        if (
            data["reply_reserve_tokens"]
            + data["tool_reserve_tokens"]
            + data["memory_budget_tokens"]
            >= data["context_tokens"]
        ):
            raise HTTPException(422, "预留回复、工具和记忆容量之和必须小于上下文容量")
        cats = data["shared_auto_categories"]
        if not isinstance(cats, list) or any(
            not isinstance(c, str) or c not in SHARED_CATEGORIES for c in cats
        ):
            raise HTTPException(422, "自动共享仅允许交流偏好与写作偏好类别")
        from app.memory import v2
        if cats and v2.ready(db):
            authority = db.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
            if authority and authority[0] == 'v2':
                raise HTTPException(409, 'V2 共享需要逐条明确确认；请关闭旧版自动共享类别并在四层记忆中授权')
        data["shared_auto_categories"] = sorted(set(cats))
        db.execute(
            "UPDATE memory7_settings SET document=?,revision=revision+1 WHERE id=1", (dump(data),)
        )
        return {**data, "revision": old["revision"] + 1}


def input_snapshot(db, batch_id):
    rows = [
        dict(r)
        for r in db.execute(
            "SELECT m.id,m.content,m.session_id,m.delivery_status FROM stage6_inputs i JOIN messages m ON m.id=i.message_id WHERE i.batch_id=? ORDER BY m.id",
            (batch_id,),
        )
    ]
    return {
        "message_ids": [r["id"] for r in rows],
        "fingerprint": hashlib.sha256(
            dump([(r["id"], r["content"], r["session_id"]) for r in rows]).encode()
        ).hexdigest(),
    }


def source_valid(db, source, seen=None):
    if source.get("batch_id"):
        batch = db.execute(
            "SELECT * FROM stage6_batches WHERE id=?", (source["batch_id"],)
        ).fetchone()
        if (
            not batch
            or batch["status"] != "complete"
            or batch["session_id"] != source.get("session_id")
        ):
            return False
        invalid = db.execute(
            "SELECT COUNT(*) FROM messages WHERE batch_id=? AND (delivery_status!='complete' OR archived=1)",
            (source["batch_id"],),
        ).fetchone()[0]
        if invalid:
            return False
        snap = input_snapshot(db, source["batch_id"])
        if snap["fingerprint"] != source.get("fingerprint") or snap["message_ids"] != source.get(
            "message_ids"
        ):
            return False
    from app.timeline import source_archived
    if source_archived(db, source):
        return False
    linked = source.get("source_memory_version_id")
    if linked is not None:
        seen = set(seen or ())
        if linked in seen:
            return False
        seen.add(linked)
        r = db.execute("SELECT * FROM fact_versions WHERE id=?", (linked,)).fetchone()
        if not r or r["status"] != "active":
            return False
        latest = db.execute(
            "SELECT MAX(id) FROM fact_versions WHERE scope=? AND owner_id=? AND key=? AND status!='withdrawn'",
            (r["scope"], r["owner_id"], r["key"]),
        ).fetchone()[0]
        # 恢复生成的新版本沿用原来源；纯恢复的版本链允许引用原有效来源版本。
        current, aliases = latest, set()
        while current and current not in aliases:
            aliases.add(current)
            parent = db.execute('SELECT source FROM fact_versions WHERE id=?', (current,)).fetchone()
            current = json.loads(parent['source']).get('restored_from_version_id') if parent else None
        if linked not in aliases or not source_valid(db, json.loads(r["source"]), seen):
            return False
    return True


def locate_historical_source(memory_id, db):
    """只读定位历史来源；不补写当前值，也不把删除/撤回版本当作有效事实。"""
    target = next(
        (
            r for r in db.execute("SELECT scope,owner_id,key FROM fact_versions GROUP BY scope,owner_id,key")
            if stage5.memory_identity(r["scope"], r["owner_id"], r["key"]) == memory_id
        ),
        None,
    )
    if not target:
        raise HTTPException(404, "记忆历史不存在")
    versions = db.execute(
        "SELECT * FROM fact_versions WHERE scope=? AND owner_id=? AND key=? ORDER BY id",
        (target["scope"], target["owner_id"], target["key"]),
    ).fetchall()
    effective = [r for r in versions if r["status"] != "withdrawn"]
    latest = effective[-1] if effective else versions[-1]
    source_version = latest
    if latest["status"] == "deleted":
        # 删除版本记录的是删除操作；查看来源应回到被删除事实的最后有效版本。
        source_version = next((r for r in reversed(effective[:-1]) if r["status"] == "active"), latest)
    return {
        "source": json.loads(source_version["source"]),
        "status": latest["status"],
    }


@router.get("/memory/{memory_id}/source")
def memory_source(memory_id: str):
    with connect() as db:
        if memory_id.startswith("event-"):
            return {"messages": [], "note": "旧事件或手动事件请查看版本中的来源说明"}
        # 来源与消息使用同一只读快照，避免并发删除/回退时拼接不同版本。
        db.execute("BEGIN")
        current_missing = False
        try:
            item, _ = stage5.locate_fact(memory_id, db)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            current_missing = True
            item = locate_historical_source(memory_id, db)
        source = item["source"]
        # valid仅表示历史来源仍有效；事实是否删除/当前值缺失由独立标记说明。
        valid = item["status"] != "withdrawn" and source_valid(db, source)
        rows = []
        if valid:
            for mid in source.get("message_ids", []):
                row = db.execute(
                    "SELECT id,session_id,role,content,at FROM messages "
                    "WHERE id=? AND session_id=? AND archived=0 "
                    "AND delivery_status IN ('complete','retained')",
                    (mid, source.get("session_id")),
                ).fetchone()
                if row:
                    rows.append(dict(row))
        return {
            "source": source,
            "messages": rows,
            "valid": valid,
            "deleted": item["status"] == "deleted",
            "current_missing": current_missing,
        }


def enrich_version(db, scope, sid, key, ctx):
    if not ready(db):
        return
    rows = db.execute(
        "SELECT * FROM fact_versions WHERE scope=? AND owner_id=? AND key=? ORDER BY id DESC LIMIT 2",
        (scope, owner(scope, sid), key),
    ).fetchall()
    current = rows[0]
    old = rows[1] if len(rows) > 1 else None
    category = ctx.get("category", old["category"] if old else "fact")
    occurred = ctx.get("occurred_at", old["occurred_at"] if old else None)
    confirmed = ctx.get(
        "confirmed_at", old["confirmed_at"] if ctx.get("metadata_only") and old else now()
    )
    pinned = ctx.get("pinned", bool(old["pinned"]) if old else False)
    if ctx.get('metadata_only') and old and current['value'] == old['value']:
        source = json.loads(current['source'])
        source['restored_from_version_id'] = old['id']
        db.execute('UPDATE fact_versions SET source=? WHERE id=?', (dump(source), current['id']))
    db.execute(
        "UPDATE fact_versions SET category=?,occurred_at=?,confirmed_at=?,pinned=? WHERE id=?",
        (category, occurred, confirmed, int(pinned), current["id"]),
    )


def list_items(scope, sid=None, db=None):
    if db is None:
        check_scope(scope, sid)
        with connect() as conn:
            return list_items(scope, sid, conn)
    oid = owner(scope, sid)
    if scope == "work":
        rows = db.execute(
            "SELECT key,value,updated_at FROM work_facts WHERE session_id=? ORDER BY updated_at DESC",
            (sid,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT key,value,updated_at FROM facts WHERE mode=? ORDER BY updated_at DESC", (scope,)
        ).fetchall()
    result = []
    for r in rows:
        item = stage5.fact_dto({**dict(r), "mode": scope}, scope, sid, db)
        version = (
            db.execute("SELECT * FROM fact_versions WHERE id=?", (item["version_id"],)).fetchone()
            if item["version_id"]
            else None
        )
        if item["status"] != "active" or not source_valid(db, item["source"]):
            continue
        result.append(
            {
                **item,
                "kind": "fact",
                "category": version["category"] if version else "legacy",
                "pinned": bool(version["pinned"]) if version else False,
                "occurred_at": version["occurred_at"] if version else None,
                "confirmed_at": version["confirmed_at"] if version else r["updated_at"],
            }
        )
    if scope != "work":
        for r in db.execute("SELECT * FROM events WHERE mode=? ORDER BY id DESC", (scope,)):
            history = db.execute(
                "SELECT * FROM memory7_event_versions WHERE event_id=? AND json_extract(document,'$.status')!='withdrawn' ORDER BY revision DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            data = (
                json.loads(history["document"])
                if history
                else {
                    "pinned": False,
                    "category": "legacy",
                    "occurred_at": None,
                    "confirmed_at": r["at"],
                    "source": {"origin": "旧事件，来源未知；日期沿用旧保存时间"},
                    "status": "active",
                }
            )
            if data["status"] == "active" and source_valid(db, data["source"]):
                result.append(
                    {
                        **data,
                        "id": f"event-{r['id']}",
                        "scope": scope,
                        "mode": scope,
                        "owner_id": oid,
                        "key": "事件",
                        "value": r["content"],
                        "kind": "event",
                        "revision": db.execute('SELECT MAX(revision) FROM memory7_event_versions WHERE event_id=?', (r['id'],)).fetchone()[0] or 1,
                        "version_id": f"event-{r['id']}:{history['revision'] if history else 1}",
                        "updated_at": r["at"],
                    }
                )
    return result


@router.get("/memory")
def memory_api(scope: str, session_id: int | None = None, offset: int = 0, limit: int = 100):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "分页范围错误")
    items = list_items(scope, session_id)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "offset": offset,
        "limit": limit,
    }


def date_value(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "日期必须是ISO日期或空值")
    try:
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=TZ).isoformat()
            if parsed.tzinfo is None
            else parsed.astimezone(TZ).isoformat()
        )
    except ValueError as exc:
        raise HTTPException(422, "日期格式错误") from exc


def validate_fields(body):
    if (
        not isinstance(body.get("value"), str)
        or not body["value"].strip()
        or len(body["value"]) > 2000
    ):
        raise HTTPException(422, "内容限1～2000字")
    if (
        not isinstance(body.get("category", "fact"), str)
        or body.get("category", "fact") not in CATEGORIES
    ):
        raise HTTPException(422, "记忆类别错误")
    if "pinned" in body and type(body["pinned"]) is not bool:
        raise HTTPException(422, "置顶必须是布尔值")
    return {
        k: date_value(body[k]) if k == "occurred_at" else body[k]
        for k in ("category", "pinned", "occurred_at")
        if k in body
    }


def require_legacy_authority():
    from app.memory import v2
    with connect() as db:
        if v2.ready(db):
            row = db.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
            if row and row[0] == 'v2':
                raise HTTPException(409, '记忆已切换到 V2；旧记忆仅归档只读，请使用四层记忆管理 /v10/memory')


@router.post("/memory")
def add_memory(body: dict):
    require_legacy_authority()
    scope, sid = body.get("scope"), body.get("session_id")
    check_scope(scope, sid)
    meta = validate_fields(body)
    if not isinstance(body.get("key"), str) or not 1 <= len(body["key"].strip()) <= 80:
        raise HTTPException(422, "名称限1～80字")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        key = normalize_key(body["key"])
        versions = db.execute('SELECT status FROM fact_versions WHERE scope=? AND owner_id=? AND key=? ORDER BY id',
                              (scope, owner(scope, sid), key)).fetchall()
        # An explicit new item after a completed deletion is a new revision, not revision zero.
        expected = len(versions) if versions and versions[-1]['status'] == 'deleted' else 0
        stage5.set_fact(
            key,
            body["value"],
            {
                "mode": scope,
                "session_id": sid,
                "expected_revision": expected,
                "origin": "用户添加",
                **meta,
            },
            db,
        )
        return next(i for i in list_items(scope, sid, db) if i["key"] == key)


@router.put("/memory/{memory_id}")
def edit_memory(memory_id: str, body: dict):
    require_legacy_authority()
    meta = validate_fields(body)
    if type(body.get("expected_revision")) is not int:
        raise HTTPException(422, "expected_revision必须是整数")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if memory_id.startswith("event-"):
            try:
                eid = int(memory_id.removeprefix("event-"))
            except ValueError as exc:
                raise HTTPException(404, "事件不存在") from exc
            event = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            if not event:
                raise HTTPException(404, "事件不存在")
            item = next(i for i in list_items(event["mode"], db=db) if i["id"] == memory_id)
            if (
                type(body.get("expected_revision")) is not int
                or body["expected_revision"] != item["revision"]
            ):
                raise HTTPException(409, "记忆已变化；保留本地输入并刷新比较")
            db.execute(
                "INSERT OR IGNORE INTO memory7_event_versions VALUES(?,1,?)", (eid, dump(item))
            )
            doc = {
                **item,
                **meta,
                "value": body["value"],
                "confirmed_at": item["confirmed_at"] if body["value"] == item["value"] else now(),
                "source": item["source"]
                if body["value"] == item["value"]
                else {"origin": "用户修正"},
                "status": "active",
            }
            db.execute(
                "INSERT INTO memory7_event_versions VALUES(?,?,?)",
                (eid, item["revision"] + 1, dump(doc)),
            )
            db.execute("UPDATE events SET content=? WHERE id=?", (body["value"], eid))
            return {**doc, "revision": item["revision"] + 1}
        item, sid = stage5.locate_fact(memory_id, db)
        metadata_only = body["value"] == item["value"]
        source = item["source"] if metadata_only else {}
        stage5.set_fact(
            item["key"],
            body["value"],
            {
                **source,
                "mode": item["scope"],
                "session_id": source.get("session_id", sid) if metadata_only else sid,
                "expected_revision": body.get("expected_revision"),
                "metadata_only": metadata_only,
                "origin": "用户调整元数据" if metadata_only else "用户修正",
                **meta,
            },
            db,
        )
        return next(i for i in list_items(item["scope"], sid, db) if i["id"] == memory_id)


@router.delete("/memory/{memory_id}")
def delete_memory(memory_id: str, expected_revision: int):
    require_legacy_authority()
    if not memory_id.startswith("event-"):
        return stage5.delete_memory(memory_id, expected_revision)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            eid = int(memory_id.removeprefix("event-"))
        except ValueError as exc:
            raise HTTPException(404, "事件不存在") from exc
        event = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not event:
            raise HTTPException(404, "事件不存在")
        item = next(i for i in list_items(event["mode"], db=db) if i["id"] == memory_id)
        if expected_revision != item["revision"]:
            raise HTTPException(409, "记忆已变化")
        db.execute("INSERT OR IGNORE INTO memory7_event_versions VALUES(?,1,?)", (eid, dump(item)))
        db.execute(
            "INSERT INTO memory7_event_versions VALUES(?,?,?)",
            (
                eid,
                item["revision"] + 1,
                dump({**item, "status": "deleted", "source": {"origin": "用户删除"}}),
            ),
        )
        db.execute("DELETE FROM events WHERE id=?", (eid,))
    return {"ok": True, "status": "deleted"}


@router.post("/memory/{memory_id}/restore")
def restore_memory(memory_id: str, body: dict | None = None):
    require_legacy_authority()
    expected_revision = (body or {}).get("expected_revision")
    if type(expected_revision) is not int:
        raise HTTPException(422, "expected_revision必须是整数")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if memory_id.startswith("event-"):
            try:
                eid = int(memory_id.removeprefix("event-"))
            except ValueError as exc:
                raise HTTPException(404, "事件不存在") from exc
            rows = db.execute(
                "SELECT * FROM memory7_event_versions WHERE event_id=? ORDER BY revision", (eid,)
            ).fetchall()
            if not rows:
                raise HTTPException(404, "事件历史不存在")
            latest = rows[-1]
            latest_doc = json.loads(latest["document"])
            if latest_doc.get("status") != "deleted":
                raise HTTPException(409, "该记忆当前没有可恢复的删除版本")
            if expected_revision != latest["revision"]:
                raise HTTPException(409, "记忆历史已经变化，请重新加载")
            previous = next((r for r in reversed(rows[:-1]) if json.loads(r["document"]).get("status") == "active"), None)
            restored = json.loads(previous["document"]) if previous else latest_doc
            restored = {
                **restored,
                "status": "active",
                "source": {"origin": "用户恢复", "restored_from_revision": latest["revision"]},
            }
            revision = latest["revision"] + 1
            db.execute(
                "INSERT INTO memory7_event_versions VALUES(?,?,?)",
                (eid, revision, dump(restored)),
            )
            db.execute(
                "INSERT INTO events(id,content,mode,at) VALUES(?,?,?,?)",
                (eid, restored.get("value", ""), restored.get("scope", "companion"), restored.get("occurred_at") or restored.get("confirmed_at") or now()),
            )
            return {**restored, "id": memory_id, "revision": revision}

        candidates = db.execute(
            "SELECT scope,owner_id,key FROM fact_versions GROUP BY scope,owner_id,key"
        ).fetchall()
        target = next(
            (r for r in candidates if stage5.memory_identity(r["scope"], r["owner_id"], r["key"]) == memory_id),
            None,
        )
        if not target:
            raise HTTPException(404, "记忆历史不存在")
        rows = db.execute(
            "SELECT * FROM fact_versions WHERE scope=? AND owner_id=? AND key=? ORDER BY id",
            (target["scope"], target["owner_id"], target["key"]),
        ).fetchall()
        latest = rows[-1]
        if latest["status"] != "deleted":
            raise HTTPException(409, "该记忆当前没有可恢复的删除版本")
        if expected_revision != len(rows):
            raise HTTPException(409, "记忆历史已经变化，请重新加载")
        previous = next((r for r in reversed(rows[:-1]) if r["status"] == "active"), None)
        if not previous:
            raise HTTPException(409, "没有可恢复的有效记忆版本")
        scope = target["scope"]
        sid = int(target["owner_id"]) if scope == "work" else None
        stage5.set_fact(
            target["key"],
            previous["value"],
            {
                "mode": scope,
                "session_id": sid,
                "expected_revision": len(rows),
                "origin": "用户恢复",
                # 只关联删除操作，不把原active版本设为别名；共享派生仍须用户重新确认。
                "restored_from_version_id": latest["id"],
                "category": previous["category"],
                "occurred_at": previous["occurred_at"],
                "pinned": bool(previous["pinned"]),
            },
            db,
        )
        return next(i for i in list_items(scope, sid, db) if i["id"] == memory_id)


@router.get("/memory/{memory_id}/versions")
def versions(memory_id: str):
    with connect() as db:
        if memory_id.startswith("event-"):
            try:
                eid = int(memory_id.removeprefix("event-"))
            except ValueError as exc:
                raise HTTPException(404, "事件不存在") from exc
            return {
                "versions": [
                    {"revision": r["revision"], **json.loads(r["document"])}
                    for r in db.execute(
                        "SELECT * FROM memory7_event_versions WHERE event_id=? ORDER BY revision",
                        (eid,),
                    )
                ]
            }
        records = []
        for r in db.execute("SELECT * FROM fact_versions ORDER BY id"):
            if stage5.memory_identity(r["scope"], r["owner_id"], r["key"]) == memory_id:
                records.append({**dict(r), "source": json.loads(r["source"])})
        return {"versions": records}


@router.get("/memory/search")
async def search_api(scope: str, q: str, session_id: int | None = None, semantic: bool = True):
    from app.memory.search import search

    check_scope(scope, session_id)
    return await search(q, scope, session_id, semantic=semantic, include_shared=False)


@router.get("/sessions/{sid}/memory-status")
def memory_status(sid: int):
    if not sessions.get_session(sid):
        raise HTTPException(404, "会话不存在")
    with connect() as db:
        jobs = [
            dict(r)
            for r in db.execute(
                "SELECT id,status,error,at FROM memory7_jobs WHERE session_id=? ORDER BY at DESC LIMIT 10",
                (sid,),
            )
        ]
        candidates = [
            dict(r)
            for r in db.execute(
                "SELECT c.id,c.key,c.status,c.reason FROM memory7_candidates c JOIN stage6_batches b ON b.id=c.batch_id WHERE b.session_id=? ORDER BY c.rowid DESC LIMIT 20",
                (sid,),
            )
        ]
    return {"jobs": jobs, "candidates": candidates}
