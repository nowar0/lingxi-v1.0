"""候选只暂存；最终回复与有效记忆在队列finish的同一事务提交。"""

import json
from uuid import uuid4

from app import stage5
from app.db import connect
from app.memory import stage7, v2_bridge, v2
from app.memory.keys import normalize_key


def begin_attempt(bid):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = db.execute(
            "SELECT * FROM stage6_batches WHERE id=? AND status='generating'", (bid,)
        ).fetchone()
        if not batch:
            raise ValueError("批次已停止或完成")
        attempt = str(uuid4())
        use_v2 = v2_bridge.active(db)
        revisions = v2_bridge.revisions(db, batch['mode'], batch['session_id']) if use_v2 else {}
        if not use_v2:
            for scope in (batch["mode"], "shared"):
                for item in stage7.list_items(
                    scope, batch["session_id"] if scope == "work" else None, db
                ):
                    if item["kind"] == "fact":
                        revisions[f"{scope}:{item['key']}"] = item["revision"]
            for scope in (batch['mode'], 'shared'):
                for row in db.execute('SELECT key,COUNT(*) AS revision FROM fact_versions WHERE scope=? AND owner_id=? GROUP BY key',
                                      (scope, stage7.owner(scope, batch['session_id']))):
                    revisions[f"{scope}:{row['key']}"] = row['revision']
        source = {
            **stage7.input_snapshot(db, bid),
            "revisions": revisions,
            "session_id": batch["session_id"],
            "session_version": batch["session_version"],
            "batch_id": bid,
            "attempt_id": attempt,
            "memory_authority": "v2" if use_v2 else "legacy",
        }
        speaker = json.loads(batch["snapshot"]).get("speaker") or {}
        source.update(
            source_character_id=speaker.get("id"), source_character_revision=speaker.get("revision")
        )
        db.execute(
            "UPDATE memory7_candidates SET status='discarded',reason='被新执行尝试替代' WHERE batch_id=? AND status='pending'",
            (bid,),
        )
        db.execute(
            "INSERT INTO memory7_attempts VALUES(?,?,?) ON CONFLICT(batch_id) DO UPDATE SET attempt_id=excluded.attempt_id,source=excluded.source",
            (bid, attempt, stage7.dump(source)),
        )
        return {
            "schema_version": 3,
            "mode": batch["mode"],
            "session_id": batch["session_id"],
            "session_version": batch["session_version"],
            "request_id": bid,
            "batch_id": bid,
            "attempt_id": attempt,
            "source_character_id": speaker.get("id"),
        }


def set_material_policy(ctx, external_material):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT source FROM memory7_attempts WHERE batch_id=? AND attempt_id=?',
                       (ctx['batch_id'],ctx['attempt_id'])).fetchone()
        if not row:raise ValueError('执行尝试已失效')
        source=json.loads(row['source']);source['external_material']=bool(external_material)
        db.execute('UPDATE memory7_attempts SET source=? WHERE batch_id=? AND attempt_id=?',
                   (stage7.dump(source),ctx['batch_id'],ctx['attempt_id']))


def stage_candidate(ctx, key, value, category="fact", occurred_at=None):
    if ctx.get('external_material'):raise ValueError('本轮含附件资料，不自动提炼个人记忆')
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key) > 80
        or not isinstance(value, str)
        or not value.strip()
        or len(value) > 2000
    ):
        raise ValueError("记忆名称限1～80字，内容限1～2000字")
    if category not in (stage7.CATEGORIES - {"legacy"}) | {"habit", "project"}:
        raise ValueError("记忆类别错误")
    key = normalize_key(key)
    occurred_at = stage7.date_value(occurred_at)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not stage7.settings(db)["auto_memory_enabled"]:
            raise ValueError("自动记忆已关闭；未暂存")
        batch = db.execute(
            "SELECT * FROM stage6_batches WHERE id=? AND status='generating'",
            (ctx.get("batch_id"),),
        ).fetchone()
        row = db.execute(
            "SELECT * FROM memory7_attempts WHERE batch_id=? AND attempt_id=?",
            (ctx.get("batch_id"), ctx.get("attempt_id")),
        ).fetchone()
        if (
            not row
            or not batch
            or batch["session_id"] != ctx.get("session_id")
            or batch["mode"] != ctx.get("mode")
        ):
            raise ValueError("执行尝试或来源范围已失效")
        source = json.loads(row["source"])
        use_v2 = v2_bridge.active(db)
        if use_v2 != (source.get('memory_authority', 'legacy') == 'v2'):
            raise ValueError('记忆主库已切换；旧执行尝试未暂存，请重新执行')
        if category in {'habit', 'project'} and not use_v2:
            raise ValueError('habit/project需先显式切换V2；旧主库仍使用原类别')
        if source.get('external_material'):raise ValueError('本轮含附件资料，不自动提炼个人记忆')
        from app.timeline import version
        if source.get('session_version', 0) != version(db, batch['session_id']):
            raise ValueError('会话已回退，旧工具结果不得提交')
        snap = stage7.input_snapshot(db, batch["id"])
        if snap["fingerprint"] != source["fingerprint"] or not snap["message_ids"]:
            raise ValueError("来源输入已变化")
        if use_v2:
            proofs = [{'scope': batch['mode'], 'owner_id': v2.owner(batch['mode'], batch['session_id']),
                       'session_id': batch['session_id'], 'message_id': mid,
                       'message_ids': [mid], 'source_id': f'message-{mid}', 'value': value.strip(),
                       'source': {'message_ids': [mid], 'session_id': batch['session_id']}}
                      for mid in snap['message_ids']]
            # Inputs are generating here; provenance status is verified on complete commit.
            if any(v2.forget_blocked(proof, db) for proof in proofs):
                raise ValueError('内容或来源已彻底忘记；禁止再次提炼')
        expected = source["revisions"].get(f"{batch['mode']}:{key}", 0)
        document = {"value": value.strip(), "category": category}
        if occurred_at is not None:
            document["occurred_at"] = occurred_at
        db.execute(
            "INSERT INTO memory7_candidates VALUES(?,?,?,?,?,?,'pending','') ON CONFLICT(batch_id,attempt_id,key) DO UPDATE SET document=excluded.document WHERE memory7_candidates.status='pending'",
            (str(uuid4()), batch["id"], ctx["attempt_id"], key, stage7.dump(document), expected),
        )
    return "候选记忆已暂存；本轮成功完成并通过来源与版本检查后才会生效。"


def discard_candidates(db, bid, reason):
    db.execute(
        "UPDATE memory7_candidates SET status='discarded',reason=? WHERE batch_id=? AND status='pending'",
        (reason, bid),
    )


def commit_candidates(db, batch):
    attempt = db.execute(
        "SELECT * FROM memory7_attempts WHERE batch_id=?", (batch["id"],)
    ).fetchone()
    if not attempt:
        return
    source = json.loads(attempt["source"])
    source.pop("revisions", None)
    use_v2 = v2_bridge.active(db)
    if use_v2 != (source.get('memory_authority', 'legacy') == 'v2'):
        discard_candidates(db, batch['id'], '记忆主库已切换；旧尝试不跨库提交')
        return
    if source.get('external_material'):
        discard_candidates(db,batch['id'],'本轮含附件资料，不自动提炼个人记忆')
        return
    from app.timeline import version
    if source.get('session_version', 0) != version(db, batch['session_id']):
        discard_candidates(db, batch['id'], '会话版本已变化')
        return
    if not stage7.source_valid(db, source) or not stage7.settings(db)["auto_memory_enabled"]:
        discard_candidates(db, batch["id"], "来源失效或自动记忆已关闭")
        return
    candidates = db.execute(
        "SELECT * FROM memory7_candidates WHERE batch_id=? AND attempt_id=? AND status='pending'",
        (batch["id"], attempt["attempt_id"]),
    ).fetchall()
    revisions = json.loads(attempt["source"])["revisions"]
    for candidate in candidates:
        data = json.loads(candidate["document"])
        if use_v2:
            try:
                status = v2_bridge.commit_candidate(db, batch, candidate, data, source)
                db.execute('UPDATE memory7_candidates SET status=?,reason=? WHERE id=?',
                           (status, 'V2单主库提交；共享须用户显式授权' if status == 'committed' else '内容相同，不新增版本',
                            candidate['id']))
            except v2.MemoryError as exc:
                db.execute("UPDATE memory7_candidates SET status='rejected',reason=? WHERE id=?",
                           (str(exc), candidate['id']))
            continue
        items = {
            i["key"]: i
            for i in stage7.list_items(
                batch["mode"], batch["session_id"] if batch["mode"] == "work" else None, db
            )
            if i["kind"] == "fact"
        }
        old = items.get(candidate["key"])
        # 已删除的键也有版本，不能把其修订号当0而复活。
        version_count = db.execute(
            "SELECT COUNT(*) FROM fact_versions WHERE scope=? AND owner_id=? AND key=?",
            (batch["mode"], stage7.owner(batch["mode"], batch["session_id"]), candidate["key"]),
        ).fetchone()[0]
        current_revision = version_count or (old["revision"] if old else 0)
        if current_revision != candidate["expected_revision"]:
            db.execute(
                "UPDATE memory7_candidates SET status='rejected',reason='记忆版本冲突；保留用户的新版本' WHERE id=?",
                (candidate["id"],),
            )
            continue
        if old and old["value"] == data["value"]:
            db.execute(
                "UPDATE memory7_candidates SET status='duplicate',reason='内容相同，不新增版本' WHERE id=?",
                (candidate["id"],),
            )
            continue
        ctx = {
            "mode": batch["mode"],
            "session_id": batch["session_id"],
            "expected_revision": current_revision,
            "origin": "自动记忆候选提交",
            **source,
            "category": data["category"],
            "confirmed_at": stage7.now(),
        }
        if "occurred_at" in data:
            ctx["occurred_at"] = data["occurred_at"]
        stage5.set_fact(candidate["key"], data["value"], ctx, db)
        db.execute(
            "UPDATE memory7_candidates SET status='committed' WHERE id=?", (candidate["id"],)
        )
        if (
            batch["mode"] == "companion"
            and data["category"] in stage7.settings(db)["shared_auto_categories"]
        ):
            latest = db.execute(
                "SELECT * FROM fact_versions WHERE scope='companion' AND owner_id='companion-global' AND key=? ORDER BY id DESC LIMIT 1",
                (candidate["key"],),
            ).fetchone()
            shared_revision = db.execute(
                "SELECT COUNT(*) FROM fact_versions WHERE scope='shared' AND owner_id='global' AND key=?",
                (candidate["key"],),
            ).fetchone()[0]
            if shared_revision != revisions.get("shared:" + candidate["key"], 0):
                db.execute(
                    "UPDATE memory7_candidates SET reason='陪伴记忆已提交；共享版本冲突，未覆盖' WHERE id=?",
                    (candidate["id"],),
                )
                continue
            stage5.set_fact(
                candidate["key"],
                data["value"],
                {
                    **ctx,
                    "mode": "shared",
                    "expected_revision": shared_revision,
                    "source_memory_version_id": latest["id"],
                    "origin": "允许类别的自动共享",
                },
                db,
            )
