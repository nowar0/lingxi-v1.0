"""Offline, pinned media dependency collection for the V0.5 Windows backend.

This module never downloads, installs, imports a decoder, executes a binary, or
changes anything on import.  The release owner supplies the reviewed Python
artifacts and exact local source-build runtime/compliance under vendor-media.
A normal frozen build must require binaries;
--python-only is explicitly a partial/development bundle, not release approval.

Integration (the caller owns its existing staging directory):
    bundle = prepare_bundle(source_root, staging / 'media-tools')
    arguments = pyinstaller_arguments(bundle)
Pass arguments to the existing PyInstaller backend invocation; do not freeze
this build tool or an external Python interpreter into the delivered package.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

# Updated only together with the reviewed manifest, never from a network result.
EXPECTED_MANIFEST_SHA256 = "2b205b5a1a07263ee34e87edf74b40aca7fe1d3a2990c5380dc377ef39884513"
PYTHON_PACKAGES = {"pillow": "13.0.0", "pypdf": "6.21.4"}
PILLOW_MODULES = (
    "PIL.Image", "PIL.ImageFile", "PIL.ImageOps", "PIL.ExifTags",
    "PIL.JpegImagePlugin", "PIL.JpegPresets", "PIL.WebPImagePlugin",
    "PIL.PngImagePlugin", "PIL._imaging", "PIL._webp",
)


class MediaDependencyBuildError(RuntimeError):
    """A pinned input is missing, changed, unsafe, or not approved to ship."""


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _plain_path(path: Path) -> Path:
    """Reject symlinks/junctions before resolve() erases their provenance."""
    absolute = Path(os.path.abspath(path))
    for item in (absolute, *absolute.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise MediaDependencyBuildError(f"linked dependency path refused: {item}")
    return absolute


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or "\\" in value or ":" in value or path.is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise MediaDependencyBuildError("invalid dependency relative path")
    return path


def _verified_file(path: Path, record: dict) -> Path:
    path = _plain_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != record["size"]:
        raise MediaDependencyBuildError(f"dependency size/type mismatch: {path.name}")
    if _sha256(path) != record["sha256"]:
        raise MediaDependencyBuildError(f"dependency SHA-256 mismatch: {path.name}")
    return path


def load_manifest(source_root: Path) -> dict:
    path = _plain_path(source_root / "packaging" / "media-dependencies.json")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 512_000
            or _sha256(path) != EXPECTED_MANIFEST_SHA256):
        raise MediaDependencyBuildError("media manifest differs from reviewed pin")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise MediaDependencyBuildError("unsupported media manifest")
    return manifest


def verify_artifacts(source_root: Path) -> dict:
    """Read-only artifact integrity inspection; does not execute installed code."""
    manifest = load_manifest(source_root)
    vendor = source_root / "packaging" / "vendor-media"
    for artifact in (*manifest["artifacts"], *manifest["audit_snapshots"]):
        _verified_file(vendor / _relative(artifact["path"]), artifact)
    for package in manifest["python_dependencies"]:
        record = package["license_file"]
        _verified_file(vendor / _relative(record["path"]), record)
    package = manifest["ffmpeg"]
    _require_ffmpeg_approval(package)
    for record in package["compliance_files"]:
        _verified_file(vendor / _relative(record["path"]), record)
    checksum_records = [r for r in package["compliance_files"]
                        if r["bundle_path"] == "SHA256SUMS.json"]
    if len(checksum_records) != 1:
        raise MediaDependencyBuildError("FFmpeg checksum inventory missing or duplicated")
    _verify_compliance_index(package, vendor / _relative(checksum_records[0]["path"]))
    _verify_runtime(package, vendor / _relative(package["runtime_source"]), source=True)
    return manifest


def _require_ffmpeg_approval(package: dict) -> None:
    """An approved label cannot bypass a physical, complete source delivery."""
    if package["redistribution_status"] != "approved":
        raise MediaDependencyBuildError(
            "FFmpeg redistribution review required: " + package["blocked_reason"]
        )
    compliance = package.get("source_compliance", {})
    if (compliance.get("complete") is not True
            or compliance.get("delivery") != "source_accompanies_runtime"
            or compliance.get("source_offer_used") is not False
            or compliance.get("unknown_bundled_components") != []
            or package.get("origin", {}).get("kind") != "local_source_build"
            or package.get("approval_kind") != "exact_local_source_build_inventory_not_runtime_acceptance"
            or not package.get("compliance_files")):
        raise MediaDependencyBuildError("FFmpeg complete accompanying source/license closure required")
    roles = {record.get("role") for record in package["compliance_files"]}
    if not {"corresponding_source", "build_recipe", "license", "notice",
            "configuration", "relink_material", "integrity_manifest"} <= roles:
        raise MediaDependencyBuildError("FFmpeg source/build/relink/license inventory incomplete")
    seen = set()
    for record in package["compliance_files"]:
        relative = str(_relative(record["bundle_path"])).casefold()
        if relative in seen:
            raise MediaDependencyBuildError("duplicate FFmpeg compliance destination")
        seen.add(relative)
    required = compliance.get("required_bundle_paths", {})
    components = {"ffmpeg", "zlib", "gcc-runtime", "mingw-runtime"}
    if (set(compliance.get("reviewed_components", ())) != components
            or set(required) != components | {"build-and-relink", "delivery"}
            or any(not paths for paths in required.values())
            or any(str(_relative(path)).casefold() not in seen
                   for paths in required.values() for path in paths)
            or len(seen) != package.get("compliance_file_count")
            or sum(r["size"] for r in package["compliance_files"])
            != package.get("compliance_total_size")):
        raise MediaDependencyBuildError("FFmpeg exact component source/license closure incomplete")


def _verify_compliance_index(package: dict, path: Path) -> None:
    """Cross-check the self-excluding index against the outer pinned inventory."""
    policy = package["source_compliance"]["checksum_index"]
    index = json.loads(path.read_text(encoding="utf-8"))
    if (policy.get("bundle_path") != "SHA256SUMS.json"
            or policy.get("excluded_self") is not True
            or index.get("excluded_self") is not True):
        raise MediaDependencyBuildError("invalid FFmpeg checksum self-reference policy")
    indexed = {}
    for record in index["files"]:
        relative = str(_relative(record["path"])).casefold()
        if relative in indexed:
            raise MediaDependencyBuildError("duplicate FFmpeg checksum index entry")
        indexed[relative] = (record["size"], record["sha256"])
    outer = {str(_relative(r["bundle_path"])).casefold(): (r["size"], r["sha256"])
             for r in package["compliance_files"]}
    omitted = {"sha256sums.json", *(
        str(_relative(p)).casefold() for p in policy["additional_bundle_paths"]
    )}
    if (len(indexed) != policy["indexed_file_count"]
            or not omitted <= outer.keys()
            or indexed != {p: value for p, value in outer.items() if p not in omitted}):
        raise MediaDependencyBuildError("FFmpeg checksum/source inventory disagreement")


def _exact_inventory(directory: Path, expected: set[str]) -> None:
    actual = set()
    for folder, directories, files in os.walk(_plain_path(directory), followlinks=False):
        for name in (*directories, *files):
            _plain_path(Path(folder) / name)
        for name in files:
            path = Path(folder) / name
            if not path.is_file():
                raise MediaDependencyBuildError("non-regular file in media inventory")
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise MediaDependencyBuildError("unexpected/missing file in media inventory")


def _verify_runtime(package: dict, directory: Path, *, source: bool) -> None:
    expected = set()
    source_names = set()
    for record in package["runtime_files"]:
        relative = str(_relative(record["source_path"] if source else record["path"]))
        source_name = str(_relative(record["source_path"]))
        if (relative.casefold() in {p.casefold() for p in expected}
                or source_name.casefold() in source_names
                or record["path"] != "bin/" + source_name
                or "/" in source_name or Path(source_name).suffix.lower() not in (".exe", ".dll")):
            raise MediaDependencyBuildError("invalid/duplicate local FFmpeg runtime entry")
        expected.add(relative)
        source_names.add(source_name.casefold())
        _verified_file(directory / _relative(relative), record)
    if (len(expected) != package["runtime_file_count"]
            or sum(r["size"] for r in package["runtime_files"]) != package["runtime_total_size"]):
        raise MediaDependencyBuildError("local FFmpeg runtime inventory totals differ")
    _exact_inventory(directory, expected)


def _check_python_metadata(manifest: dict) -> None:
    for package in manifest["python_dependencies"]:
        name, version = package["name"], package["version"]
        if PYTHON_PACKAGES.get(name) != version:
            raise MediaDependencyBuildError("unexpected Python media pin")
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise MediaDependencyBuildError(f"missing pinned distribution: {name}") from exc
        if distribution.version != version:
            raise MediaDependencyBuildError(f"installed media version drift: {name}")
        # Hash exact wheel bytes: read_text() normalizes Pillow's CRLF and would
        # disagree with the release collector's frozen raw-METADATA readback.
        record = package["metadata_file"]
        if record["sha256"] != package["metadata_sha256"]:
            raise MediaDependencyBuildError(f"inconsistent media metadata pin: {name}")
        _verified_file(Path(distribution.locate_file(_relative(record["path"]))), record)
        for record in package["native_files"]:
            _verified_file(Path(distribution.locate_file(_relative(record["path"]))), record)


def prepare_bundle(source_root: Path, destination: Path, *, require_binaries: bool = True) -> Path:
    """Create a new, offline collector input; never overwrite existing outputs.

    Only source-tree packaging/_build or vendor-media children are accepted.
    Licensing gates run before any output is created.  The complete license
    files are carried even when only the JPEG/WebP Pillow native subset ships.
    """
    source_root = _plain_path(source_root)
    destination = _plain_path(destination)
    allowed = (source_root / "packaging" / "_build", source_root / "packaging" / "vendor-media")
    if not any(destination.is_relative_to(root) and destination != root for root in allowed):
        raise MediaDependencyBuildError("media output must be inside the project build/vendor tree")
    if destination.exists():
        raise MediaDependencyBuildError("refuse to overwrite an existing media bundle")
    manifest = verify_artifacts(source_root)
    _check_python_metadata(manifest)
    ffmpeg = manifest["ffmpeg"]
    if require_binaries:
        _require_ffmpeg_approval(ffmpeg)
    vendor = source_root / "packaging" / "vendor-media"
    destination.mkdir(parents=True, exist_ok=False)
    raw_manifest = (source_root / "packaging" / "media-dependencies.json").read_bytes()
    (destination / "media-dependencies.json").write_bytes(raw_manifest)
    # License bytes were originally extracted from hash-verified upstream wheels.
    for package in manifest["python_dependencies"]:
        license_record = package["license_file"]
        origin = _verified_file(vendor / _relative(license_record["path"]), license_record)
        target = destination / "licenses" / origin.name
        target.parent.mkdir(exist_ok=True)
        with target.open("xb") as stream:
            stream.write(origin.read_bytes())
    if require_binaries:
        runtime_source = vendor / _relative(ffmpeg["runtime_source"])
        for record in ffmpeg["runtime_files"]:
            origin = _verified_file(runtime_source / _relative(record["source_path"]), record)
            target = destination / _relative(ffmpeg["runtime_directory"]) / _relative(record["path"])
            _copy_verified(origin, target, record)
        # An explicit selected inventory, never a recursive vendor/cache copy.
        for record in ffmpeg["compliance_files"]:
            origin = _verified_file(vendor / _relative(record["path"]), record)
            target = destination / "ffmpeg-compliance" / _relative(record["bundle_path"])
            _copy_verified(origin, target, record)
    _verify_bundle_inventory(destination, manifest, require_binaries)
    return destination


def _copy_verified(origin: Path, target: Path, record: dict) -> None:
    target = _plain_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with origin.open("rb") as source, target.open("xb") as output:
        while block := source.read(1024 * 1024):
            output.write(block)
    _verified_file(target, record)


def _verify_bundle_inventory(bundle: Path, manifest: dict, require_binaries: bool) -> None:
    manifest_path = _plain_path(bundle / "media-dependencies.json")
    if _sha256(manifest_path) != EXPECTED_MANIFEST_SHA256:
        raise MediaDependencyBuildError("copied bundle manifest differs from reviewed pin")
    expected = {"media-dependencies.json"}
    for package in manifest["python_dependencies"]:
        record = package["license_file"]
        relative = "licenses/" + PurePosixPath(record["path"]).name
        _verified_file(bundle / relative, record)
        expected.add(relative)
    if require_binaries:
        package = manifest["ffmpeg"]
        _require_ffmpeg_approval(package)
        _verify_runtime(package, bundle / _relative(package["runtime_directory"]), source=False)
        for record in package["runtime_files"]:
            relative = str(_relative(package["runtime_directory"])) + "/" + record["path"]
            expected.add(relative)
        for record in package["compliance_files"]:
            relative = "ffmpeg-compliance/" + str(_relative(record["bundle_path"]))
            if relative in expected:
                raise MediaDependencyBuildError("duplicate FFmpeg compliance destination")
            _verified_file(bundle / _relative(relative), record)
            expected.add(relative)
        _verify_compliance_index(package, bundle / "ffmpeg-compliance" / "SHA256SUMS.json")
    actual = set()
    for directory, folders, files in os.walk(bundle, followlinks=False):
        for name in (*folders, *files):
            _plain_path(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            if not path.is_file():
                raise MediaDependencyBuildError("non-regular file in media bundle")
            actual.add(path.relative_to(bundle).as_posix())
    if actual != expected:
        raise MediaDependencyBuildError("unexpected/missing file in media bundle")


def pyinstaller_arguments(bundle: Path, *, require_binaries: bool = True) -> list[str]:
    """Arguments for the existing onedir collector; no subprocess is started.

    --add-data deliberately preserves vendor EXE/DLL bytes rather than allowing
    PyInstaller's binary rewriting/UPX processing to invalidate audited hashes.
    JPEG/WebP do not need PIL.ImageFont/_imagingft, libavif, Tk or PDFium.
    The Pillow hook may collect additional format plugins; unwanted optional
    font/GUI/AVIF modules are explicitly excluded.  Check the final collection
    at the user-approved concentrated acceptance stage.
    """
    bundle = _plain_path(bundle)
    manifest_path = _plain_path(bundle / "media-dependencies.json")
    if _sha256(manifest_path) != EXPECTED_MANIFEST_SHA256:
        raise MediaDependencyBuildError("bundle has no reviewed media manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if require_binaries:
        _require_ffmpeg_approval(manifest["ffmpeg"])
    _verify_bundle_inventory(bundle, manifest, require_binaries)
    # Pillow native hash pins require untouched .pyd files; never enable UPX/strip.
    result = [
        "--noupx", "--add-data", f"{bundle}{os.pathsep}media-tools",
        "--collect-submodules", "pypdf",
    ]
    for package in PYTHON_PACKAGES:
        result.extend(("--copy-metadata", package))
    for module in PILLOW_MODULES:
        result.extend(("--hidden-import", module))
    for module in ("PIL.ImageFont", "PIL._imagingft", "PIL._avif", "PIL.AvifImagePlugin",
                   "PIL.ImageTk", "PIL._imagingtk", "tkinter"):
        result.extend(("--exclude-module", module))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path)
    parser.add_argument(
        "--python-only", action="store_true", help="partial bundle; not a releasable V0.5"
    )
    parser.add_argument("--inventory", action="store_true", help="read-only SHA/metadata inventory")
    options = parser.parse_args()
    try:
        if options.inventory:
            manifest = verify_artifacts(options.source_root)
            _check_python_metadata(manifest)
            print(json.dumps({
                "integrity": "verified", "ffmpeg": manifest["ffmpeg"]["redistribution_status"],
            }))
        elif options.destination is not None:
            bundle = prepare_bundle(
                options.source_root, options.destination, require_binaries=not options.python_only
            )
            arguments = pyinstaller_arguments(bundle, require_binaries=not options.python_only)
            print(json.dumps(arguments, ensure_ascii=False))
        else:
            parser.error("--inventory or --destination is required")
    except (OSError, ValueError, KeyError, MediaDependencyBuildError) as exc:
        print(f"media dependency collection refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
