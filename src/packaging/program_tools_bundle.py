"""Bundle the three reviewed program-manager tools into fresh build staging only.

These tiny files are required frozen runtime assets, not an extra project backup.
No application/tool code is imported or executed, and no old bundle is overwritten.
The consumer accepts exactly {schema, files}; build metadata is returned, not added
as incompatible extra manifest fields. Actual frozen execution needs a later test.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib

TOOL_NAMES = frozenset({'release_tools.py', 'install_transaction.py', 'uninstall_transaction.py'})
FORMAT = 'lingxi.program-tools.v1'
MAX_TOTAL_BYTES = 100 * 1024
MAX_BOOTSTRAP_BYTES = 128 * 1024


class BundleError(ValueError):
    pass


def _checked(path):
    raw = os.fspath(path)
    if not Path(raw).is_absolute() or raw.startswith(('\\\\', '//')) or '..' in Path(raw).parts:
        raise BundleError('必须使用本机绝对路径，不接受网络/设备/父目录跳转')
    value = Path(os.path.abspath(raw))
    for part in (value, *value.parents):
        if (part.is_symlink() or part.is_junction()
                or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise BundleError('路径含链接或重解析点')
    return value


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_checked(path, maximum):
    path = _checked(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise BundleError('来源须为单链接普通文件且不超过容量限制')
    with path.open('rb') as stream:
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise BundleError('来源在打开期间改变')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > maximum or _identity(before) != _identity(after) or _identity(before) != _identity(path.stat()):
        raise BundleError('来源在读取期间改变')
    return raw, _identity(before)


def _bootstrap_metadata(raw):
    try:
        tree = ast.parse(raw.decode('utf-8-sig'))
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {'FORMAT', 'TOOL_PINS'}:
                        if target.id in constants:
                            raise BundleError('bootstrap规范常量重复')
                        constants[target.id] = ast.literal_eval(node.value)
        pins = constants.get('TOOL_PINS')
        if constants.get('FORMAT') != FORMAT or type(pins) is not dict or set(pins) != TOOL_NAMES:
            raise BundleError('bootstrap工具集合/schema不是固定三工具合同')
        for name, pin in pins.items():
            if (type(pin) is not dict or set(pin) != {'sha256', 'size'}
                    or type(pin['sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}', pin['sha256'])
                    or type(pin['size']) is not int or not 0 < pin['size'] <= MAX_TOTAL_BYTES):
                raise BundleError('bootstrap工具pin格式无效')
        if sum(pin['size'] for pin in pins.values()) > MAX_TOTAL_BYTES:
            raise BundleError('工具总量超过100KiB')
        return pins
    except (SyntaxError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError('bootstrap不是有限静态pin声明') from exc


def prepare_tools(source_root, destination):
    """Validate source pins twice, create four files, and return build evidence.

    Only source_root/packaging/_build/staging descendants are writable outputs.
    Failure after creation preserves the partial staging for review; no cleanup,
    permission changes, backup, fallback tools or previous releases are touched.
    """
    source = _checked(source_root)
    if not source.is_dir():
        raise BundleError('源码根必须为已存在目录')
    staging = _checked(source / 'packaging/_build/staging')
    target = _checked(destination)
    if target == staging or not target.is_relative_to(staging):
        raise BundleError('输出必须为本源码packaging/_build/staging内新子目录')
    if target.exists():
        raise BundleError('输出已存在，拒绝覆盖或混入未知文件')
    bootstrap = source / 'app/program_manager.py'
    bootstrap_raw, bootstrap_identity = _read_checked(bootstrap, MAX_BOOTSTRAP_BYTES)
    pins = _bootstrap_metadata(bootstrap_raw)
    version_raw, version_identity = _read_checked(source / 'pyproject.toml', MAX_BOOTSTRAP_BYTES)
    try:
        version = tomllib.loads(version_raw.decode('utf-8-sig'))['project']['version']
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise BundleError('规范版本声明无效') from exc
    if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise BundleError('规范版本必须为三段数字')
    observations = [(bootstrap, bootstrap_raw, bootstrap_identity),
                    (source / 'pyproject.toml', version_raw, version_identity)]
    contents = {}
    for name in sorted(TOOL_NAMES):
        path = source / 'packaging' / name
        raw, identity = _read_checked(path, pins[name]['size'])
        if len(raw) != pins[name]['size'] or hashlib.sha256(raw).hexdigest() != pins[name]['sha256']:
            raise BundleError('程序管理工具与bootstrap已审核pin不一致：' + name)
        contents[name] = raw
        observations.append((path, raw, identity))

    def recheck_sources():
        for path, expected, identity in observations:
            observed, current_identity = _read_checked(path, len(expected))
            if observed != expected or current_identity != identity:
                raise BundleError('构建期间来源被替换或修改，拒绝发布工具bundle')
    recheck_sources()
    # Recheck lexical parents immediately before creating this invocation's output.
    _checked(target)
    target.mkdir(parents=True, exist_ok=False)
    manifest = {'schema': FORMAT, 'files': pins}
    manifest_raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(manifest_raw) > 4096:
        raise BundleError('工具清单超过bootstrap容量限制')
    for name, raw in {**contents, 'tools-manifest.json': manifest_raw}.items():
        path = _checked(target / name)
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    if {path.name for path in target.iterdir()} != TOOL_NAMES | {'tools-manifest.json'}:
        raise BundleError('输出出现未知文件')
    for name, expected in {**contents, 'tools-manifest.json': manifest_raw}.items():
        raw, _ = _read_checked(target / name, len(expected))
        if raw != expected:
            raise BundleError('输出内容变化')
    recheck_sources()
    return {'schema': FORMAT, 'destination': str(target), 'application_version': version,
            'bootstrap_sha256': hashlib.sha256(bootstrap_raw).hexdigest(),
            'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
            'files': pins, 'tool_bytes': sum(len(raw) for raw in contents.values()),
            'application_started': False, 'project_backup_created': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare_tools(args.source_root, args.destination), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (BundleError, OSError) as exc:
        print('program-tools bundle refused: ' + str(exc), file=__import__('sys').stderr)
        raise SystemExit(1)
