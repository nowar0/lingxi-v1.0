"""Bounded package reader: no extraction by zipfile, no code execution.

The rejection rules are deliberately portable to Windows. A valid hash proves file
integrity only, not publisher identity. This is not an OS-level file sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from app.plugin_manifest import ManifestError, canonical_json, validate_manifest, validate_relpath


@dataclass(frozen=True)
class Limits:
    max_files: int = 1000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    max_archive_bytes: int = 40 * 1024 * 1024
    max_manifest_bytes: int = 256 * 1024
    max_ratio: int = 100


@dataclass
class Package:
    source: str
    manifest: dict
    blobs: dict[str, bytes]
    digest: str
    total_bytes: int

    def write_to(self, directory: Path, *, reuse_from: Path|None=None, reuse_hashes: dict|None=None) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        # Paths were validated before any write; only new regular files are created.
        for name, blob in self.blobs.items():
            if reuse_from is not None and name in (reuse_hashes or {}):
                candidate=reuse_from.joinpath(*name.split('/'))
                try:
                    _not_link(candidate)
                    cached=candidate.read_bytes()
                    if len(cached)==len(blob) and hashlib.sha256(cached).hexdigest()==reuse_hashes[name]:blob=cached
                except (OSError,ManifestError):pass
            target = directory.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(blob)
        target = directory / "manifest.json"
        with target.open("xb") as stream:
            stream.write((canonical_json(self.manifest) + "\n").encode("utf-8"))


_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)


def safe_name(name: str) -> str:
    path = validate_relpath(name)
    if "\\" in name or len(path) > 240:
        raise ManifestError("包路径过长或使用反斜杠")
    for segment in path.split("/"):
        if segment.endswith((" ", ".")) or _RESERVED.fullmatch(segment):
            raise ManifestError("包路径含 Windows 保留名或尾随点/空格")
        if any(ord(ch) < 32 or ch in '<>:"|?*' for ch in segment):
            raise ManifestError("包路径包含平台非法字符")
    return path


def _not_link(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ManifestError("包目录不能包含链接/reparse point")
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise ManifestError("包文件不能是硬链接")
    return info


def _read_limited(path: Path, maximum: int) -> bytes:
    before = _not_link(path)
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ManifestError("文件不是常规文件或大小超限")
    # O_NOFOLLOW closes the common symlink race on POSIX; Windows reparse points
    # are checked before and after. Concurrent local filesystem attackers remain
    # outside the trust boundary, so only user-owned package roots are supported.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ManifestError("扫描时文件已替换")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ManifestError("文件大小超限")
        _not_link(path)
        return data
    finally:
        os.close(fd)


def _unique(names: dict[str, str], name: str) -> None:
    folded = name.casefold()
    if folded in names:
        raise ManifestError("包路径重复或大小写冲突")
    # A file cannot be an ancestor of a second member, even if differently cased.
    for old in names:
        if old.startswith(folded + "/") or folded.startswith(old + "/"):
            raise ManifestError("包路径出现文件/目录冲突")
    names[folded] = name


def scan_package(source: str | Path, limits: Limits | None = None) -> Package:
    limits = limits or Limits()
    path = Path(source).absolute()
    for ancestor in (*reversed(path.parents), path):
        _not_link(ancestor)
    blobs: dict[str, bytes] = {}
    names: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    total = 0

    def check_prefixes(name: str):
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            previous = prefixes.get(prefix.casefold())
            if previous is not None and previous != prefix:
                raise ManifestError("包路径段存在大小写冲突")
            prefixes[prefix.casefold()] = prefix

    def add(name: str, value: bytes):
        nonlocal total
        safe_name(name)
        if name.endswith((".pyc", ".pyo")) or "__pycache__" in name.split("/"):
            raise ManifestError("包必须提供源文件，不能包含 Python 字节码缓存")
        check_prefixes(name)
        _unique(names, name)
        if len(blobs) + 1 > limits.max_files or len(value) > limits.max_file_bytes:
            raise ManifestError("插件文件数量或单文件大小超限")
        total += len(value)
        if total > limits.max_total_bytes:
            raise ManifestError("插件解压总大小超限")
        blobs[name] = value

    if path.is_dir():
        directory_names: set[str] = set()
        def walk_error(error):
            raise error
        for root, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
            if len(directory_names) + len(dirs) > limits.max_files:
                raise ManifestError("包目录数量超限")
            for child in dirs:
                candidate = Path(root) / child
                _not_link(candidate)
                relative = safe_name(candidate.relative_to(path).as_posix())
                check_prefixes(relative)
                if relative.casefold() in directory_names:
                    raise ManifestError("目录大小写冲突")
                directory_names.add(relative.casefold())
            for child in files:
                candidate = Path(root) / child
                name = safe_name(candidate.relative_to(path).as_posix())
                add(name, _read_limited(candidate, limits.max_file_bytes))
    elif path.is_file() and path.suffix.lower() == ".zip":
        raw = _read_limited(path, limits.max_archive_bytes)
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > limits.max_files * 2:
                    raise ManifestError("压缩包成员数量超限")
                directory_names = set()
                for info in infos:
                    name = safe_name(info.filename.rstrip("/"))
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                        raise ManifestError("压缩包不能包含链接或特殊文件")
                    if info.flag_bits & 1:
                        raise ManifestError("不支持加密压缩包")
                    if info.is_dir():
                        check_prefixes(name)
                        if name.casefold() in directory_names:
                            raise ManifestError("压缩包目录重复")
                        directory_names.add(name.casefold())
                        continue
                    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise ManifestError("不支持该压缩算法")
                    if info.file_size > limits.max_file_bytes:
                        raise ManifestError("压缩包单文件大小超限")
                    if info.file_size > max(info.compress_size, 1) * limits.max_ratio:
                        raise ManifestError("疑似压缩炸弹：压缩比过高")
                    with archive.open(info, "r") as stream:
                        value = stream.read(limits.max_file_bytes + 1)
                    if len(value) != info.file_size:
                        raise ManifestError("压缩包文件大小与头部不一致")
                    add(name, value)
                for directory in directory_names:
                    if directory in names:
                        raise ManifestError("压缩包文件/目录冲突")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise ManifestError("损坏或不支持的 zip") from exc
    else:
        raise ManifestError("只支持本地目录或 .zip 文件")
    manifest_raw = blobs.pop("manifest.json", None)
    if manifest_raw is None or len(manifest_raw) > limits.max_manifest_bytes:
        raise ManifestError("缺少或过大的根 manifest.json")
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (UnicodeError, ValueError) as exc:
        raise ManifestError("manifest.json 不是严格 UTF-8 JSON") from exc
    validate_manifest(manifest, file_names=set(blobs))
    for name, blob in blobs.items():
        if hashlib.sha256(blob).hexdigest() != manifest["files"][name]:
            raise ManifestError(f"SHA-256 不匹配: {name}")
    digest = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    return Package(str(path), manifest, blobs, digest, total)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains duplicate keys")
        result[key] = value
    return result
