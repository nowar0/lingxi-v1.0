"""本地数据管理边界：不读取凭据，不启动插件，不改变正式数据路径。

用户备份、迁移回滚快照、src/开发备份是三种独立产物。本模块的所有测试
只能使用临时目录中的合成 SQLite 数据；不声称已验证任何用户的真实旧数据。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 4
BACKUP_FILES = frozenset({"persona.json", "user_settings.json", "browser-preferences.json"})
BACKUP_DIRECTORIES = frozenset(
    {
        "characters",
        "attachments",
        "artifacts",
        "workspaces",
        "skill-assets",
        "local-tts/references",
        "plugin-packages/data",
    }
)
PLUGIN_REGISTRY = "plugin-packages/registry.sqlite3"
# Never accept an arbitrary cache root supplied by a browser.
CLEANUP_ROOTS = {
    "logs": ("logs",),
    "temporary": ("tmp", "temp"),
    "rebuildable_cache": ("cache/rebuildable", "cache/thumbnails", "cache/embeddings"),
}
MAX_CLEANUP_FILES = 1000
MAX_CLEANUP_SCANNED = 5000
MAX_CLEANUP_BYTES = 256 * 1024**2
TOKEN_TTL = 300
MAX_TOKENS = 100
_PROTECTED = {
    "lingxi.db",
    "attachments",
    "characters",
    "artifacts",
    "workspaces",
    "skill-assets",
    "plugin-data",
    "plugin-packages",
    "plugins",
    "plugin-manager",
    "backups",
    "imports",
    "persona.json",
    "user_settings.json",
    "browser-preferences.json",
}
_tokens: dict[str, dict] = {}
_token_lock = threading.RLock()


def _root(root=None):
    if root is None:
        from app import config

        root = config.DATA_DIR
    return Path(root).absolute()


def is_link(path):
    path = Path(path)
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def assert_no_links(path):
    """Check lexical ancestors before resolve(), including dangling links/junctions."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if is_link(parent):
            raise ValueError("数据路径不能包含符号链接或目录联接")
    return path


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def readonly_database(database):
    path = assert_no_links(database)
    if not path.is_file():
        raise ValueError("没有可读取的数据库")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def assert_idle(database, *, runtime=True):
    """Refuse known live work and persisted queues before snapshotting (read-only)."""
    if runtime:
        for module_name, field in (
            ("app.chat", "active"),
            ("app.jobs", "running"),
            ("app.queue6", "tasks"),
        ):
            module = sys.modules.get(module_name)
            if module is not None and getattr(module, field, None):
                raise ValueError("请先停止聊天与工作任务，再导出或导入备份")
    with closing(readonly_database(database)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column, statuses in (
            ("messages", "delivery_status", ("queued", "generating")),
            ("work_jobs", "status", ("queued", "running", "generating", "pausing")),
            ("stage6_batches", "status", ("queued", "running", "generating")),
            ("memory7_jobs", "status", ("pending", "running")),
            ("stage5_attempts", "status", ("pending",)),
        ):
            if table not in tables:
                continue
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                continue
            placeholders = ",".join("?" for _ in statuses)
            archived = " AND archived=0" if "archived" in columns else ""
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders})" + archived + " LIMIT 1",
                statuses,
            ).fetchone():
                raise ValueError("请先处理聊天队列与工作任务，再导出或导入备份")
    plugin_registry_state(Path(database).parent)


def validate_plugin_registry(conn):
    """Only the host's inert tables; reject executable schema and secret-shaped config."""
    schema = conn.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
    if any(
        row[0] in ("trigger", "view") or "CREATE VIRTUAL" in (row[2] or "").upper()
        for row in schema
    ):
        raise ValueError("插件注册表不能包含触发器、视图或虚拟表")
    tables = {row[1] for row in schema if row[0] == "table"}
    if "plugins" not in tables or tables - {
        "plugins",
        "versions",
        "approvals",
        "audit",
        "cleanup",
        "plugin_data_generations",
        "plugin_data_transactions",
        "sqlite_sequence",
    }:
        raise ValueError("插件注册表含未知数据表或缺少 plugins 表")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plugins)")}
    if not {"enabled", "loaded", "config", "state", "credentials"}.issubset(columns):
        raise ValueError("插件注册表 schema 不受支持")

    def secret_field(value):
        if isinstance(value, dict):
            return any(
                not isinstance(key, str)
                or re.search(r"(?i)(api[_-]?key|password|secret|access[_-]?token|credential)", key)
                or secret_field(child)
                for key, child in value.items()
            )
        return isinstance(value, list) and any(secret_field(child) for child in value)

    for config_value, state_value, raw in conn.execute(
        "SELECT config,state,credentials FROM plugins"
    ):
        if secret_field(json.loads(config_value)) or secret_field(json.loads(state_value)):
            raise ValueError("插件配置/状态含密钥字段，禁止备份或恢复；请改用凭据引用")
        references = json.loads(raw)
        if (
            not isinstance(references, list)
            or len(references) > 30
            or any(
                not isinstance(value, str)
                or not re.fullmatch(
                    r"(?:vault|keyring|credential|ref):[a-zA-Z0-9_.:-]{1,100}", value
                )
                for value in references
            )
        ):
            raise ValueError("插件凭据字段只能包含引用，不能备份或恢复真实密钥")
    from app.plugin_data_transactions import validate_registry
    validate_registry(conn)
    return tables, columns


def plugin_registry_state(root=None):
    """Read an idle registry through SQLite, including WAL, without importing its host."""
    root = assert_no_links(_root(root)).resolve()
    registry = assert_no_links(root / PLUGIN_REGISTRY)
    if not registry.exists():
        return None
    if not registry.is_file() or registry.stat().st_nlink != 1:
        raise ValueError("插件注册表必须是非硬链接的普通数据库")
    from app.plugin_data_stage import pending_candidates
    if pending_candidates(root/'plugin-packages'):
        raise ValueError('插件迁移A候选仍待激活或复核，禁止备份或恢复')
    safety = assert_no_links(root/'plugin-packages/runtime-safety')
    if safety.exists():
        # Prepared migration generations are runtime transactions, not idle business
        # data. Never clear them merely to make an export appear healthy.
        for plugin in safety.iterdir():
            assert_no_links(plugin)
            stage = assert_no_links(plugin/'stage')
            if stage.exists() and any(stage.iterdir()):
                raise ValueError('插件迁移暂存尚未完成，禁止备份或恢复')
    try:
        with closing(readonly_database(registry)) as conn:
            conn.execute("BEGIN")
            tables, columns = validate_plugin_registry(conn)
            from app.plugin_data_transactions import assert_backup_idle
            assert_backup_idle(conn)
            if conn.execute("SELECT 1 FROM plugins WHERE loaded!=0 LIMIT 1").fetchone():
                raise ValueError("请先停止运行中的插件，再导出或导入备份")
            if (
                "status" in columns
                and conn.execute(
                    "SELECT 1 FROM plugins WHERE status IN ('running','starting','loading') LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("请先停止运行中的插件，再导出或导入备份")
            if "cleanup" in tables and conn.execute("SELECT 1 FROM cleanup LIMIT 1").fetchone():
                raise ValueError("插件清理操作未完成，请先处理后再备份")
            # iterdump reads logical content, not an unsafe main-file/WAL byte copy.
            digest = hashlib.sha256()
            for statement in conn.iterdump():
                digest.update(statement.encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()
    except sqlite3.Error as exc:
        raise ValueError("插件注册表校验失败；未读取或导出系统凭据") from exc


def plugin_backup_state(root=None):
    """Bounded fingerprint of registry + exact persistent-data set for export guards."""
    root = assert_no_links(_root(root)).resolve()
    registry = plugin_registry_state(root)
    if registry is not None:
        from app.plugin_data_transactions import validate_backup_data, DataTransactionError
        try:
            with closing(readonly_database(root/PLUGIN_REGISTRY)) as conn:
                validate_backup_data(conn,root/'plugin-packages')
        except DataTransactionError as exc:
            # Never adopt a dirty cached inventory during export. A trusted host
            # must explicitly reconcile it only after all runtime cleanup is idle.
            raise DataTransactionError('插件活动数据尚未核实；请停止调用并明确执行宿主 idle inventory refresh 后再备份，未自动重写库存') from exc
    files = {}
    total = 0
    for path in _walk_files(root / "plugin-packages/data"):
        before = path.stat()
        if before.st_nlink != 1:
            raise ValueError("插件持久数据不接受硬链接")
        total += before.st_size
        if len(files) >= 20000 or total > 2 * 1024**3:
            raise ValueError("插件持久数据超过备份容量限制")
        value = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            sha256(path),
        )
        after = path.stat()
        if value[:5] != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("插件数据在扫描期间变化，请停止插件后重试")
        files[path.relative_to(root).as_posix()] = value
    return {"registry": registry, "data_files": files}


def assert_plugin_backup_unchanged(root, expected):
    if plugin_backup_state(root) != expected:
        raise ValueError("插件注册表或持久数据在导出期间变化，未发布备份；请停止插件后重试")


def data_inventory(root=None):
    """Machine-readable coverage; presence does not pretend external resources are copied."""
    root = _root(root)
    categories = [
        ("chat", "聊天", ["lingxi.db"], "数据库内会话、消息与历史；外部项目原文件不包括"),
        ("memory", "记忆", ["lingxi.db"], "数据库内事实、事件、待办及记忆版本"),
        (
            "characters",
            "人物",
            ["lingxi.db", "characters", "persona.json"],
            "仅数据目录内资源；外链图片、模型需自行迁移",
        ),
        (
            "attachments",
            "附件",
            ["lingxi.db", "attachments", "artifacts", "workspaces"],
            "仅白名单目录内文件；外部工作区不随备份迁移",
        ),
        ("drafts", "草稿", ["lingxi.db"], "仅已同步服务器的草稿；浏览器未提交内容不保证包含"),
        (
            "plugin_configuration",
            "插件配置",
            ["lingxi.db", "skill-assets", PLUGIN_REGISTRY],
            "注册表通过 SQLite 快照保存配置、状态和凭据引用；Key 本体不导出。"
            "恢复时全部停用，清除旧审批，不含插件代码，需另行安装并授权",
        ),
        (
            "plugin_persistent_data",
            "插件持久数据",
            ["plugin-packages/data"],
            "仅真实插件根目录内持久数据；插件程序、staging、任意外部目录"
            "和旧自定义 plugin-data 不导出",
        ),
        (
            "rebuildable_indexes",
            "索引（可重建）",
            ["lingxi.db"],
            "数据库内已有索引一并快照；独立缓存不备份，可从源数据重建",
        ),
    ]
    return {
        "format": "lingxi-data-inventory",
        "version": 1,
        "categories": [
            {
                "id": key,
                "label": label,
                "coverage": "included" if key != "rebuildable_indexes" else "included_rebuildable",
                "paths": paths,
                "limitations": note,
                "present_paths": [name for name in paths if (root / name).exists()],
            }
            for key, label, paths, note in categories
        ],
        "credentials": {
            "coverage": "excluded",
            "secret_values_exported": False,
            "limitation": "系统凭据管理器中的 Key 不读取、不导出；新机器需重新保存 Key",
        },
        "excluded": [
            "program_source",
            "development_backups",
            "migration_backups",
            "logs",
            "temporary_files",
            "external_models",
            "external_workspaces",
        ],
        "plugin_restore_policy": "仅恢复惰性数据；不自动安装、启动、加载或执行归档内容",
        "backup_kinds": {
            "user": "白名单业务数据 ZIP",
            "migration": "schema 升级前回滚快照",
            "source_development": "由开发者另行保存；不属于用户备份",
        },
    }


def lifecycle_status(root=None, program_dir=None):
    from app import config

    root = assert_no_links(_root(root)).resolve()
    program = Path(program_dir or config.BASE_DIR).resolve()
    independent = not root.is_relative_to(program)
    return {
        "data_dir": str(root),
        "program_dir": str(program),
        "data_dir_source": "LINGXI_DATA_DIR"
        if os.environ.get("LINGXI_DATA_DIR")
        else ("APPDATA" if os.environ.get("APPDATA") else "legacy_fallback"),
        "independent_of_program": independent,
        "copy_program_copies_data": not independent,
        "copy_notice": (
            "程序目录与数据目录分离；只复制程序不会带走聊天、人物和附件。请导出用户备份。"
            if independent
            else (
                "当前数据位于程序目录内（兼容路径/自定义路径），"
                "复制此目录可能带走数据；请先确认路径。"
            )
        ),
        "uninstall_reinstall": (
            "卸载/重装应保留显示的数据目录。删除数据必须由用户另行明确执行；本接口不提供卸载器。"
        ),
        "downgrade": (
            "旧程序不得打开更高版本 schema；先导出备份，降级只使用匹配版本的旧快照和新目录。"
        ),
        "installer_complete": False,
        "restart_required_after_import": True,
    }


def _walk_files(directory):
    if not directory.exists():
        return
    assert_no_links(directory)
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs:
            if is_link(Path(base) / name):
                raise ValueError("目录包含符号链接或目录联接，已拒绝扫描")
        for name in files:
            path = Path(base) / name
            if is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("目录包含链接或特殊文件，已拒绝扫描")
            yield path


def storage_overview(root=None, program_dir=None):
    root = assert_no_links(_root(root)).resolve()
    groups = []
    total = 0
    count = 0
    truncated = False
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            entry = {"name": child.name, "bytes": 0, "files": 0, "cleanable": False}
            if is_link(child):
                entry["limitation"] = "链接/目录联接：不跟随、不清理"
            else:
                try:
                    for item in [child] if child.is_file() else _walk_files(child):
                        if count >= 50000:
                            truncated = True
                            break
                        entry["bytes"] += item.stat().st_size
                        entry["files"] += 1
                        count += 1
                    entry["cleanable"] = child.name in {"logs", "tmp", "temp"}
                except OSError, ValueError:
                    entry["limitation"] = "扫描受限；未知数据不清理"
            total += entry["bytes"]
            groups.append(entry)
            if truncated:
                break
    return {
        "lifecycle": lifecycle_status(root, program_dir),
        "inventory": data_inventory(root),
        "groups": groups,
        "bytes": total,
        "files": count,
        "truncated": truncated,
        "cleanup_categories": list(CLEANUP_ROOTS),
        "cleanup_notice": (
            "只清理明确登记的日志、临时文件和可重建缓存；"
            "数据库、附件、人物、插件及未知业务数据一律不删"
        ),
    }


def _cleanable(path, root):
    assert_no_links(path)
    relative = path.relative_to(root)
    if len(relative.parts) < 2:
        raise ValueError("不能清理根目录或根目录文件")
    parts = {v.casefold() for v in relative.parts}
    name = path.name.casefold()
    if parts & _PROTECTED or re.search(r"\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?$", name):
        raise ValueError("不能清理数据库、附件、人物或业务数据")
    if not any(
        relative.is_relative_to(Path(prefix)) and relative != Path(prefix)
        for prefixes in CLEANUP_ROOTS.values()
        for prefix in prefixes
    ):
        raise ValueError("未知路径不允许清理")
    attributes = path.lstat()
    if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
        raise ValueError("只能清理非硬链接的普通文件")
    prefix = relative.parts[0].casefold()
    known = (
        prefix == "logs"
        and bool(re.search(r"\.log(?:\.\d+)?$", name))
        or prefix in ("tmp", "temp")
        and path.suffix.casefold() in (".tmp", ".temp", ".part")
        or relative.is_relative_to(Path("cache/rebuildable"))
        and path.suffix.casefold() in (".cache", ".idx")
        or relative.is_relative_to(Path("cache/thumbnails"))
        and path.suffix.casefold() in (".png", ".jpg", ".jpeg", ".webp")
        or relative.is_relative_to(Path("cache/embeddings"))
        and path.suffix.casefold() in (".cache", ".npy", ".npz")
    )
    if not known:
        raise ValueError("未知文件类型不清理")


def _fingerprint(path, root):
    _cleanable(path, root)
    before = path.stat()
    digest = sha256(path)
    after = path.stat()

    def values(st):
        return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    if values(before) != values(after):
        raise ValueError("文件在扫描期间变化，请重新预览")
    return {
        "path": path.relative_to(root).as_posix(),
        "size": after.st_size,
        "sha256": digest,
        "identity": list(values(after)),
    }


def cleanup_preview(root=None, categories=None):
    root = assert_no_links(_root(root)).resolve()
    categories = list(CLEANUP_ROOTS) if categories is None else categories
    if (
        not isinstance(categories, (list, tuple))
        or not categories
        or any(value not in CLEANUP_ROOTS for value in categories)
    ):
        raise ValueError("仅允许已登记的日志、临时文件、可重建缓存类别")
    files, skipped = [], []
    total = 0
    scanned = 0
    for category in dict.fromkeys(categories):
        for prefix in CLEANUP_ROOTS[category]:
            directory = root / prefix
            if directory.exists() or is_link(directory):
                assert_no_links(directory)
                for path in _walk_files(directory):
                    scanned += 1
                    if scanned > MAX_CLEANUP_SCANNED:
                        raise ValueError("清理扫描超过 5000 个文件限制，请缩小类别")
                    try:
                        fingerprint = _fingerprint(path, root)
                    except ValueError:
                        skipped.append(path.relative_to(root).as_posix())
                        continue
                    total += fingerprint["size"]
                    if len(files) >= MAX_CLEANUP_FILES or total > MAX_CLEANUP_BYTES:
                        raise ValueError("清理超过 1000 个文件或 256 MiB 限制，请缩小类别")
                    files.append(fingerprint)
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _token_lock:
        for key in list(_tokens):
            if _tokens[key]["expires_at"] <= now:
                _tokens.pop(key, None)
        if len(_tokens) >= MAX_TOKENS:
            raise ValueError("清理预览过多，请稍后重试")
        _tokens[token] = {"root": str(root), "expires_at": now + TOKEN_TTL, "files": files}
    return {
        "token": token,
        "expires_at": now + TOKEN_TTL,
        # JSON numbers cannot represent nanosecond stat identities exactly in JS.
        # Token state above keeps original integers for all server-side comparisons.
        "files": [
            {**item, "identity": [str(value) for value in item["identity"]]} for item in files
        ],
        "bytes": total,
        "skipped": skipped,
        "confirmation_required": True,
    }


def cleanup_confirm(token, root=None):
    root = assert_no_links(_root(root)).resolve()
    with _token_lock:
        preview = _tokens.pop(token, None)
    if not preview or preview["expires_at"] <= time.time() or preview["root"] != str(root):
        raise ValueError("清理令牌无效、过期或已使用；请重新预览")
    # Validate EVERY candidate before deleting ANY file; additions are not candidates.
    for expected in preview["files"]:
        path = root / expected["path"]
        try:
            actual = _fingerprint(path, root)
        except (OSError, ValueError) as exc:
            raise ValueError("文件已变化，未执行清理；请重新预览") from exc
        if actual != expected:
            raise ValueError("文件已变化，未执行清理；请重新预览")
    removed, total = [], 0
    for expected in preview["files"]:
        path = root / expected["path"]
        if _fingerprint(path, root) != expected:
            raise ValueError("清理期间文件发生变化，已停止；请重新预览")
        path.unlink()
        removed.append(expected["path"])
        total += expected["size"]
    return {
        "deleted": removed,
        "files": len(removed),
        "bytes": total,
        "business_data_deleted": False,
    }


def controlled_import_directory(root=None):
    root = assert_no_links(_root(root)).resolve()
    directory = root.parent / "imports"
    assert_no_links(directory)
    if directory == root or root.is_relative_to(directory):
        raise ValueError("imports 不能包含正在运行的数据目录")
    return directory


def import_backup(archive_path, root=None, name=None):
    """HTTP-safe wrapper: no client directory path, no running DB overwrite."""
    from app import release

    root = assert_no_links(_root(root)).resolve()
    if (root / "lingxi.db").exists():
        assert_idle(root / "lingxi.db")
    if name is None:
        name = "restored-" + secrets.token_hex(12)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        raise ValueError("导入名称仅允许字母、数字、下划线和连字符；不能指定路径")
    imports = controlled_import_directory(root)
    destination = imports / name
    result = release.restore_backup(archive_path, destination)
    return {
        **result,
        "active_data_dir": str(root),
        "activated": False,
        "restart_required": True,
        "steps": [
            "停止零汐及全部任务",
            f"离线设置 LINGXI_DATA_DIR={destination}",
            "重新启动零汐并核对聊天、人物、附件和记忆",
            "在新机器重新保存 Key；原数据目录保留不变",
        ],
        "notice": "只恢复到受控 imports 新目录；未修改正式环境、未自动切换数据、未启动插件",
    }
