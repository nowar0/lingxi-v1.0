"""Lingxi-native extensions inspired by DSH; no DSH runtime or third-party assets."""

import asyncio
import base64
import hashlib
import io
import json
import re
import sqlite3
import stat
import time
import zipfile
from datetime import date, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import catalog, config, stage5
from app.db import connect

router = APIRouter(prefix="/v7")
TZ = timezone(timedelta(hours=8))
_imports = {}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def init_table():
    from app import db, migrations

    marker = config.DATA_DIR / "backups" / "extension-schema-backup.json"
    if db.DB_PATH.is_file() and not marker.exists():
        saved = migrations.backup(config.DATA_DIR)
        marker.write_text(
            stage5.dump({"backup": saved, "schema": "dsh-native-extensions-v1"}), encoding="utf-8"
        )
    with connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS extension_forks(request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, session_id INTEGER NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS extension_sources(skill_id TEXT PRIMARY KEY, document TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS extension_budget(id TEXT PRIMARY KEY, day TEXT NOT NULL, job_id TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER NOT NULL, known INTEGER NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS extension_budget_day ON extension_budget(day)")
        db.execute("CREATE TABLE IF NOT EXISTS extension_media_budget(id TEXT PRIMARY KEY,day TEXT NOT NULL,job_id TEXT NOT NULL,module_id TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS extension_media_budget_day ON extension_media_budget(day)")
        db.execute("UPDATE extension_media_budget SET status='unknown' WHERE status='submitted'")
        db.execute(
            "CREATE TABLE IF NOT EXISTS extension_schedule_runs(schedule_id TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL, job_id TEXT, detail TEXT NOT NULL, PRIMARY KEY(schedule_id,due))"
        )
        db.execute(
            "UPDATE extension_schedule_runs SET status='interrupted',detail='服务中断，未自动重放；请人工检查任务列表' WHERE status='launching'"
        )


def optional(kind, item_id, default=None):
    try:
        return catalog.get(kind, item_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return default if default is not None else {"revision": 0}


def busy(db, sid):
    from app import chat, persistent_terminal, enhanced_terminal, git_commits, git_transition

    return bool(
        git_transition.is_running(sid,db) or git_commits.is_running(sid) or persistent_terminal.is_running(sid)
        or enhanced_terminal.is_running(sid) or ("session", sid) in chat.active
        or db.execute(
            "SELECT 1 FROM stage6_batches WHERE session_id=? AND status='generating'", (sid,)
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM messages WHERE session_id=? AND archived=0 AND delivery_status='queued'",
            (sid,),
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM work_jobs WHERE session_id=? AND status IN ('running','pausing')", (sid,)
        ).fetchone()
    )


class EditBranch(Strict):
    message_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=8000)
    expected_version: int = Field(ge=0)
    request_id: UUID


@router.post("/sessions/{sid}/edit-branch")
def edit_branch(sid: int, body: EditBranch):
    """Fork a complete work-history prefix and queue the replacement exactly once."""
    if not body.text.strip():
        raise HTTPException(422, "消息不能为空")
    digest = hashlib.sha256(
        stage5.dump({"sid": sid, **body.model_dump(mode="json")}).encode()
    ).hexdigest()
    key = str(body.request_id)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM extension_forks WHERE request_id=?", (key,)).fetchone()
        if previous:
            if previous["fingerprint"] != digest:
                raise HTTPException(409, "操作编号已用于不同内容")
            target = db.execute(
                "SELECT * FROM sessions WHERE id=?", (previous["session_id"],)
            ).fetchone()
            if not target:
                raise HTTPException(409, "已创建的分支被删除，不能重放旧操作")
            return {"session": dict(target), "replayed": True}
        source = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not source or source["mode"] != "work":
            raise HTTPException(409, "编辑重发仅支持工作会话")
        if (
            source["timeline_version"] != body.expected_version
            or source["archived"]
            or busy(db, sid)
        ):
            raise HTTPException(409, "会话正在运行、已归档或版本变化，请刷新后操作")
        original = db.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=? AND archived=0",
            (body.message_id, sid),
        ).fetchone()
        if not original or original["role"] != "user" or original["delivery_status"] != "complete":
            raise HTTPException(409, "只能编辑已完成回合中的纯文本用户消息")
        # Queued inputs can share a single assistant batch. A prefix must not copy half a batch.
        if (
            original["batch_id"]
            and db.execute(
                "SELECT 1 FROM messages WHERE session_id=? AND batch_id=? AND role='user' AND id<? AND archived=0",
                (sid, original["batch_id"], original["id"]),
            ).fetchone()
        ):
            raise HTTPException(
                409, "这条输入与更早输入合并为同一回复，请从该批次第一条用户消息编辑"
            )
        at = stage5.now()
        new_id = db.execute(
            "INSERT INTO sessions(title,mode,created_at,updated_at) VALUES(?,?,?,?)",
            ((source["title"] or "工作")[:65] + "（编辑分支）", "work", at, at),
        ).lastrowid
        # Copy complete earlier turns as ordinary immutable history; do not transplant queue ids.
        rows = db.execute(
            "SELECT * FROM messages WHERE session_id=? AND id<? AND archived=0 AND delivery_status='complete' ORDER BY id",
            (sid, body.message_id),
        ).fetchall()
        for row in rows:
            db.execute(
                "INSERT INTO messages(session_id,role,content,at,speaker_snapshot,delivery_status) VALUES(?,?,?,?,?,'complete')",
                (new_id, row["role"], row["content"], row["at"], row["speaker_snapshot"]),
            )
        record = db.execute(
            "SELECT document FROM stage6_work_records WHERE session_id=?", (sid,)
        ).fetchone()
        if record:
            db.execute(
                "INSERT INTO stage6_work_records VALUES(?,?,1)", (new_id, record["document"])
            )
        # Copy the selected project binding, never copy prior approvals or executed task state.
        for kind, old_id, new_doc_id in [
            ("work-settings", str(sid), str(new_id)),
            ("workspace", "session_" + str(sid), "session_" + str(new_id)),
            ("agent-binding", str(sid), str(new_id)),
        ]:
            saved = db.execute(
                "SELECT document FROM capability_documents WHERE kind=? AND id=?", (kind, old_id)
            ).fetchone()
            if saved:
                document = json.loads(saved["document"])
                if document.get("deleted"):
                    continue
                if "session_id" in document:
                    document["session_id"] = new_id
                if "frozen" in document:
                    document.pop("frozen", None)
                raw = stage5.dump(document)
                db.execute(
                    "INSERT INTO capability_documents VALUES(?,?,1,?,?)",
                    (kind, new_doc_id, raw, at),
                )
                db.execute(
                    "INSERT INTO capability_versions VALUES(?,?,1,?,?)", (kind, new_doc_id, raw, at)
                )
        # Saving a draft inside the creation transaction makes a failed network response recoverable.
        db.execute("INSERT INTO session_drafts VALUES(?,?,1)", (new_id, body.text))
        db.execute("INSERT INTO extension_forks VALUES(?,?,?)", (key, digest, new_id))
        target = dict(db.execute("SELECT * FROM sessions WHERE id=?", (new_id,)).fetchone())
    return {"session": target, "replayed": False}


@router.get("/sessions")
def session_list(
    q: str = Query("", max_length=200),
    mode: str = "work",
    archived: str = "all",
    sort: str = "updated",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    orders = {
        "updated": "s.updated_at DESC,s.id DESC",
        "created": "s.created_at DESC,s.id DESC",
        "title": "s.title,s.id",
        "size": "bytes DESC,s.id DESC",
    }
    if (
        sort not in orders
        or mode not in ("work", "companion", "all")
        or archived not in ("all", "yes", "no")
    ):
        raise HTTPException(422, "筛选条件无效")
    where, args = ["s.mode IN ('work','companion')"], []
    if mode != "all":
        where.append("s.mode=?")
        args.append(mode)
    if archived != "all":
        where.append("s.archived=?")
        args.append(int(archived == "yes"))
    if q:
        where.append("(instr(lower(s.title),lower(?))>0 OR CAST(s.id AS TEXT)=?)")
        args += [q, q]
    clause = " AND ".join(where)
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM sessions s WHERE " + clause, args).fetchone()[0]
        rows = db.execute(
            f"""SELECT s.*,COUNT(m.id) AS messages,COALESCE(SUM(length(CAST(m.content AS BLOB))),0) AS bytes
             FROM sessions s LEFT JOIN messages m ON m.session_id=s.id AND m.archived=0 WHERE {clause}
             GROUP BY s.id ORDER BY {orders[sort]} LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        ).fetchall()
        items = [{**dict(r), "busy": busy(db, r["id"])} for r in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit}


class Selection(Strict):
    id: int = Field(gt=0)
    updated_at: str


class Bulk(Strict):
    action: str
    items: list[Selection] = Field(min_length=1, max_length=100)
    current_session_id: int | None = None
    confirm: str = ""


@router.post("/sessions/bulk")
def bulk(body: Bulk):
    if body.action not in ("archive", "restore", "delete"):
        raise HTTPException(422, "未知批量操作")
    if body.action == "delete" and body.confirm != "删除" + str(len(body.items)) + "个会话":
        raise HTTPException(422, "请填写展示的删除确认文字")
    if len({i.id for i in body.items}) != len(body.items):
        raise HTTPException(422, "重复的会话编号")
    results = []
    for item in body.items:
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM sessions WHERE id=?", (item.id,)).fetchone()
                if not row or row["mode"] != "work":
                    raise HTTPException(409, "批量修改只支持工作会话，陪伴历史保持现状")
                if item.id == body.current_session_id or busy(db, item.id):
                    raise HTTPException(409, "当前会话或正在运行的会话受到保护")
                if row["updated_at"] != item.updated_at:
                    raise HTTPException(409, "会话已更新，请刷新后重选")
                if body.action == "delete":
                    from app import session_lifecycle
                    session_lifecycle.remove(db, item.id)
                else:
                    db.execute(
                        "UPDATE sessions SET archived=?,updated_at=? WHERE id=?",
                        (int(body.action == "archive"), stage5.now(), item.id),
                    )
            results.append({"id": item.id, "ok": True})
        except HTTPException as exc:
            results.append({"id": item.id, "ok": False, "reason": str(exc.detail)})
        except sqlite3.IntegrityError:
            results.append(
                {
                    "id": item.id,
                    "ok": False,
                    "reason": "包含关联记录，已取消此项删除；可先归档保留资料",
                }
            )
    return {"items": results}


MAX_IMPORT = 16 * 1024 * 1024


def skill_candidate(label, text, files=None):
    if len(text.encode("utf-8")) > 200000 or not text.startswith("---"):
        raise ValueError("SKILL.md 需要 YAML 头部且不超过 200 KB")
    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.M)
    if len(parts) != 3:
        raise ValueError("技能头部格式错误")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict):
        raise ValueError("技能头部必须是对象")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("技能名需要 1～64 位小写英文、数字、下划线或短横线")
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise ValueError("技能用途需为 1～300 字")
    if len(text) > 20000:
        raise ValueError("技能正文超过当前零汐的 20000 字限制")
    return {
        "name": name,
        "description": description.strip(),
        "body": text,
        "source": label,
        "files": files or {},
        "enabled": False,
    }


class SkillImport(Strict):
    zip_base64: str = Field(default="", max_length=24 * 1024 * 1024)
    directory: str = Field(default="", max_length=2000)


@router.post("/skills/import-preview")
def import_preview(body: SkillImport):
    candidates, errors, total = [], [], 0
    if bool(body.zip_base64) == bool(body.directory):
        raise HTTPException(422, "请选择一个 ZIP 文件或一个目录")
    try:
        if body.zip_base64:
            raw = base64.b64decode(body.zip_base64, validate=True)
            if len(raw) > MAX_IMPORT:
                raise ValueError("压缩包超过 16 MiB")
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                entries = z.infolist()
                if len(entries) > 2000:
                    raise ValueError("文件数量超过 2000")
                files = {}
                for info in entries:
                    name = info.filename
                    path = PurePosixPath(name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in name
                        or ":" in name
                        or stat.S_ISLNK(info.external_attr >> 16)
                    ):
                        raise ValueError("压缩包包含危险路径或链接")
                    from app.release import safe_name

                    safe_name(name.rstrip("/"))
                    total += info.file_size
                    if total > MAX_IMPORT or info.file_size > 2 * 1024 * 1024:
                        raise ValueError("解压后大小超出限制")
                    if not info.is_dir():
                        if name in files:
                            raise ValueError("压缩包包含重名文件")
                        files[name] = z.read(info)
                for name, data in files.items():
                    if PurePosixPath(name).name != "SKILL.md":
                        continue
                    folder = str(PurePosixPath(name).parent)
                    prefix = "" if folder == "." else folder + "/"
                    assets = {
                        p[len(prefix) :]: content
                        for p, content in files.items()
                        if p.startswith(prefix)
                    }
                    try:
                        candidates.append(skill_candidate(name, data.decode("utf-8-sig"), assets))
                    except (ValueError, UnicodeError) as exc:
                        errors.append({"source": name, "reason": str(exc)})
        else:
            root = Path(body.directory)
            if (
                not root.is_absolute()
                or not root.is_dir()
                or root.is_symlink()
                or root.is_junction()
            ):
                raise ValueError("需要存在且不是链接的绝对目录")
            root = root.resolve()
            visited = 0
            # A bounded walk refuses junctions before descending into them.
            import os

            for folder, directories, names in os.walk(root, followlinks=False):
                visited += 1
                if visited > 2000:
                    raise ValueError("目录过大，请选择较小的技能目录")
                directories[:] = [
                    d
                    for d in directories
                    if d not in (".git", ".venv", "node_modules", ".trash")
                    and not (Path(folder) / d).is_symlink()
                    and not (Path(folder) / d).is_junction()
                ]
                if "SKILL.md" not in names:
                    continue
                path = Path(folder) / "SKILL.md"
                if path.is_symlink() or path.stat().st_size > 200000:
                    errors.append({"source": str(path), "reason": "SKILL.md 是链接或过大"})
                    continue
                assets = {}
                for base, dirs, names2 in os.walk(folder, followlinks=False):
                    dirs[:] = [
                        d
                        for d in dirs
                        if d not in (".git", ".venv", "node_modules", ".trash")
                        and not (Path(base) / d).is_symlink()
                        and not (Path(base) / d).is_junction()
                    ]
                    for file in names2:
                        asset = Path(base) / file
                        if asset.is_symlink() or asset.stat().st_size > 2 * 1024 * 1024:
                            continue
                        total += asset.stat().st_size
                        if total > MAX_IMPORT:
                            raise ValueError("导入资料超过 16 MiB")
                        assets[asset.relative_to(folder).as_posix()] = asset.read_bytes()
                try:
                    candidates.append(
                        skill_candidate(str(path), path.read_text(encoding="utf-8-sig"), assets)
                    )
                except (ValueError, UnicodeError) as exc:
                    errors.append({"source": str(path), "reason": str(exc)})
        if len(candidates) > 100:
            raise ValueError("一次最多导入 100 个技能")
    except (ValueError, OSError, zipfile.BadZipFile, yaml.YAMLError) as exc:
        raise HTTPException(422, "技能导入失败：" + str(exc)) from None
    existing = {s["name"].casefold() for s in catalog.listing("skill")}
    for item in candidates:
        item["duplicate"] = item["name"].casefold() in existing
        existing.add(item["name"].casefold())
    now = time.monotonic()
    for token in list(_imports):
        if _imports[token]["expires"] <= now:
            _imports.pop(token, None)
    if len(_imports) >= 8:
        raise HTTPException(429, "待确认导入过多，请稍后再试")
    token = uuid4().hex
    _imports[token] = {"items": candidates, "expires": now + 600}
    return {
        "token": token,
        "items": [{k: v for k, v in x.items() if k not in ("files", "body")} for x in candidates],
        "errors": errors,
        "note": "重复技能跳过；新技能默认停用。附带文件保留为资料，不自动执行。",
    }


class CommitImport(Strict):
    token: str


@router.post("/skills/import-commit")
def import_commit(body: CommitImport):
    pending = _imports.pop(body.token, None)
    if not pending or pending["expires"] < time.monotonic():
        raise HTTPException(409, "导入预览已过期或已提交，请重新预览")
    results = []
    for item in pending["items"]:
        names = {s["name"].casefold() for s in catalog.listing("skill")}
        if item["name"].casefold() in names:
            results.append(
                {"name": item["name"], "status": "skipped", "reason": "已有同名技能，未覆盖"}
            )
            continue
        key = uuid4().hex
        folder = config.DATA_DIR / "skill-assets" / key
        try:
            folder.mkdir(parents=True, exist_ok=False)
            for name, data in item["files"].items():
                target = folder.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            document = catalog.Skill(
                name=item["name"],
                description=item["description"],
                body=item["body"],
                tags=["导入"],
                enabled=False,
            ).model_dump(exclude={"expected_revision"})
            catalog.put("skill", key, document, 0)
            with connect() as db:
                db.execute(
                    "INSERT INTO extension_sources VALUES(?,?)",
                    (
                        key,
                        stage5.dump(
                            {
                                "origin": item["source"],
                                "files": list(item["files"]),
                                "resources_relative": "skill-assets/" + key,
                            }
                        ),
                    ),
                )
            results.append({"name": item["name"], "id": key, "status": "imported"})
        except OSError:
            results.append(
                {
                    "name": item["name"],
                    "status": "failed",
                    "reason": "本机无法保存技能资料，请检查空间和目录权限",
                }
            )
    return {"items": results}


@router.get("/skills/sources")
def skill_sources():
    with connect() as db:
        sources = {
            r["skill_id"]: json.loads(r["document"])
            for r in db.execute("SELECT * FROM extension_sources")
        }
    return {
        "items": [
            {
                **{k: v for k, v in s.items() if k != "body"},
                "source": sources.get(s["id"], {"origin": "零汐本地创建"}),
            }
            for s in catalog.listing("skill")
        ]
    }


@router.get("/skills/trash")
def skill_trash():
    with connect() as db:
        rows = db.execute("SELECT * FROM capability_documents WHERE kind='skill'").fetchall()
    return {
        "items": [
            {**json.loads(r["document"]), "id": r["id"], "revision": r["revision"]}
            for r in rows
            if json.loads(r["document"]).get("deleted")
        ]
    }


class Revision(Strict):
    expected_revision: int = Field(ge=1)


@router.post("/skills/{key}/restore")
def restore_skill(key: str, body: Revision):
    item = catalog.get("skill", key)
    if not item.get("deleted"):
        raise HTTPException(409, "该技能不在回收站")
    if any(s["name"].casefold() == item["name"].casefold() for s in catalog.listing("skill")):
        raise HTTPException(409, "已有同名技能，请先处理名称冲突")
    document = {k: v for k, v in item.items() if k not in ("id", "revision", "deleted")}
    return catalog.put("skill", key, {**document, "enabled": False}, body.expected_revision)


@router.get("/status")
def status():
    import shutil

    return {
        "ready": True,
        "version": 1,
        "edit_branch": "work-only",
        "executables": {
            name: bool(shutil.which(name)) for name in ("git", "ssh", "sftp", "ssh-keyscan")
        },
        "sources": [
            "https://github.com/zhu1090093659/dsh-web",
            "https://github.com/zhu1090093659/dsh-skins",
            "https://github.com/zhu1090093659/dsh-presets",
        ],
    }
