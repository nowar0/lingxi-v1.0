"""迁移备份只包含本项目业务数据，不包含模型凭据。"""

import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app import data_management, db


def ensure_schema_backup(data_dir):
    root = Path(data_dir)
    marker = root / "backups" / "stage5-schema-backup.json"
    if db.DB_PATH.exists() and not marker.exists():
        relative = backup(root)
        marker.write_text(json.dumps({"backup": relative}, ensure_ascii=False), encoding="utf-8")


def backup(data_dir, browser_cache=None):
    """Migration rollback snapshot, deliberately separate from portable user ZIPs."""
    from app import release

    root = data_management.assert_no_links(data_dir).resolve()
    if Path(db.DB_PATH).resolve() != root / "lingxi.db":
        raise ValueError("迁移快照数据库必须位于指定数据根目录")
    if browser_cache is not None and (
        not isinstance(browser_cache, str) or len(browser_cache) > 20_000_000
    ):
        raise ValueError("浏览器正式缓存备份格式或大小错误")
    target = (
        root
        / "backups"
        / ("stage5-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    )
    stage = target.with_name("." + target.name + ".staging")
    data_management.assert_no_links(stage)
    stage.mkdir(parents=True)
    try:
        with (
            closing(data_management.readonly_database(db.DB_PATH)) as source,
            closing(sqlite3.connect(stage / "lingxi.db")) as dest,
        ):
            source.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
        release.check_database(stage / "lingxi.db")
        plugin_state = data_management.plugin_backup_state(root)
        total, count = (stage / "lingxi.db").stat().st_size, 1
        for name in sorted(release.FILES | release.DIRECTORIES):
            item = root / name
            data_management.assert_no_links(item)
            if item.is_dir():
                for source in data_management._walk_files(item):
                    total += source.stat().st_size
                    count += 1
                    if total > release.MAX_BYTES or count > release.MAX_FILES:
                        raise ValueError("迁移快照超出数量或容量限制")
                    if source.stat().st_nlink != 1:
                        raise ValueError("迁移快照不接受业务硬链接")
                    destination = stage / source.relative_to(root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            elif item.is_file():
                total += item.stat().st_size
                count += 1
                if total > release.MAX_BYTES or count > release.MAX_FILES:
                    raise ValueError("迁移快照超出数量或容量限制")
                if item.stat().st_nlink != 1:
                    raise ValueError("迁移快照不接受业务硬链接")
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
        release.snapshot_plugin_registry(root, stage)
        data_management.assert_plugin_backup_unchanged(root, plugin_state)
        staged = [path for path in stage.rglob("*") if path.is_file()]
        if (
            len(staged) > release.MAX_FILES
            or sum(path.stat().st_size for path in staged) > release.MAX_BYTES
        ):
            raise ValueError("迁移快照超出数量或容量限制")
        if browser_cache is not None:
            (stage / "browser-formal-cache.json").write_text(browser_cache, encoding="utf-8")
        manifest = {
            p.relative_to(stage).as_posix(): data_management.sha256(p)
            for p in stage.rglob("*")
            if p.is_file()
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.rename(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return target.relative_to(root).as_posix()


def ensure_capabilities_backup(data_dir):
    root = Path(data_dir)
    marker = root / "backups" / "stage10-15-schema-backup.json"
    if db.DB_PATH.exists() and not marker.exists():
        relative = backup(root)
        marker.write_text(
            json.dumps({"backup": relative, "schema": "stage10-15"}, ensure_ascii=False),
            encoding="utf-8",
        )


class SchemaVersionError(ValueError):
    """A newer database must never be silently opened by an older executable."""


def schema_status(database=None, supported_version=data_management.SCHEMA_VERSION):
    database = Path(database or db.DB_PATH)
    if not database.exists():
        return {
            "schema_version": 0,
            "supported_schema_version": supported_version,
            "compatible": True,
            "state": "new",
            "downgrade_allowed": False,
        }
    with closing(data_management.readonly_database(database)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checkpoint = None
        if "lingxi_schema_checkpoints" in tables:
            row = conn.execute(
                "SELECT target_version,status,error FROM lingxi_schema_checkpoints "
                "ORDER BY target_version DESC LIMIT 1"
            ).fetchone()
            if row:
                checkpoint = {"target_version": row[0], "status": row[1], "error": row[2]}
    return {
        "schema_version": version,
        "supported_schema_version": supported_version,
        "compatible": version <= supported_version,
        "checkpoint": checkpoint,
        "state": "newer_schema_blocked" if version > supported_version else "compatible",
        "downgrade_allowed": False,
        "notice": "schema 版本只用于防止新库被旧程序打开；不宣称完整安装或真实旧数据验收",
    }


def guard_schema(database=None, supported_version=data_management.SCHEMA_VERSION):
    result = schema_status(database, supported_version)
    if not result["compatible"]:
        raise SchemaVersionError(
            "数据 schema 高于此程序支持版本；禁止降级打开。"
            "请使用匹配版本程序，或离线恢复旧快照到新目录。"
        )
    return result


def _checkpoint_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS lingxi_schema_checkpoints(
        target_version INTEGER PRIMARY KEY, source_version INTEGER NOT NULL,
        status TEXT NOT NULL, backup_path TEXT, attempts INTEGER NOT NULL,
        error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""")


def prepare_upgrade(data_dir, database=None, target_version=data_management.SCHEMA_VERSION):
    """Call before existing init_table functions. A failed attempt reuses its checkpoint.

    The pre-upgrade SQLite snapshot is immutable and never replaces the running DB.
    The caller must explicitly call finish_upgrade only after all initializers succeed.
    """
    if type(target_version) is not int or not 1 <= target_version <= data_management.SCHEMA_VERSION:
        raise ValueError("迁移目标 schema 版本不受支持")
    root = data_management.assert_no_links(data_dir).resolve()
    database = data_management.assert_no_links(database or root / "lingxi.db")
    if database != root / "lingxi.db":
        raise ValueError("schema 迁移只能操作数据根目录的 lingxi.db")
    status = guard_schema(database, target_version)
    root.mkdir(parents=True, exist_ok=True)
    source_version = status["schema_version"]
    with closing(sqlite3.connect(database)) as conn:
        _checkpoint_tables(conn)
        conn.commit()
        row = conn.execute(
            "SELECT source_version,status,backup_path,attempts "
            "FROM lingxi_schema_checkpoints WHERE target_version=?",
            (target_version,),
        ).fetchone()
        if source_version == target_version and (not row or row[1] == "complete"):
            return {**status, "migration_required": False, "checkpoint_status": "complete"}
        backup_path = row[2] if row else None
        if backup_path:
            relative = Path(backup_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.is_relative_to(Path("backups/schema"))
            ):
                raise ValueError("迁移检查点路径不在受控 schema 快照目录")
            snapshot = root / relative
            data_management.assert_no_links(snapshot)
            if not snapshot.is_file():
                raise ValueError("迁移检查点快照缺失；请先恢复快照，禁止静默创建新基线")
            from app import release

            release.check_database(snapshot)
        elif status["state"] != "new":
            folder = (
                root
                / "backups"
                / "schema"
                / ("v" + str(source_version) + "-to-v" + str(target_version) + "-" + uuid4().hex)
            )
            folder.mkdir(parents=True)
            stage = folder / "lingxi.db.part"
            snapshot = folder / "lingxi.db"
            with (
                closing(data_management.readonly_database(database)) as source,
                closing(sqlite3.connect(stage)) as dest,
            ):
                source.backup(dest)
            from app import release

            release.check_database(stage)
            os.rename(stage, snapshot)
            backup_path = snapshot.relative_to(root).as_posix()
        attempts = (row[3] if row else 0) + 1
        with conn:
            conn.execute(
                "INSERT INTO lingxi_schema_checkpoints VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(target_version) DO UPDATE SET status=excluded.status,"
                "attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
                (
                    target_version,
                    row[0] if row else source_version,
                    "running",
                    backup_path,
                    attempts,
                    "",
                    datetime.now().isoformat(),
                ),
            )
    return {
        "migration_required": True,
        "source_version": source_version,
        "target_version": target_version,
        "checkpoint_status": "running",
        "backup_path": backup_path,
        "attempts": attempts,
    }


def finish_upgrade(database=None, *, error=None, target_version=data_management.SCHEMA_VERSION):
    if type(target_version) is not int or not 1 <= target_version <= data_management.SCHEMA_VERSION:
        raise ValueError("迁移目标 schema 版本不受支持")
    database = Path(database or db.DB_PATH)
    guard_schema(database, target_version)
    with closing(sqlite3.connect(database)) as conn, conn:
        _checkpoint_tables(conn)
        row = conn.execute(
            "SELECT status FROM lingxi_schema_checkpoints WHERE target_version=?", (target_version,)
        ).fetchone()
        if not row:
            raise ValueError("缺少迁移检查点，不能标记完成")
        if error is None:
            if (
                conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or conn.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise ValueError("迁移后数据库校验失败，不能发布 schema 版本")
            conn.execute("PRAGMA user_version=" + str(int(target_version)))
        conn.execute(
            "UPDATE lingxi_schema_checkpoints SET status=?,error=?,updated_at=? "
            "WHERE target_version=?",
            (
                "failed" if error is not None else "complete",
                "schema 初始化失败；请检查日志并重试或离线恢复检查点" if error is not None else "",
                datetime.now().isoformat(),
                target_version,
            ),
        )
    return schema_status(database, target_version)


def migrate_schema(data_dir, upgrade, database=None, target_version=data_management.SCHEMA_VERSION):
    """Transactional migration callback for isolated/offline upgrades and synthetic tests."""
    database = Path(database or Path(data_dir) / "lingxi.db")
    checkpoint = prepare_upgrade(data_dir, database, target_version)
    if not checkpoint["migration_required"]:
        return checkpoint
    try:
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            upgrade(conn)
        return finish_upgrade(database, target_version=target_version)
    except BaseException:
        finish_upgrade(database, error=True, target_version=target_version)
        raise
