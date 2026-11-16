"""Pinned local media provider, safe to import before dependencies are installed.

Only explicit calls perform stdlib metadata/stat/SHA reads.  There are no
network calls, installers, subprocesses, decoder imports, native loads, PATH
searches or environment overrides.  The host owns limits and decoder execution.
A frozen build uses only its own media-tools bundle, never a source-tree venv.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath

EXPECTED_MANIFEST_SHA256 = "2b205b5a1a07263ee34e87edf74b40aca7fe1d3a2990c5380dc377ef39884513"
PYTHON_DEPENDENCY_PINS = {"pillow": "13.0.0", "pypdf": "6.21.4"}


class MediaDependencyUnavailable(RuntimeError):
    """Missing, changed, unsupported, or not redistribution-approved dependency."""


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _plain_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for item in (absolute, *absolute.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise MediaDependencyUnavailable("linked media dependency path refused")
    return absolute


def _relative(value: str) -> PurePosixPath:
    result = PurePosixPath(value)
    if (not value or "\\" in value or ":" in value or result.is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise MediaDependencyUnavailable("invalid dependency manifest path")
    return result


def _layout() -> tuple[Path, Path, bool]:
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        # PyInstaller onedir exposes _internal via _MEIPASS.  No cwd/PATH fallback.
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "media-tools"
        return root / "media-dependencies.json", root, True
    root = Path(__file__).resolve().parents[1] / "packaging"
    return root / "media-dependencies.json", root / "vendor-media", False


def _manifest() -> tuple[dict, Path, bool]:
    path, root, frozen = _layout()
    path = _plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 512_000:
        raise MediaDependencyUnavailable("invalid media dependency manifest")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED_MANIFEST_SHA256:
        raise MediaDependencyUnavailable("media dependency manifest differs from reviewed pin")
    manifest = json.loads(data)
    if manifest.get("schema_version") != 1:
        raise MediaDependencyUnavailable("unsupported media dependency manifest")
    return manifest, root, frozen


def _verify(path: Path, record: dict) -> None:
    path = _plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != record["size"]:
        raise MediaDependencyUnavailable("media dependency file size/type mismatch")
    if _file_sha256(path) != record["sha256"]:
        raise MediaDependencyUnavailable("media dependency file SHA-256 mismatch")


def _python_status(package: dict, root: Path, frozen: bool) -> dict:
    name = package["name"]
    result = {
        "name": name, "expected_version": package["version"], "installed_version": None,
        "available": False, "license": package["license_expression"],
        "verification": "metadata_and_selected_native_sha256", "native_executed": False,
        "native_files": [], "diagnostic": "",
    }
    try:
        if PYTHON_DEPENDENCY_PINS.get(name) != package["version"]:
            raise MediaDependencyUnavailable("unrecognized dependency pin")
        license_record = package["license_file"]
        license_path = (root / "licenses" / PurePosixPath(license_record["path"]).name
                        if frozen else root / _relative(license_record["path"]))
        _verify(license_path, license_record)
        distribution = metadata.distribution(name)
        result["installed_version"] = distribution.version
        if distribution.version != package["version"]:
            raise MediaDependencyUnavailable("installed dependency version differs from pin")
        # Preserve original metadata bytes (including Pillow's CRLF), matching
        # the collector's raw-file SHA check rather than universal-newline text.
        record = package["metadata_file"]
        if record["sha256"] != package["metadata_sha256"]:
            raise MediaDependencyUnavailable("inconsistent dependency metadata pin")
        _verify(Path(distribution.locate_file(_relative(record["path"]))), record)
        for record in package["native_files"]:
            path = Path(distribution.locate_file(_relative(record["path"])))
            _verify(path, record)
            result["native_files"].append({**record, "path": str(path), "verified": True})
        result["available"] = True
        result["diagnostic"] = (
            "Pinned metadata/native bytes present; decoder behavior not executed or validated"
        )
    except (OSError, ValueError, KeyError, metadata.PackageNotFoundError,
            MediaDependencyUnavailable) as exc:
        result["diagnostic"] = str(exc)
    return result


def require_python_dependency(name: str) -> str:
    """Require pinned metadata/native bytes, without importing PIL or pypdf.

    Returns the installed version.  This is availability/integrity checking,
    not a guarantee that a hostile file is safe to parse.  Run parsers under
    the host's explicit format/input/pixel/page/stream/time/output limits.
    """
    normalized = str(name).lower()
    if normalized not in PYTHON_DEPENDENCY_PINS:
        raise MediaDependencyUnavailable("unsupported Python media dependency")
    try:
        manifest, root, frozen = _manifest()
        package = next(p for p in manifest["python_dependencies"] if p["name"] == normalized)
        result = _python_status(package, root, frozen)
        if not result["available"]:
            raise MediaDependencyUnavailable(result["diagnostic"])
        return result["installed_version"]
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        raise MediaDependencyUnavailable("media dependency audit manifest unavailable") from exc


def _binary_group(manifest: dict, root: Path, frozen: bool) -> tuple[dict, Path]:
    package = manifest["ffmpeg"]
    # Source mode uses only the reviewed local build; frozen mode only its bundle.
    # Never consult old quarantine, environment variables, cwd or PATH.
    directory = root / _relative(package["runtime_directory"] if frozen
                                 else package["runtime_source"])
    return package, _plain_path(directory)


def _runtime_relative(record: dict, frozen: bool) -> PurePosixPath:
    source = _relative(record["source_path"])
    if (len(source.parts) != 1 or record["path"] != "bin/" + str(source)
            or source.suffix.lower() not in (".exe", ".dll")):
        raise MediaDependencyUnavailable("invalid local FFmpeg runtime entry")
    return _relative(record["path"]) if frozen else source


def _exact_inventory(directory: Path, expected: set[str]) -> None:
    actual = set()
    for folder, directories, files in os.walk(_plain_path(directory), followlinks=False):
        for name in (*directories, *files):
            _plain_path(Path(folder) / name)
        for name in files:
            path = Path(folder) / name
            if not path.is_file():
                raise MediaDependencyUnavailable("non-regular media inventory entry")
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise MediaDependencyUnavailable("unexpected/missing media inventory entry")


def _verify_ffmpeg_runtime(package: dict, directory: Path, frozen: bool) -> None:
    expected = set()
    seen = set()
    for record in package["runtime_files"]:
        relative = _runtime_relative(record, frozen)
        if str(relative).casefold() in seen:
            raise MediaDependencyUnavailable("duplicate FFmpeg runtime entry")
        expected.add(str(relative))
        seen.add(str(relative).casefold())
        _verify(directory / relative, record)
    if (len(expected) != package["runtime_file_count"]
            or sum(r["size"] for r in package["runtime_files"]) != package["runtime_total_size"]):
        raise MediaDependencyUnavailable("FFmpeg runtime inventory totals differ")
    _exact_inventory(directory, expected)


def _verify_ffmpeg_compliance(package: dict, root: Path, frozen: bool) -> None:
    compliance = package.get("source_compliance", {})
    if (compliance.get("complete") is not True
            or compliance.get("delivery") != "source_accompanies_runtime"
            or compliance.get("source_offer_used") is not False
            or compliance.get("unknown_bundled_components") != []
            or package.get("origin", {}).get("kind") != "local_source_build"
            or package.get("approval_kind") != "exact_local_source_build_inventory_not_runtime_acceptance"
            or not package.get("compliance_files")):
        raise MediaDependencyUnavailable("FFmpeg complete accompanying source/license closure required")
    records = package["compliance_files"]
    roles = {record.get("role") for record in records}
    if not {"corresponding_source", "build_recipe", "license", "notice",
            "configuration", "relink_material", "integrity_manifest"} <= roles:
        raise MediaDependencyUnavailable("FFmpeg source/build/relink/license inventory incomplete")
    seen = set()
    outer = {}
    checksum_path = None
    for record in records:
        relative = _relative(record["bundle_path"])
        key = str(relative).casefold()
        if key in seen:
            raise MediaDependencyUnavailable("duplicate FFmpeg compliance destination")
        seen.add(key)
        outer[key] = (record["size"], record["sha256"])
        path = (root / "ffmpeg-compliance" / relative if frozen
                else root / _relative(record["path"]))
        _verify(path, record)
        if str(relative) == "SHA256SUMS.json":
            checksum_path = path
    required = compliance.get("required_bundle_paths", {})
    components = {"ffmpeg", "zlib", "gcc-runtime", "mingw-runtime"}
    if (set(compliance.get("reviewed_components", ())) != components
            or set(required) != components | {"build-and-relink", "delivery"}
            or any(not paths for paths in required.values())
            or any(str(_relative(path)).casefold() not in seen
                   for paths in required.values() for path in paths)
            or len(seen) != package["compliance_file_count"]
            or sum(r["size"] for r in records) != package["compliance_total_size"]):
        raise MediaDependencyUnavailable("FFmpeg exact component source/license closure incomplete")
    policy = compliance["checksum_index"]
    if checksum_path is None:
        raise MediaDependencyUnavailable("FFmpeg checksum inventory missing")
    index = json.loads(checksum_path.read_text(encoding="utf-8"))
    indexed = {}
    for record in index["files"]:
        key = str(_relative(record["path"])).casefold()
        if key in indexed:
            raise MediaDependencyUnavailable("duplicate FFmpeg checksum index entry")
        indexed[key] = (record["size"], record["sha256"])
    omitted = {"sha256sums.json", *(
        str(_relative(p)).casefold() for p in policy["additional_bundle_paths"]
    )}
    if (policy.get("bundle_path") != "SHA256SUMS.json"
            or policy.get("excluded_self") is not True
            or index.get("excluded_self") is not True
            or len(indexed) != policy["indexed_file_count"]
            or not omitted <= outer.keys()
            or indexed != {p: value for p, value in outer.items() if p not in omitted}):
        raise MediaDependencyUnavailable("FFmpeg checksum/source inventory disagreement")
    if frozen:
        _exact_inventory(root / "ffmpeg-compliance", {str(_relative(r["bundle_path"]))
                                                    for r in records})


def resolve_media_binary(name: str) -> Path:
    """Return the exact absolute ffmpeg/ffprobe path only when approved+intact.

    Verifies every bundled companion DLL as well as the requested executable.
    Never returns quarantined files, never executes --version, and never
    substitutes an installed system binary.  No lifetime/tamper-free guarantee
    is implied between this check and the host's later subprocess creation.
    """
    normalized = str(name).lower().removesuffix(".exe")
    if normalized not in ("ffmpeg", "ffprobe"):
        raise MediaDependencyUnavailable("unsupported media binary")
    try:
        manifest, root, frozen = _manifest()
        package, directory = _binary_group(manifest, root, frozen)
        if package["redistribution_status"] != "approved":
            raise MediaDependencyUnavailable(
                "FFmpeg redistribution review required: " + package["blocked_reason"]
            )
        _verify_ffmpeg_compliance(package, root, frozen)
        if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
            raise MediaDependencyUnavailable("pinned FFmpeg requires Windows x64")
        _verify_ffmpeg_runtime(package, directory, frozen)
        for record in package["runtime_files"]:
            if record["source_path"] == f"{normalized}.exe":
                return directory / _runtime_relative(record, frozen)
        raise MediaDependencyUnavailable("requested binary absent from reviewed inventory")
    except (OSError, ValueError, KeyError) as exc:
        raise MediaDependencyUnavailable("media binary audit unavailable or invalid") from exc


def get_media_dependency_status(*, verify_binary_hashes: bool = True) -> dict:
    """Return JSON-safe local diagnostics, including actual candidate paths.

    Calling this is an explicit stdlib inspection, not a decoder/native probe.
    Availability requires exact approved binaries AND accompanying source and
    license bytes. Configured capabilities are declarations, not runtime probes.
    Do not expose local paths to an untrusted remote client without the host's
    normal diagnostic filtering.
    """
    result = {
        "available": False, "python": {}, "binaries": {},
        "diagnostic": "", "native_executed": False,
        "declared_supported_codecs": [], "supported_demuxers": [],
        "supported_muxers": [], "supported_encoders": [], "supported_protocols": [],
        "png_encoding": False, "png_decoding": False,
        "av1_software_decode": False, "automatic_fallback": False,
    }
    try:
        manifest, root, frozen = _manifest()
        result["manifest_sha256"] = EXPECTED_MANIFEST_SHA256
        result["frozen"] = frozen
        for package in manifest["python_dependencies"]:
            result["python"][package["name"]] = _python_status(package, root, frozen)
        package, directory = _binary_group(manifest, root, frozen)
        capabilities = package["capabilities"]
        for key in ("declared_supported_codecs", "supported_demuxers", "supported_muxers",
                    "supported_encoders", "supported_protocols"):
            result[key] = list(capabilities[key])
        for key in ("png_encoding", "png_decoding", "av1_software_decode", "automatic_fallback"):
            result[key] = capabilities[key]
        result["capabilities"] = dict(capabilities)
        result["capability_evidence"] = capabilities["evidence"]
        result["av1_policy"] = capabilities["av1_policy"]
        result["runtime_tested"] = False
        binary_records = [
            r for r in package["runtime_files"]
            if r["path"] in ("bin/ffmpeg.exe", "bin/ffprobe.exe")
        ]
        integrity = False
        diagnostic = "Hash verification not requested"
        if verify_binary_hashes:
            try:
                _verify_ffmpeg_runtime(package, directory, frozen)
                integrity = True
                diagnostic = (
                    "Reviewed executable and companion DLL hashes match; no binary executed"
                )
            except (OSError, ValueError, KeyError, MediaDependencyUnavailable) as exc:
                diagnostic = str(exc)
        allowed = package["redistribution_status"] == "approved"
        compliance_verified = False
        if allowed:
            if verify_binary_hashes:
                try:
                    _verify_ffmpeg_compliance(package, root, frozen)
                    compliance_verified = True
                except (OSError, ValueError, KeyError, MediaDependencyUnavailable) as exc:
                    diagnostic = str(exc)
            allowed = compliance_verified
        result["ffmpeg_source_compliance"] = {
            "complete": package.get("source_compliance", {}).get("complete", False),
            "delivery": package.get("source_compliance", {}).get("delivery", ""),
            "source_offer_used": package.get("source_compliance", {}).get("source_offer_used"),
            "hash_verified": compliance_verified,
            "compliance_file_count": package["compliance_file_count"],
            "approval_kind": package["approval_kind"],
        }
        supported = sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64")
        for record in binary_records:
            name = Path(record["path"]).stem
            result["binaries"][name] = {
                "path": str(directory / _runtime_relative(record, frozen)), "version": package["version"],
                "version_evidence": package["version_evidence"], "sha256": record["sha256"],
                "size": record["size"], "hash_verified": integrity,
                "available": allowed and integrity and supported,
                "license": package["license_expression"],
                "redistribution_status": package["redistribution_status"],
                "blocked_reason": (
                    "" if allowed else
                    package["blocked_reason"] if package["redistribution_status"] != "approved"
                    else diagnostic
                ),
                "diagnostic": diagnostic,
            }
        result["available"] = (
            all(p["available"] for p in result["python"].values())
            and all(b["available"] for b in result["binaries"].values())
        )
        result["diagnostic"] = (
            "Dependency inventory only; decoding, parsing, probing and frozen acceptance "
            "remain unvalidated"
        )
    except (OSError, ValueError, KeyError, MediaDependencyUnavailable) as exc:
        result["diagnostic"] = str(exc)
    return result
