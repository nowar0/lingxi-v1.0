"""Offline, verified user-data backup/restore. Never includes credential stores.

Restores create a NEW directory; they never overwrite a running installation.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from app import config, data_management

FILES = set(data_management.BACKUP_FILES)
DIRECTORIES = set(data_management.BACKUP_DIRECTORIES)
PLUGIN_REGISTRY = data_management.PLUGIN_REGISTRY
# v1/v2 archives created by earlier releases used this pure-data directory.
# Keep import compatibility without claiming that the current host uses it.
LEGACY_DIRECTORIES = {"plugin-data"}
MAX_BYTES = 2 * 1024**3
MAX_FILES = 20000
MAX_MANIFEST_BYTES = 32 * 1024**2


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(name):
    p = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or p.is_absolute()
        or any(ord(character) < 32 or character in '<>"|?*' for character in name)
        or any(v in ("", ".", "..") for v in name.split("/"))
        or any(v.endswith((" ", ".")) for v in p.parts)
        or any(
            v.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for v in p.parts
        )
    ):
        raise ValueError("备份包含非法路径")
    return p


def resource_path(path, *, restoring=False):
    return any(
        path.is_relative_to(PurePosixPath(root))
        and len(path.parts) > len(PurePosixPath(root).parts)
        for root in DIRECTORIES | (LEGACY_DIRECTORIES if restoring else set())
    )


def relocate_resources(stage, destination, old_root):
    """Rewrite only exact references to verified, restored business resources.

    Never rewrite chat/memory text or external model/project installations.
    Historical configuration revisions remain usable at the new location.
    """
    if not isinstance(old_root, str) or not Path(old_root).is_absolute():
        return
    old_root = Path(old_root)

    def rewrite(value):
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, str):
            try:
                relative = Path(value).relative_to(old_root)
                if (
                    resource_path(PurePosixPath(relative.as_posix()))
                    and (stage / relative).is_file()
                ):
                    return str(destination / relative)
            except ValueError, OSError:
                pass
        return value

    for name in FILES:
        file = stage / name
        if file.is_file():
            original = json.loads(file.read_text(encoding="utf-8-sig"))
            changed = rewrite(original)
            if changed != original:
                file.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with closing(sqlite3.connect(stage / "lingxi.db")) as conn, conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in (
            "capability_documents",
            "capability_versions",
            "character_cards",
            "character_versions",
        ):
            if table not in tables:
                continue
            for rowid, raw in conn.execute(f"SELECT rowid,document FROM {table}").fetchall():
                original = json.loads(raw)
                changed = rewrite(original)
                if changed != original:
                    conn.execute(
                        f"UPDATE {table} SET document=? WHERE rowid=?",
                        (json.dumps(changed, ensure_ascii=False), rowid),
                    )


def check_database(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("数据库完整性检查失败")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("数据库外键完整性检查失败")


def snapshot_plugin_registry(root, stage):
    """Copy through SQLite backup, never copy the live main/WAL files directly."""
    registry = root / PLUGIN_REGISTRY
    state = data_management.plugin_registry_state(root)
    if state is None:
        return False
    target = stage / PLUGIN_REGISTRY
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(data_management.readonly_database(registry)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            # A portable snapshot must not inherit source WAL mode and emit sidecars.
            destination.execute("PRAGMA journal_mode=DELETE")
    check_database(target)
    # The copied registry must describe the same transaction as the idle guard.
    if data_management.plugin_registry_state(stage) != state:
        raise ValueError("插件注册表在快照期间变化，请停止插件后重试")
    # Compare the raw logical snapshot first. Only then sanitize the EXPORT COPY:
    # runtime previous generations and terminal intent are not portable user data.
    from app.plugin_data_transactions import sanitize_portable_snapshot
    with closing(sqlite3.connect(target)) as destination, destination:
        sanitize_portable_snapshot(destination)
        # Validate again after copy-only removal of previous generation rows and
        # their old config/state bodies. Never remove them from the live registry.
        data_management.validate_plugin_registry(destination)
        if destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_data_transactions'").fetchone() and destination.execute('SELECT 1 FROM plugin_data_transactions LIMIT 1').fetchone():
            raise ValueError('运行保护事务正文不能进入普通用户备份')
    return True


def disable_restored_plugins(stage):
    """Modify ONLY a verified staging snapshot; importing never reuses old consent."""
    registry = stage / PLUGIN_REGISTRY
    if not registry.is_file():
        return False
    check_database(registry)
    with closing(sqlite3.connect(registry)) as conn, conn:
        tables, columns = data_management.validate_plugin_registry(conn)
        from app.plugin_data_transactions import validate_backup_data, sanitize_portable_snapshot
        # Reject hostile/pending intent BEFORE clearing approvals or markers.
        validate_backup_data(conn,stage/'plugin-packages')
        sanitize_portable_snapshot(conn)
        assignments = ["enabled=0", "loaded=0"]
        assignments.extend(
            f"{column}=0" for column in ("needs_restart", "pending_update") if column in columns
        )
        if "error" in columns:
            assignments.append("error='restored_disabled_code_not_included'")
        conn.execute("UPDATE plugins SET " + ",".join(assignments))
        # Deferred cleanup is executable intent too: do not let restored outbox erase data.
        for table in ("approvals", "cleanup"):
            if table in tables:
                conn.execute("DELETE FROM " + table)
    check_database(registry)
    return True


def export_backup(destination, root=None, database=None):
    root = data_management.assert_no_links(root or config.DATA_DIR).resolve()
    database = data_management.assert_no_links(database or root / "lingxi.db").resolve()
    destination = data_management.assert_no_links(destination).resolve()
    if database != root / "lingxi.db":
        raise ValueError("只能备份当前数据根目录的 lingxi.db")
    data_management.assert_idle(database)
    if destination.exists():
        raise ValueError("备份目标已存在，请使用新文件名")
    if not database.is_file():
        raise ValueError("没有可备份的数据库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lingxi-export-") as temp:
        stage = Path(temp)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(stage / "lingxi.db")) as target:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
        check_database(stage / "lingxi.db")
        plugin_state = data_management.plugin_backup_state(root)
        paths = [root / name for name in sorted(FILES) if (root / name).is_file()]
        for name in sorted(DIRECTORIES):
            directory = data_management.assert_no_links(root / name)
            if directory.is_dir():
                for item in data_management._walk_files(directory):
                    paths.append(item)
                    if len(paths) + 1 > MAX_FILES:
                        raise ValueError("备份超出文件数量限制")
        total = (stage / "lingxi.db").stat().st_size
        if total > MAX_BYTES or len(paths) + 1 > MAX_FILES:
            raise ValueError("备份超出数量或容量限制")
        for item in paths:
            if item.is_symlink() or item.is_junction():
                raise ValueError("备份不跟随业务文件链接")
            relative = item.relative_to(root).as_posix()
            safe_name(relative)
            total += item.stat().st_size
            if total > MAX_BYTES or len(paths) + 1 > MAX_FILES:
                raise ValueError("备份超过 2 GiB 或 20000 个文件限制")
            before = item.stat()
            if before.st_nlink != 1:
                raise ValueError("备份不接受业务文件硬链接")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            after = item.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or target.stat().st_size != before.st_size:
                raise ValueError("业务文件在导出期间变化，请停止相关任务后重试")
        snapshot_plugin_registry(root, stage)
        data_management.assert_plugin_backup_unchanged(root, plugin_state)
        staged_files = [p for p in stage.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in staged_files)
        if total > MAX_BYTES or len(staged_files) > MAX_FILES:
            raise ValueError("备份超出容量限制")
        manifest = {
            "format": "lingxi-user-backup",
            "version": 2,
            "source_root": str(root),
            "inventory": data_management.data_inventory(root),
            "files": {
                p.relative_to(stage).as_posix(): {"sha256": digest(p), "size": p.stat().st_size}
                for p in stage.rglob("*")
                if p.is_file()
            },
        }
        encoded_manifest = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        if len(encoded_manifest) > MAX_MANIFEST_BYTES:
            raise ValueError("备份清单过大")
        (stage / "manifest.json").write_bytes(encoded_manifest)
        # Build a same-volume temporary ZIP, fsync, then publish without replacement.
        # os.link is atomic and fails if a competing writer created destination.
        fd, temporary = tempfile.mkstemp(
            prefix=".lingxi-export-", suffix=".zip.part", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for p in stage.rglob("*"):
                    if p.is_file():
                        archive.write(p, p.relative_to(stage).as_posix())
            with temporary.open("rb+") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            data_management.assert_idle(database)
            data_management.assert_plugin_backup_unchanged(root, plugin_state)
            data_management.assert_no_links(destination)
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "path": str(destination),
        "files": len(manifest["files"]),
        "bytes": total,
        "version": 2,
        "inventory": manifest["inventory"],
    }


def restore_backup(archive_path, destination):
    archive_path = data_management.assert_no_links(archive_path)
    destination = data_management.assert_no_links(destination).resolve()
    if destination.exists():
        raise ValueError("恢复目标必须是不存在的新目录，不覆盖现有数据")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lingxi-restore-", dir=destination.parent) as temp:
        stage = Path(temp) / "data"
        stage.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if (
                len(infos) > MAX_FILES + 2
                or sum(i.file_size for i in infos if i.filename != "manifest.json") > MAX_BYTES
                or any(
                    i.file_size > MAX_MANIFEST_BYTES for i in infos if i.filename == "manifest.json"
                )
            ):
                raise ValueError("备份超出数量或容量限制")
            seen, prefixes, spellings = set(), set(), {}
            for info in infos:
                p = safe_name(info.filename)
                folded = info.filename.casefold()
                parents = ["/".join(p.parts[:index]) for index in range(1, len(p.parts))]
                if (
                    info.is_dir()
                    or folded in seen
                    or folded in prefixes
                    or any(parent.casefold() in seen for parent in parents)
                ):
                    raise ValueError("备份含目录占位、重复或冲突路径")
                for partial in [*parents, info.filename]:
                    previous = spellings.setdefault(partial.casefold(), partial)
                    if previous != partial:
                        raise ValueError("备份含大小写冲突路径")
                seen.add(folded)
                prefixes.update(parent.casefold() for parent in parents)
                if info.filename not in FILES | {
                    "manifest.json",
                    "lingxi.db",
                    PLUGIN_REGISTRY,
                } and not resource_path(p, restoring=True):
                    raise ValueError("备份包含非业务文件")
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in (0, 0o100000) or info.external_attr & 0x400:
                    raise ValueError("备份包含符号链接、重解析点或特殊文件")
                target = stage.joinpath(*p.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as source, target.open("xb") as sink:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size:
                            raise ValueError("解压文件超过声明大小")
                        sink.write(chunk)
                if written != info.file_size:
                    raise ValueError("解压文件大小不一致")
        if not (stage / "manifest.json").is_file():
            raise ValueError("备份缺少清单")
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != "lingxi-user-backup"
            or manifest.get("version") not in (1, 2)
        ):
            raise ValueError("不支持的备份格式")
        expected = manifest.get("files", {})
        if not isinstance(expected, dict):
            raise ValueError("备份清单格式错误")
        actual = {
            p.relative_to(stage).as_posix()
            for p in stage.rglob("*")
            if p.is_file() and p.relative_to(stage).as_posix() != "manifest.json"
        }
        if set(expected) != actual or "lingxi.db" not in actual:
            raise ValueError("备份清单与实际文件不一致")
        for name, metadata in expected.items():
            safe_name(name)
            if (
                not isinstance(metadata, dict)
                or type(metadata.get("size")) is not int
                or metadata["size"] < 0
                or not isinstance(metadata.get("sha256"), str)
                or len(metadata["sha256"]) != 64
            ):
                raise ValueError("备份文件元数据错误：" + name)
            target = stage / name
            if target.stat().st_size != metadata["size"] or digest(target) != metadata["sha256"]:
                raise ValueError("备份文件校验失败：" + name)
        check_database(stage / "lingxi.db")
        disable_restored_plugins(stage)
        from app import migrations

        migrations.guard_schema(stage / "lingxi.db")
        relocate_resources(stage, destination, manifest.get("source_root"))
        check_database(stage / "lingxi.db")
        (stage / "manifest.json").unlink()
        # Same-volume rename publishes only a fully verified restore.
        data_management.assert_no_links(destination)
        if destination.exists():
            raise ValueError("恢复目标已出现，未覆盖现有数据")
        os.rename(stage, destination)
    return {"path": str(destination), "files": len(expected)}


def main():
    parser = argparse.ArgumentParser(description="零汐离线备份与恢复；请先退出服务")
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.action == "backup":
        result = export_backup(args.path)
    else:
        if not args.destination:
            parser.error("恢复必须指定 --destination 新目录")
        result = restore_backup(args.path, args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
