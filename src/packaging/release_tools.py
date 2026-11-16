"""离线发布工程工具；不导入业务代码、不启动应用、不读取用户数据。

版本唯一源：src/pyproject.toml 的 [project].version。
manifest 是完整性清单而不是签名；分发时须经可信渠道核对外置 ZIP SHA256。
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import tomllib
import uuid
import zipfile

MANIFEST_NAME = "release-manifest.json"
SCHEMA = "lingxi.release.v1"
VERSION_SOURCE = "src/pyproject.toml"
EXCLUDED_DIRS = {"__pycache__", ".venv", "node_modules", "_build", ".build-temp", ".ruff_cache", ".pytest_cache", ".git"}
GENERATED_SOURCE_PATHS = {("frontend", "dist"), ("workbench", "dist")}
# Observed in this development round; match only root test-profile families, not
# arbitrary business assets called lingxi-* anywhere in the tree.
TEMP_SOURCE_PREFIXES = ("lingxi-import-check-", "lingxi-plugin-runtime-", "lingxi-plugin-test-",
                        "lingxi-plugin-api-", "lingxi-plugin-security-", "package-mcp-")
OLD_RELEASE_RECORDS = {MANIFEST_NAME, "发布文件清单.json", "release-files.json", "checksums.json"}
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm")
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
SHA_RE = re.compile(r"[a-f0-9]{64}\Z")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_from(source: Path) -> str:
    version = tomllib.loads(source.read_text(encoding="utf-8-sig"))["project"]["version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("project.version 必须为规范三段数字版本，例如 0.4.0")
    if any(int(item) > 65535 for item in version.split(".")):
        raise ValueError("版本数字超出 Windows PE 版本范围")
    return version


def app_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"找不到 VERSION：{path}")


def sync_versions(source: Path) -> str:
    """只写授权元数据；依赖版本/运行时/Live2D 资源名均不变。"""
    version = version_from(source / "pyproject.toml")
    targets = [
        source / "frontend/package.json", source / "packaging/shell/package.json",
        source / "packaging/package.json", (source.parent / "resources/app/package.json" if (source.parent / "resources/app/package.json").is_file() else source.parent / "app/resources/app/package.json"),
    ]
    for path in targets:
        value = load_json(path)  # 必须先读现存文件再修改。
        value["version"] = version
        write_json(path, value)
    lock_path = source / "frontend/package-lock.json"
    lock = load_json(lock_path)
    lock["version"] = version
    lock["packages"][""]["version"] = version
    write_json(lock_path, lock)
    tool_lock = source / "packaging/package-lock.json"
    if tool_lock.exists():
        value = load_json(tool_lock)
        value["version"] = version
        value["packages"][""]["version"] = version
        write_json(tool_lock, value)
    version_path = source / "app/version.py"
    text = version_path.read_text(encoding="utf-8-sig")
    updated, count = re.subn(r"(?m)^VERSION\s*=\s*[^\n]+$", f'VERSION = "{version}"', text)
    if count != 1:
        raise ValueError("app/version.py 必须恰有一个 VERSION 字面量")
    version_path.write_text(updated, encoding="utf-8")
    uv_path = source / "uv.lock"
    text = uv_path.read_text(encoding="utf-8-sig")
    pattern = r'(\[\[package\]\]\s*\nname = "lingxi-app"\s*\nversion = ")[^"]+(")'
    updated, count = re.subn(pattern, lambda match: match[1] + version + match[2], text)
    if count != 1:
        raise ValueError("uv.lock 必须恰有一个 lingxi-app 版本")
    uv_path.write_text(updated, encoding="utf-8")
    info_path = source.parent / "version-info.json"
    info = load_json(info_path)
    info["版本"] = version
    write_json(info_path, info)
    return check_versions(source)


def check_versions(source: Path) -> str:
    """检查派生元数据，Electron 与 Live2D 资源版本不在应用版本同步范围内。"""
    version = version_from(source / "pyproject.toml")
    root = source.parent
    values = {
        "app/version.py:VERSION": app_version(source / "app/version.py"),
        "frontend/package.json": load_json(source / "frontend/package.json")["version"],
        "frontend/package-lock.json": load_json(source / "frontend/package-lock.json")["version"],
        "frontend/package-lock.json:packages['']": load_json(source / "frontend/package-lock.json")["packages"][""]["version"],
        "packaging/shell/package.json": load_json(source / "packaging/shell/package.json")["version"],
        "packaging/package.json": load_json(source / "packaging/package.json")["version"],
        "resources/app/package.json": load_json(root / ("resources/app/package.json" if (root / "resources/app/package.json").is_file() else "app/resources/app/package.json"))["version"],
        "version-info.json": load_json(root / "version-info.json")["版本"],
    }
    lock = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8-sig"))
    app_packages = [item for item in lock["package"] if item["name"] == "lingxi-app"]
    if len(app_packages) != 1:
        raise ValueError("uv.lock 必须恰有一个 lingxi-app 条目")
    values["uv.lock:lingxi-app"] = app_packages[0]["version"]
    errors = [f"{name}={value!r}" for name, value in values.items() if value != version]
    if errors:
        raise ValueError("派生版本与 project.version 不一致：" + "; ".join(errors))
    return version


def contained(path: Path, parent: Path) -> Path:
    """严格只允许 parent 下的子路径，不允许清理根本身或跳转链接。"""
    parent = parent.resolve()
    absolute = Path(os.path.abspath(path))
    if absolute == parent or not absolute.is_relative_to(parent):
        raise ValueError(f"目标必须在受控目录内且不能是根本身：{absolute}")
    current = absolute
    while current != parent:
        if current.is_symlink() or (current.exists() and current.is_junction()):
            raise ValueError(f"禁止符号链接或 junction：{current}")
        current = current.parent
    resolved = absolute.resolve()
    if not resolved.is_relative_to(parent):
        raise ValueError(f"路径越界：{resolved}")
    return resolved


def clean_staging(build: Path, staging: Path) -> None:
    target = contained(staging, build)
    if target.relative_to(build.resolve()).parts[0] != "staging":
        raise ValueError("仅允许清理 _build/staging 及其子目录，不允许清理 releases/cache")
    # 调用方给出并验证完整绝对目标后才进行删除；仅用于本次构建可重建目录。
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"staging 不是目录：{target}")
        shutil.rmtree(target)
    target.mkdir(parents=True)


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


@contextmanager
def temporary_workspace(parent: Path, prefix: str):
    """在显式受控父目录建唯一临时目录；兼容 Windows 3.14 的隔离令牌。

    不使用 tempfile 的 mode=0700 特殊 Windows ACL，也不修改任何文件权限。
    """
    parent.mkdir(parents=True, exist_ok=True)
    parent = parent.resolve()
    target = parent / (prefix + uuid.uuid4().hex)
    target.mkdir()  # 默认 mode，沿用授权工作树的目录权限。
    try:
        yield target
    finally:
        checked = contained(target, parent)
        if checked.name.startswith(prefix) and checked.exists():
            shutil.rmtree(checked)


def _is_database_file(path: Path) -> bool:
    return path.name.casefold().endswith(DATABASE_SUFFIXES)


def _source_excluded(path: Path, source_root: Path) -> bool:
    rel = path.relative_to(source_root)
    parts = tuple(rel.parts)
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if len(parts) >= 2 and parts[:2] in GENERATED_SOURCE_PATHS:
        return True
    # Download caches/quarantined third-party media artifacts are never a source
    # or runtime deliverable. Only the audited media-tools bundle can be released.
    if len(parts)>=2 and parts[:2]==('packaging','vendor-media'):
        return True
    # Installer synthetic tests may leave locked fixtures only under tools; keep
    # the developer evidence but never package their synthetic SQLite databases.
    if len(parts) >= 2 and parts[0] == 'packaging' and parts[1].startswith(('install-transaction-', 'install-review-contract-')):
        return True
    # Temporary validation profiles are excluded only at the source root.
    if len(parts) >= 1 and parts[0].startswith(TEMP_SOURCE_PREFIXES):
        return True
    return False


def copy_tree(source: Path, destination: Path, *, source_code: bool = False, _source_root: Path | None = None) -> None:
    """Copy ordinary files into staging, excluding only explicit rebuild outputs.

    Business assets and unknown static resources remain publishable. Source-side
    database files are rejected rather than silently copied into a release.
    """
    if is_link(source) or not source.is_dir():
        raise ValueError(f"来源不是普通目录：{source}")
    source = source.resolve()
    _source_root = _source_root or source
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
        if source_code and _source_excluded(item, _source_root):
            continue
        if is_link(item):
            raise ValueError(f"发布来源含链接：{item}")
        if item.is_dir():
            copy_tree(item, destination / item.name, source_code=source_code, _source_root=_source_root)
        elif item.is_file():
            if source_code and (item.suffix.casefold() in {".pyc", ".pyo", ".tsbuildinfo"} or _is_database_file(item)):
                if _is_database_file(item):
                    raise ValueError(f"源码包含数据库文件，拒绝发布：{item}")
                continue
            shutil.copy2(item, destination / item.name)
        else:
            raise ValueError(f"发布来源含特殊文件：{item}")


def source_preflight(source: Path) -> dict:
    """Inspect source payload before staging writes; never follows test/profile links."""
    if is_link(source):
        raise ValueError(f'源码根不能是链接：{source}')
    source = source.resolve(strict=True)
    excluded, databases = [], []
    for folder, dirs, files in os.walk(source, followlinks=False):
        for name in list(dirs):
            path = Path(folder) / name
            if _source_excluded(path, source):
                excluded.append(path.relative_to(source).as_posix())
                dirs.remove(name)
            elif is_link(path):
                raise ValueError(f"源码包含链接：{path}")
        for name in files:
            path = Path(folder) / name
            if is_link(path):
                raise ValueError(f"源码包含链接：{path}")
            if path.suffix.casefold() in {".pyc", ".pyo", ".tsbuildinfo"}:
                excluded.append(path.relative_to(source).as_posix())
                continue
            if _is_database_file(path):
                databases.append(path.relative_to(source).as_posix())
            else:
                # Also reject renamed SQLite stores without a database extension.
                with path.open('rb') as stream:
                    if stream.read(16) == b'SQLite format 3\x00':
                        databases.append(path.relative_to(source).as_posix())
    if databases:
        raise ValueError("源码包含数据库，拒绝发布；请由负责人核查（本工具不删除）：" + ", ".join(sorted(databases)))
    return {"source": str(source), "excluded": sorted(excluded), "databases": []}


def _ordinary_hashes(root: Path) -> dict[str, str]:
    if is_link(root) or not root.is_dir():
        raise ValueError(f"静态构建目录缺失或是链接：{root}")
    values, seen = {}, set()
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            if is_link(path):
                raise ValueError(f"静态目录含链接：{path}")
        for name in files:
            path = Path(folder) / name
            rel = safe_relative(path.relative_to(root).as_posix())
            if rel.casefold() in seen:
                raise ValueError(f"静态目录大小写冲突：{rel}")
            seen.add(rel.casefold())
            values[rel] = sha256(path)
    return values


def check_static(source: Path, backend: Path | None = None) -> dict:
    """Every dist file must already be synced; extra legacy static is retained.

    Workbench currently writes directly to static. An optional workbench/dist,
    when present, is interpreted relative to its workbench static output root.
    Frozen backend static must exactly match the complete source static tree.
    """
    source = source.resolve(strict=True)
    static_root = source / 'app/static/ui'
    static = _ordinary_hashes(static_root)
    dist = _ordinary_hashes(source / 'frontend/dist')
    if 'index.html' not in dist or not dist:
        raise ValueError('主前端 dist 缺少 index.html，请先生产构建并同步静态资源')
    mismatch = [name for name, digest in dist.items() if static.get(name) != digest]
    if mismatch:
        raise ValueError('dist 未同步到 app/static/ui：' + ', '.join(mismatch[:20]))
    workbench_source = source / 'workbench'
    if (workbench_source / 'package.json').is_file():
        required = ['workbench-tools.html', 'assets/workbench/workbench.js', 'assets/workbench/workbench.css']
        missing = [name for name in required if not (static_root / name).is_file() or not (static_root / name).stat().st_size]
        if missing:
            raise ValueError('工作工具页面或资源缺失：' + ', '.join(missing))
        receipt_path = static_root / 'assets/workbench/workbench-build.json'
        if not receipt_path.is_file():
            raise ValueError('工作工具缺少构建记录，请重新构建 workbench')
        receipt = load_json(receipt_path)
        if receipt.get('schema') != 'lingxi.workbench-build.v1' or not receipt.get('inputs') or not set(required).issubset(receipt.get('outputs', {})):
            raise ValueError('工作工具构建记录无效')
        for base, records in [(workbench_source, receipt['inputs']), (static_root, receipt['outputs'])]:
            for name, expected in records.items():
                path = base / name
                if (not isinstance(name, str) or '\\' in name or ':' in name or Path(name).is_absolute()
                        or '..' in Path(name).parts or not path.resolve().is_relative_to(base.resolve())
                        or not path.is_file() or sha256(path) != expected):
                    raise ValueError('工作工具源码或产物已变化，请重新构建：' + str(name))
    workbench_dist = source / 'workbench/dist'
    workbench_mode = 'direct_static_no_independent_dist'
    if workbench_dist.exists():
        built = _ordinary_hashes(workbench_dist)
        if not built:
            raise ValueError('工作台 dist 为空')
        destination = _ordinary_hashes(static_root / 'assets/workbench')
        mismatch = [name for name, digest in built.items() if destination.get(name) != digest]
        if mismatch:
            raise ValueError('工作台 dist 未同步到 static/assets/workbench：' + ', '.join(mismatch[:20]))
        workbench_mode = 'dist_synced'
    if backend is not None:
        frozen = _ordinary_hashes(backend.resolve(strict=True) / '_internal/app/static/ui')
        if frozen != static:
            raise ValueError('冻结后端静态资源与完整源码静态目录不一致；请重新冻结，保留 legacy 资源')
    return {"dist_files": len(dist), "static_files": len(static),
            "legacy_or_extra_static_files_retained": len(set(static) - set(dist)),
            "workbench": workbench_mode, "backend_checked": backend is not None}


def copy_release_inputs(root: Path, destination: Path) -> list[str]:
    """复制所有根文档/JSON/BAT；现有插件目录完整复制，缺失不冒充已发布插件。"""
    copied = []
    for item in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if item.is_file() and item.suffix.lower() in {".md", ".json", ".bat"}:
            if item.name in OLD_RELEASE_RECORDS or item.name == "vk_swiftshader_icd.json" or item.name.endswith(".release.json"):
                # Vulkan ICD is the selected Electron runtime asset; old manifests,
                # ZIP records and hash lists must be regenerated, never copied.
                continue
            if is_link(item):
                raise ValueError(f"根文档含链接：{item}")
            shutil.copy2(item, destination / item.name)
            copied.append(item.name)
    plugins = root / "插件"
    if plugins.exists():
        copy_tree(plugins, destination / "插件")
        copied.append("插件/")
    return copied


def safe_relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError(f"不安全的发布路径：{name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise ValueError(f"不安全的发布路径：{name!r}")
    # 同时拒绝 Windows 设备路径、ADS、末尾点/空格，保证 Windows 解压语义唯一。
    for part in pure.parts:
        if part.endswith((".", " ")) or re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part):
            raise ValueError(f"不安全的 Windows 路径：{name!r}")
    return pure.as_posix()


def file_inventory(root: Path) -> list[dict]:
    entries = []
    seen = set()
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            if is_link(path):
                raise ValueError(f"发布目录含链接：{path}")
        for name in files:
            path = Path(folder) / name
            rel = safe_relative(path.relative_to(root).as_posix())
            if rel == MANIFEST_NAME:
                continue
            if rel.casefold() in seen:
                raise ValueError(f"大小写冲突：{rel}")
            seen.add(rel.casefold())
            if not path.is_file():
                raise ValueError(f"发布目录含特殊文件：{path}")
            entries.append({"path": rel, "size": path.stat().st_size, "sha256": sha256(path)})
    return sorted(entries, key=lambda item: item["path"])


def utc_now() -> str:
    # 实际构建时间；未把历史文档日期充当本次构建日期。
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def data_schema_from(source: Path) -> dict | None:
    """Read explicit compatibility, never infer SQLite schema from app version."""
    value = tomllib.loads(source.read_text(encoding='utf-8-sig')).get('tool', {}).get('lingxi', {}).get('data-schema')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'min', 'max', 'target'}:
        raise ValueError('data-schema 必须明确声明 min/max/target')
    if any(type(value[key]) is not int for key in value) or not 0 <= value['min'] <= value['target'] <= value['max']:
        raise ValueError('data-schema 兼容范围无效')
    return dict(value)


def create_manifest(root: Path, electron: str, *, required: list[str] | None = None) -> dict:
    if (root / MANIFEST_NAME).exists():
        raise ValueError("manifest 已存在，拒绝覆盖旧发布记录")
    source = root / VERSION_SOURCE
    version = version_from(source)
    files = file_inventory(root)
    paths = {item["path"] for item in files}
    missing = sorted(set(required or []) - paths)
    if missing:
        raise ValueError("发布缺少必需文件：" + ", ".join(missing))
    manifest = {
        "schema": SCHEMA,
        "product": "Lingxi",
        "version": version,
        "built_at_utc": utc_now(),
        "version_source": {"path": VERSION_SOURCE, "field": "project.version", "sha256": sha256(source)},
        "runtimes": {"electron": electron},
        "integrity": "SHA256; not a digital signature",
        "files": files,
    }
    schema_compatibility = data_schema_from(source)
    if schema_compatibility is not None:
        manifest['data_schema'] = schema_compatibility
    write_json(root / MANIFEST_NAME, manifest)
    verify_manifest(root)
    return manifest


def verify_manifest(root: Path) -> dict:
    manifest = load_json(root / MANIFEST_NAME)
    if manifest.get("schema") != SCHEMA or not VERSION_RE.fullmatch(manifest.get("version", "")):
        raise ValueError("未知清单格式或应用版本")
    built = datetime.fromisoformat(manifest["built_at_utc"].replace("Z", "+00:00"))
    if built.tzinfo != timezone.utc:
        raise ValueError("构建时间必须为 UTC")
    source = manifest.get("version_source", {})
    if source.get("path") != VERSION_SOURCE or source.get("field") != "project.version":
        raise ValueError("不可信版本来源字段")
    if sha256(root / VERSION_SOURCE) != source.get("sha256") or version_from(root / VERSION_SOURCE) != manifest["version"]:
        raise ValueError("版本来源哈希或版本不符")
    declared_schema = data_schema_from(root / VERSION_SOURCE)
    if manifest.get('data_schema') != declared_schema:
        raise ValueError('发布清单数据 schema 兼容范围与规范源不一致')
    expected = manifest.get("files")
    if not isinstance(expected, list) or not expected:
        raise ValueError("发布清单不能为空")
    seen = set()
    for item in expected:
        rel = safe_relative(item["path"])
        if rel == MANIFEST_NAME or rel.casefold() in seen:
            raise ValueError("重复路径或清单自引用")
        seen.add(rel.casefold())
        if type(item.get("size")) is not int or item["size"] < 0 or not SHA_RE.fullmatch(item.get("sha256", "")):
            raise ValueError(f"清单文件记录无效：{rel}")
    if sorted(expected, key=lambda item: item["path"]) != file_inventory(root):
        raise ValueError("发布目录文件集合/大小/SHA256 与 manifest 不一致")
    return manifest


def make_archive(root: Path, archive: Path) -> Path:
    manifest = verify_manifest(root)
    sidecar = Path(str(archive) + ".release.json")
    if archive.exists() or sidecar.exists():
        raise ValueError("ZIP 或外置记录已存在；使用新输出名，禁止删除旧整包 ZIP")
    if archive.resolve().is_relative_to(root.resolve()):
        raise ValueError("ZIP 不能写入待压缩目录内部")
    archive.parent.mkdir(parents=True, exist_ok=True)
    # 不预先删除旧输出；同目录临时文件仅属于本次构建。
    fd, temp = tempfile.mkstemp(prefix=".lingxi-zip-", suffix=".tmp", dir=archive.parent)
    os.close(fd)
    temp_path = Path(temp)
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
            for entry in manifest["files"] + [{"path": MANIFEST_NAME}]:
                rel = entry["path"]
                zip_file.write(root / rel, f"{root.name}/{rel}")
        # 再验证输入，避免压缩途中被修改而记录失真。
        verify_manifest(root)
        record = {
            "schema": "lingxi.archive.v1", "version": manifest["version"],
            "built_at_utc": manifest["built_at_utc"], "archive": archive.name,
            "size": temp_path.stat().st_size, "sha256": sha256(temp_path),
            "manifest_sha256": sha256(root / MANIFEST_NAME),
            "version_source": manifest["version_source"],
            "notice": "Integrity only, not a signature. Obtain this record over a trusted channel.",
        }
        # Windows 上 rename 不覆盖已存在文件；其他平台也先用独占创建保留输出名。
        with archive.open("xb") as target, temp_path.open("rb") as data:
            shutil.copyfileobj(data, target)
        with sidecar.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return sidecar
    finally:
        # 明确验证本次创建的临时文件后才删除，不触碰旧整包 ZIP。
        if temp_path.parent.resolve() == archive.parent.resolve() and temp_path.name.startswith(".lingxi-zip-"):
            temp_path.unlink(missing_ok=True)


def verify_archive(archive: Path, record_path: Path, *, temp_dir: Path | None = None) -> dict:
    record = load_json(record_path)
    if record.get("schema") != "lingxi.archive.v1" or record.get("archive") != archive.name:
        raise ValueError("ZIP 外置记录格式或文件名不匹配")
    if not SHA_RE.fullmatch(record.get("sha256", "")) or sha256(archive) != record["sha256"] or archive.stat().st_size != record.get("size"):
        raise ValueError("ZIP SHA256 或大小不匹配")
    # 默认临时解压也在工具受控 _build，避免写入正式安装或用户数据目录。
    temp_dir = temp_dir or Path(__file__).resolve().parent / "_build" / "verification"
    temp_dir.mkdir(parents=True, exist_ok=True)
    if is_link(temp_dir):
        raise ValueError("校验临时目录不能为链接")
    with temporary_workspace(temp_dir, "lingxi-release-verify-") as temp_root:
        with zipfile.ZipFile(archive) as zip_file:
            seen = set()
            roots = set()
            total = 0
            for item in zip_file.infolist():
                rel = safe_relative(item.filename.rstrip("/"))
                if rel.casefold() in seen:
                    raise ValueError("ZIP 含重复或大小写冲突路径")
                seen.add(rel.casefold())
                roots.add(rel.split("/")[0])
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or item.flag_bits & 1:
                    raise ValueError("ZIP 不允许链接或加密项")
                total += item.file_size
                if total > 16 * 1024 ** 3:
                    raise ValueError("ZIP 解压总大小超出 16 GiB 安全上限")
            if len(roots) != 1:
                raise ValueError("ZIP 必须包含唯一顶级发布目录")
            zip_file.extractall(temp_root)
        package = temp_root / roots.pop()
        if sha256(package / MANIFEST_NAME) != record.get("manifest_sha256"):
            raise ValueError("ZIP 内 manifest SHA256 不匹配")
        manifest = verify_manifest(package)
        for field in ("version", "built_at_utc", "version_source"):
            if record.get(field) != manifest.get(field):
                raise ValueError(f"ZIP 外置记录与清单 {field} 不一致")
    return record


def assemble(source: Path, stage: Path, electron: Path, backend: Path, build: Path) -> None:
    """仅操作受控 staging；不修改现有运行目录或删除任何未知 static。"""
    source = source.resolve(strict=True)
    stage = contained(stage, build)
    if stage.exists():
        raise ValueError(f'组装目标已存在，禁止覆盖：{stage}')
    check_versions(source)
    source_preflight(source)
    check_static(source, backend)
    clean_staging(build, stage)
    copy_tree(electron, stage)
    default_app = contained(stage / "resources/default_app.asar", build)
    if default_app.exists():
        default_app.unlink()
    copy_tree(source / "packaging/shell", stage / "resources/app")
    copy_tree(backend, stage / "resources/backend")
    copy_release_inputs(source.parent, stage)
    copy_tree(source, stage / "src", source_code=True)
    old = contained(stage / "electron.exe", build)
    new = contained(stage / "Lingxi.exe", build)
    if not old.is_file() or new.exists():
        raise ValueError("Electron 来源缺少 electron.exe 或存在冲突的Lingxi.exe")
    old.rename(new)
    # 根版本信息的旧构建日期不能冒充本轮时间；仅在新 staging 派生实际构建字段。
    info_path = stage / "version-info.json"
    info = load_json(info_path)
    if info.get("构建日期"):
        info["历史整包构建日期"] = info["构建日期"]
    info["构建日期"] = utc_now()
    info["版本来源"] = VERSION_SOURCE + ":project.version"
    info["发布工程状态"] = "本轮构建产物；业务运行未由packaging验收"
    write_json(info_path, info)
    icon = source / "packaging/icon/lingxi.ico"
    shutil.copy2(icon, stage / "resources/app/assets/lingxi.ico")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    sub.add_parser("check-version")
    sub.add_parser("sync-version")
    sub.add_parser("preflight")
    p = sub.add_parser("check-static")
    p.add_argument('--backend', type=Path)
    p = sub.add_parser("assemble")
    for name in ("stage", "electron", "backend", "build"):
        p.add_argument("--" + name, type=Path, required=True)
    p = sub.add_parser("manifest")
    p.add_argument("root", type=Path)
    p.add_argument("--electron-version", required=True)
    p = sub.add_parser("verify")
    p.add_argument("root", type=Path)
    p = sub.add_parser("archive")
    p.add_argument("root", type=Path)
    p.add_argument("zip", type=Path)
    p = sub.add_parser("verify-archive")
    p.add_argument("zip", type=Path)
    p.add_argument("record", type=Path)
    args = parser.parse_args(argv)
    args.source = args.source.resolve()
    if args.command == "version":
        print(version_from(args.source / "pyproject.toml"))
    elif args.command == "check-version":
        print(check_versions(args.source))
    elif args.command == "sync-version":
        print(sync_versions(args.source))
    elif args.command == "preflight":
        print(json.dumps(source_preflight(args.source), ensure_ascii=False, indent=2))
    elif args.command == "check-static":
        print(json.dumps(check_static(args.source, args.backend), ensure_ascii=False, indent=2))
    elif args.command == "assemble":
        assemble(args.source.resolve(), args.stage, args.electron, args.backend, args.build)
        print(f"组装完成：{args.stage}")
    elif args.command == "manifest":
        manifest = create_manifest(args.root, args.electron_version, required=["Lingxi.exe", "resources/app/package.json", "resources/backend/LingxiServer.exe", "UserGuide.html", "version-info.json", VERSION_SOURCE])
        print(f"manifest 验证通过：{manifest['version']} {manifest['built_at_utc']}")
    elif args.command == "verify":
        manifest = verify_manifest(args.root)
        print(f"发布文件完整性验证通过：{len(manifest['files'])} 文件")
    elif args.command == "archive":
        print(make_archive(args.root, args.zip))
    elif args.command == "verify-archive":
        record = verify_archive(args.zip, args.record)
        print(f"ZIP 与内置 manifest 验证通过：{record['version']} {record['sha256']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"发布工具失败：{error}", file=sys.stderr)
        raise SystemExit(1)
