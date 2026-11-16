"""Build a separately inventoried minimal plugin worker, never a host backend.

Planning only reads explicit source files. Building requires an explicit invocation,
uses one new UUID directory, does not remove/overwrite old outputs, install packages,
launch the built worker, copy to an ordinary test location, or change ACL/MIC/profile.
A completed build is NOT evidence that AppContainer execution or isolation works.
"""
from __future__ import annotations

import argparse
import ast
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tomllib
import uuid

FORMAT = 'lingxi.sandbox-runtime-build-plan.v1'
MANIFEST = 'runtime-inventory.json'
NAME = 'LingxiPluginWorker'
INPUTS = ('app/__init__.py', 'app/plugin_sandbox_worker.py', 'app/plugin_mcp_sandbox_worker.py',
          'app/plugin_stage_worker.py', 'app/plugin_wire.py', 'pyproject.toml', 'packaging/sandbox_worker_entry.py', 'packaging/build_sandbox_runtime.py')
ALLOWED_APP_MODULES = {'app', 'app.plugin_wire', 'app.plugin_sandbox_worker', 'app.plugin_mcp_sandbox_worker', 'app.plugin_stage_worker'}
FORBIDDEN = {'keyring', 'numpy', 'onnxruntime', 'tokenizers', 'playwright', 'cryptography',
             'fastapi', 'uvicorn', 'openai', 'httpx', 'httpx2', 'mcp', 'asyncssh',
             'config', 'models', 'attachments', 'characters', 'plugin-packages'}
MAX_FILES = 50000
MAX_BYTES = 512 * 1024**2


class RuntimeBuildError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def checked(path):
    raw = os.fspath(path)
    if raw.startswith(('\\\\', '//')) or '..' in Path(raw).parts:
        raise RuntimeBuildError('network/device/parent path refused')
    result = Path(os.path.abspath(raw))
    for parent in (result, *result.parents):
        if (parent.is_symlink() or parent.is_junction()
                or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise RuntimeBuildError('link/reparse path refused')
    return result.resolve()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or value.startswith('/'):
        raise RuntimeBuildError('invalid relative runtime path')
    for part in value.split('/'):
        if (not part or part in ('.', '..') or part.endswith(('.', ' '))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise RuntimeBuildError('unsafe runtime path')
    return value


def file_record(path, *, identity=False, dependency=False):
    path = checked(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or before.st_nlink != 1 and not dependency:
        raise RuntimeBuildError('ordinary single-link file required')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        ended = os.fstat(stream.fileno())
    ident = lambda x: [x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_nlink]
    if not ident(before) == ident(opened) == ident(ended) == ident(path.stat()):
        raise RuntimeBuildError('file changed while hashing')
    result = {'sha256': digest, 'size': before.st_size}
    if identity:
        result['identity'] = ident(before)
    return result


def python_inputs(python):
    """Pin the launcher AND its base Python; no interpreter probe/process here."""
    python = checked(python)
    if python != checked(sys.executable):
        raise RuntimeBuildError('Base Python provenance requires the current explicitly selected interpreter')
    prefix, base = checked(sys.prefix), checked(sys.base_prefix)
    config = prefix / 'pyvenv.cfg'
    files, directories = {}, {}
    def failed(error):
        raise RuntimeBuildError('Python input tree unreadable: ' + str(error)) from error
    def add(path):
        path = checked(path)
        files[str(path)] = file_record(path, identity=True, dependency=True)
        for directory in path.parents:
            info = checked(directory).lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeBuildError('Python input ancestor is not a directory')
            directories[str(directory)] = [info.st_dev, info.st_ino]
    add(python)
    if config.is_file():
        add(config)
    for path in sorted(base.iterdir()):
        if path.suffix.casefold() == '.dll' or path.name.casefold() in {'python.exe', 'pythonw.exe'}:
            add(path)
    if not any(Path(name).name.casefold().startswith('python') and Path(name).suffix.casefold() == '.dll' for name in files):
        raise RuntimeBuildError('base Python DLL missing from provenance')
    archives = sorted(base.glob('python*.zip'))
    for path in archives:
        add(path)
    library = base / 'Lib'
    if library.is_dir():
        for folder, dirs, names in os.walk(checked(library), followlinks=False, onerror=failed):
            dirs[:] = sorted(name for name in dirs if name not in {'site-packages', '__pycache__', '.git'})
            for name in dirs:
                checked(Path(folder) / name)
            for name in sorted(names):
                if not name.endswith(('.pyc', '.pyo')):
                    add(Path(folder) / name)
    elif not archives:
        raise RuntimeBuildError('base Python stdlib zip or Lib required')
    dlls = base / 'DLLs'
    if dlls.exists():
        if not checked(dlls).is_dir():
            raise RuntimeBuildError('base Python DLLs is not a directory')
        for folder, dirs, names in os.walk(dlls, followlinks=False, onerror=failed):
            for name in dirs:
                checked(Path(folder) / name)
            for name in sorted(names):
                if Path(name).suffix.casefold() in {'.dll', '.pyd'}:
                    add(Path(folder) / name)
    return {'prefix': str(prefix), 'base_prefix': str(base),
            'files': dict(sorted(files.items())), 'directory_identities': dict(sorted(directories.items()))}


def fixed_child_path(python, *, node_directory=None, inputs=None):
    inputs = inputs or python_inputs(python)
    base, prefix = Path(inputs['base_prefix']), Path(inputs['prefix'])
    windows = checked(os.environ['SystemRoot'])
    paths = ([checked(node_directory)] if node_directory is not None else [])
    paths += [prefix / 'Scripts', base]
    if (base / 'DLLs').is_dir():
        paths.append(base / 'DLLs')
    paths.append(windows / 'System32')
    result = []
    for path in paths:
        if not checked(path).is_dir():
            raise RuntimeBuildError('Explicit child PATH directory missing: ' + str(path))
        if str(path).casefold() not in {value.casefold() for value in result}:
            result.append(str(path))
    return os.pathsep.join(result)


def pe_version_record(path, version):
    """Optional compiler input, deliberately NOT part of strict worker INPUTS."""
    if path is None:
        return None
    if (type(version) is not str or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version)
            or any(int(part) > 65535 for part in version.split('.'))):
        raise RuntimeBuildError('Canonical PE version required')
    if not Path(path).is_absolute():
        raise RuntimeBuildError('Explicit absolute PE version file required')
    path = checked(path)
    relative(path.name)
    before = path.stat()
    item = file_record(path)
    if not item['size']:
        raise RuntimeBuildError('Empty PE version file refused')
    identity = lambda info: [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]
    if identity(before) != identity(path.stat()):
        raise RuntimeBuildError('PE version file changed during observation')
    return {'path': str(path), 'name': path.name, 'version': version,
            **item, 'identity': identity(before)}


def require_pe_version(plan):
    expected = plan.get('pe_version_record')
    if expected is not None and pe_version_record(expected['path'], expected['version']) != expected:
        raise RuntimeBuildError('PE version input changed; re-plan/re-freeze, never hot-patch')


def pe_readback(path, expected):
    """Read fixed and every translated PE version string; never launch the file."""
    path = checked(path)
    api = ctypes.WinDLL('version', use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    handle = wintypes.DWORD(0)
    size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
    if not size:
        raise RuntimeBuildError('PE has no version resource: ' + str(path))
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, data):
        raise RuntimeBuildError('PE version read failed')
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    def query(key):
        if not api.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)) or not pointer.value:
            raise RuntimeBuildError('Missing PE version field: ' + key)
        return pointer.value, length.value
    address, count = query('\\')
    if count < 52:
        raise RuntimeBuildError('Truncated PE fixed version info')
    fields = list((wintypes.DWORD * 13).from_address(address))
    parts = tuple(map(int, expected.split('.'))) + (0,)
    versions = [((fields[i] >> 16) & 65535, fields[i] & 65535,
                 (fields[i + 1] >> 16) & 65535, fields[i + 1] & 65535) for i in (2, 4)]
    if fields[0] != 0xFEEF04BD or any(value != parts for value in versions):
        raise RuntimeBuildError('PE numeric FileVersion/ProductVersion mismatch')
    address, count = query('\\VarFileInfo\\Translation')
    if count < 4 or count % 4:
        raise RuntimeBuildError('Invalid PE language translations')
    languages = (ctypes.c_ushort * (count // 2)).from_address(address)
    strings = []
    for index in range(0, len(languages), 2):
        values = {}
        for name in ('FileVersion', 'ProductVersion'):
            address, count = query('\\StringFileInfo\\%04x%04x\\%s' % (languages[index], languages[index + 1], name))
            text = ctypes.wstring_at(address).rstrip('\x00')
            if text not in {expected, expected + '.0'}:
                raise RuntimeBuildError('PE string version mismatch')
            values[name] = text
        strings.append(values)
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise RuntimeBuildError('Not a Windows PE')
        stream.seek(0x3c); offset = struct.unpack('<I', stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b'PE\x00\x00' or struct.unpack('<H', stream.read(2))[0] != 0x8664:
            raise RuntimeBuildError('Windows AMD64 PE required')
    return {'numeric': list(parts), 'strings': strings}


def pe_build_options(plan, executable):
    require_pe_version(plan)
    item = plan.get('pe_version_record')
    if item is None:
        return None
    result = {'pe_version_file': {key: item[key] for key in ('name', 'version', 'size', 'sha256')},
              'pe_version_readback': pe_readback(executable, item['version'])}
    require_pe_version(plan)
    return result


def verify_pe_options(document, executable):
    # Legacy unversioned manifests keep exactly their previous verification rules.
    if 'build_options' not in document:
        return
    options = document['build_options']
    if type(options) is not dict or set(options) != {'pe_version_file', 'pe_version_readback'}:
        raise RuntimeBuildError('Invalid optional PE build options')
    item, readback = options['pe_version_file'], options['pe_version_readback']
    if (type(item) is not dict or set(item) != {'name', 'version', 'size', 'sha256'}
            or type(item.get('name')) is not str or '/' in item['name']
            or type(item.get('version')) is not str
            or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', item['version'])
            or any(int(part) > 65535 for part in item['version'].split('.'))
            or item['version'] != document.get('host_version')
            or type(item.get('size')) is not int or item['size'] < 1
            or type(item.get('sha256')) is not str or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
            or type(readback) is not dict or set(readback) != {'numeric', 'strings'}
            or type(readback.get('numeric')) is not list
            or any(type(part) is not int for part in readback['numeric'])
            or readback['numeric'] != [*map(int, item['version'].split('.')), 0]
            or type(readback.get('strings')) is not list or not readback['strings']):
        raise RuntimeBuildError('Invalid optional PE version pin')
    relative(item['name'])
    for values in readback['strings']:
        if (type(values) is not dict or set(values) != {'FileVersion', 'ProductVersion'}
                or any(type(value) is not str or value not in {item['version'], item['version'] + '.0'}
                       for value in values.values())):
            raise RuntimeBuildError('Invalid optional PE string version evidence')
    if pe_readback(executable, item['version']) != readback:
        raise RuntimeBuildError('PE manifest version readback drift')


def source_records(source):
    records = {name: file_record(source / name) for name in INPUTS}
    if (source / 'app/__init__.py').read_text(encoding='utf-8-sig').strip():
        raise RuntimeBuildError('app package initializer must remain empty; no host initialization')
    # Prevent this narrow builder from silently becoming a host-app collector.
    for name in ('app/plugin_wire.py', 'app/plugin_sandbox_worker.py', 'app/plugin_mcp_sandbox_worker.py', 'app/plugin_stage_worker.py',
                 'packaging/sandbox_worker_entry.py'):
        tree = ast.parse((source / name).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                raise RuntimeBuildError('relative worker import requires explicit closure review')
            modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module
                       else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            for module in modules:
                top = module.split('.')[0]
                if top == 'app':
                    if module not in ALLOWED_APP_MODULES or module == 'app':
                        raise RuntimeBuildError('worker source imports an unapproved host module')
                elif top not in sys.stdlib_module_names or top in FORBIDDEN:
                    raise RuntimeBuildError('worker source imports forbidden runtime dependency')
    return records


def _paths(source, build_root, python, run_id):
    source, build_root, python = checked(source), checked(build_root), checked(python)
    if not source.is_dir() or not python.is_file():
        raise RuntimeBuildError('explicit existing source and Python executable required')
    expected = source / 'packaging/_build/sandbox-runtime'
    if build_root != expected or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise RuntimeBuildError('build root/run id not in unique sandbox-runtime namespace')
    output = build_root / run_id
    if output.exists():
        raise RuntimeBuildError('old runtime output exists; never overwrite or delete it')
    return source, build_root, python, output


def _make_plan(source, build_root, python, run_id, pe_version_file=None):
    source, build_root, python, output = _paths(source, build_root, python, run_id)
    inputs = source_records(source)
    project = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8-sig'))
    version = project['project']['version']
    if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise RuntimeBuildError('canonical project version required')
    command = [str(python), '-I', '-m', 'PyInstaller', '--noconfirm', '--onedir', '--console', '--noupx',
               '--name', NAME, '--paths', str(source), '--hidden-import', 'app.plugin_sandbox_worker',
               '--hidden-import', 'app.plugin_wire', '--hidden-import', 'app.plugin_mcp_sandbox_worker',
               '--hidden-import', 'app.plugin_stage_worker',
               '--distpath', str(output / 'dist'),
               '--workpath', str(output / 'work'), '--specpath', str(output / 'spec')]
    for module in sorted(FORBIDDEN - {'config', 'models', 'attachments', 'characters', 'plugin-packages'}):
        command += ['--exclude-module', module]
    pe_input = pe_version_record(pe_version_file, version)
    if pe_input is not None:
        command += ['--version-file', pe_input['path']]
    command += [str(source / 'packaging/sandbox_worker_entry.py')]
    body = {'schema': FORMAT, 'source_root': str(source), 'build_root': str(build_root),
            'python': str(python), 'python_record': file_record(python), 'run_id': run_id,
            'output_root': str(output), 'runtime_root': str(output / 'dist' / NAME),
            'version': version, 'worker_api': 1, 'source_inputs': inputs, 'command': command,
            'build_started': False, 'runtime_executed': False}
    if pe_input is not None:
        body['pe_version_record'] = pe_input
    return {**body, 'fingerprint': fingerprint(body)}


def plan_build(source_root, build_root, python, *, pe_version_file=None):
    """Read-only source/optional PE pinning, no directories/processes created."""
    return _make_plan(source_root, build_root, python, uuid.uuid4().hex, pe_version_file)


def validate_plan(plan):
    if type(plan) is not dict or plan.get('schema') != FORMAT:
        raise RuntimeBuildError('unknown build plan')
    fresh = _make_plan(plan['source_root'], plan['build_root'], plan['python'], plan['run_id'],
                       plan.get('pe_version_record', {}).get('path'))
    if fresh != plan:
        raise RuntimeBuildError('source/interpreter/build plan changed; do not build stale inputs')
    return fresh


def audit_toc_origins(output, product, interpreter_inputs):
    """Record actual Analysis/PYZ/COLLECT origins, including generated inputs."""
    work = checked(Path(output) / 'work' / product)
    result = {}
    base = checked(interpreter_inputs['base_prefix'])
    pinned = interpreter_inputs['files']
    for name, list_index in (('Analysis-00.toc', None), ('PYZ-00.toc', 1), ('COLLECT-00.toc', 0)):
        path = work / name
        before = file_record(path)
        if before['size'] > 64 * 1024**2:
            raise RuntimeBuildError('Compiler TOC exceeds audit bound')
        value = ast.literal_eval(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, tuple) or list_index is not None and (
                len(value) <= list_index or not isinstance(value[list_index], list)):
            raise RuntimeBuildError('Unknown compiler origin TOC format')
        # Analysis has several top-level lists (scripts, pure modules, binaries,
        # data, etc.). PYZ's third field is an integer module/package type code,
        # unlike the string type tags in Analysis/COLLECT.
        lists = [part for part in value if isinstance(part, list)] if list_index is None else [value[list_index]]
        entries = []
        for items in lists:
            for item in items:
                if not (isinstance(item, (tuple, list)) and len(item) == 3
                        and isinstance(item[0], str)
                        and (isinstance(item[2], str) or name == 'PYZ-00.toc' and type(item[2]) is int)):
                    if list_index is not None:
                        raise RuntimeBuildError('Invalid compiler origin inventory')
                    continue
                alias, origin, kind = item
                if origin is not None and not isinstance(origin, str):
                    raise RuntimeBuildError('Invalid compiler origin path')
                entry = {'name': alias, 'origin': origin, 'kind': kind}
                if origin not in (None, '', '-'):
                    actual = checked(origin)
                    if actual.is_file():
                        entry['origin_record'] = file_record(actual, identity=True, dependency=True)
                        if actual.is_relative_to(base) and 'site-packages' not in actual.relative_to(base).parts:
                            if pinned.get(str(actual)) != entry['origin_record']:
                                raise RuntimeBuildError('Compiler used an unpinned/changed base Python origin: ' + str(actual))
                    else:
                        # zipimport module origins can name a member below a real
                        # pythonXY.zip. Pin the real archive, never pretend that
                        # pseudo-directory/member is a missing ordinary source.
                        archives = [parent for parent in actual.parents
                                    if str(parent) in pinned and parent.suffix.casefold() == '.zip']
                        if len(archives) != 1:
                            raise RuntimeBuildError('Compiler origin is not a file or pinned stdlib zip member: ' + origin)
                        archive = archives[0]
                        observed = file_record(archive, identity=True, dependency=True)
                        if observed != pinned[str(archive)]:
                            raise RuntimeBuildError('Compiler stdlib archive origin changed')
                        entry['archive_origin'] = str(archive)
                        entry['archive_member'] = relative(actual.relative_to(archive).as_posix())
                        entry['origin_record'] = observed
                entries.append(entry)
        if file_record(path) != before:
            raise RuntimeBuildError('Compiler TOC changed during origin audit')
        result[name] = {'toc_record': before, 'entries': entries}
    return result


def _audit_analysis(output, source):
    toc = checked(output / 'work' / NAME / 'PYZ-00.toc')
    if not toc.is_file():
        raise RuntimeBuildError('missing actual PyInstaller module inventory')
    record = ast.literal_eval(toc.read_text(encoding='utf-8-sig'))
    if not isinstance(record, tuple) or len(record) < 2 or not isinstance(record[1], list):
        raise RuntimeBuildError('unknown PyInstaller PYZ inventory')
    names = []
    for item in record[1]:
        if not isinstance(item, tuple) or len(item) < 3 or not isinstance(item[0], str):
            raise RuntimeBuildError('invalid analyzed module')
        name, path = item[:2]
        top = name.split('.')[0]
        if top in FORBIDDEN or (top == 'app' and name not in ALLOWED_APP_MODULES):
            raise RuntimeBuildError('host/forbidden module in minimal worker: ' + name)
        normalized = str(path).replace('\\', '/').casefold()
        if ('/site-packages/' in normalized or '/dist-packages/' in normalized) and top != 'app':
            raise RuntimeBuildError('unexpected third-party module in minimal worker: ' + name)
        if top == 'app' and checked(path) != source / (name.replace('.', '/') + ('.py' if name != 'app' else '/__init__.py')):
            raise RuntimeBuildError('worker app source came from an unexpected tree')
        names.append(name)
    if not ALLOWED_APP_MODULES <= set(names):
        raise RuntimeBuildError('minimal worker modules missing from PYZ')
    return sorted(names)


def _audit_collect(output):
    """Reject unexpected third-party binaries/data, not just visible directory names."""
    path = checked(output / 'work' / NAME / 'COLLECT-00.toc')
    if not path.is_file():
        raise RuntimeBuildError('missing PyInstaller collection inventory')
    value = ast.literal_eval(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, tuple) or not value or not isinstance(value[0], list):
        raise RuntimeBuildError('unknown PyInstaller collection inventory')
    seen = set()
    for item in value[0]:
        if not isinstance(item, tuple) or len(item) != 3 or not all(isinstance(x, str) for x in item):
            raise RuntimeBuildError('invalid collection item')
        name, origin, kind = item
        relative(name)
        if name.casefold() in seen:
            raise RuntimeBuildError('duplicate collection artifact')
        seen.add(name.casefold())
        normalized = origin.replace('\\', '/').casefold()
        if '/site-packages/' in normalized or '/dist-packages/' in normalized:
            raise RuntimeBuildError('unexpected third-party collection artifact: ' + name)
        if kind not in {'EXECUTABLE', 'BINARY', 'EXTENSION', 'DATA'}:
            raise RuntimeBuildError('unknown collection type requires review: ' + kind)
        if not (name == NAME + '.exe' or name == 'base_library.zip'
                or Path(name).suffix.casefold() in {'.dll', '.pyd'}):
            raise RuntimeBuildError('unknown collected data requires review: ' + name)
    if (NAME + '.exe').casefold() not in seen:
        raise RuntimeBuildError('worker executable absent from collection inventory')


def inventory_runtime(runtime, *, allow_manifest=False):
    runtime = checked(runtime)
    if not runtime.is_dir():
        raise RuntimeBuildError('built runtime missing')
    records, seen, total = [], {}, 0
    for folder, dirs, files in os.walk(runtime, followlinks=False):
        for name in dirs + files:
            path = checked(Path(folder) / name)
            rel = relative(path.relative_to(runtime).as_posix())
            if rel.casefold() in seen:
                raise RuntimeBuildError('runtime case/path collision')
            seen[rel.casefold()] = path.is_dir()
            if any(part.casefold() in FORBIDDEN for part in rel.split('/')):
                raise RuntimeBuildError('forbidden host asset in worker runtime: ' + rel)
            if path.is_dir():
                if len(seen) > MAX_FILES:
                    raise RuntimeBuildError('runtime directory/file count exceeds limit')
                continue
            if rel == MANIFEST:
                if allow_manifest:
                    file_record(path)
                    continue
                raise RuntimeBuildError('unexpected old manifest in new runtime')
            if path.suffix.casefold() in {'.db', '.sqlite', '.sqlite3', '.wal', '.shm'}:
                raise RuntimeBuildError('unexpected database in new runtime')
            if not (rel == NAME + '.exe' or (rel.startswith('_internal/')
                    and (path.suffix.casefold() in {'.dll', '.pyd'} or rel == '_internal/base_library.zip'))):
                raise RuntimeBuildError('unknown runtime artifact requires review: ' + rel)
            record = file_record(path)
            with path.open('rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':
                    raise RuntimeBuildError('SQLite bytes must never enter plugin runtime')
            total += record['size']
            if total > MAX_BYTES or len(seen) > MAX_FILES:
                raise RuntimeBuildError('runtime size/file count exceeds limit')
            records.append({'relative_path': rel, **record})
    if not any(entry['relative_path'] == NAME + '.exe' for entry in records):
        raise RuntimeBuildError('dedicated worker executable missing')
    return sorted(records, key=lambda item: item['relative_path'])


def build(plan, *, runner=subprocess.run):
    """Explicit build only; runner inherits stdio. Built runtime is never executed."""
    plan = validate_plan(plan)
    output, source = Path(plan['output_root']), Path(plan['source_root'])
    checked(output.parent).mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    for part in ('dist', 'work', 'spec', 'cache'):
        (output / part).mkdir()
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['PYINSTALLER_CONFIG_DIR'] = str(output / 'cache')
    metadata_path = output / 'target-python.json'
    probe = ('import json,platform,struct,sys;from pathlib import Path;'
             'Path(sys.argv[1]).write_text(json.dumps({"version":sys.version.split()[0],'
             '"bits":struct.calcsize("P")*8,"machine":platform.machine()}),encoding="utf-8")')
    probe_result = runner([plan['python'], '-I', '-c', probe, str(metadata_path)],
                          cwd=str(source), env=env, check=False)
    if probe_result.returncode != 0 or not metadata_path.is_file():
        raise RuntimeBuildError('target interpreter metadata probe failed; no compilation')
    metadata_record = file_record(metadata_path)
    if metadata_record['size'] > 4096:
        raise RuntimeBuildError('target interpreter metadata exceeds bound')
    target_python = json.loads(metadata_path.read_text(encoding='utf-8'))
    if (type(target_python) is not dict or set(target_python) != {'version', 'bits', 'machine'}
            or not isinstance(target_python['version'], str)
            or not re.fullmatch(r'3\.\d+\.\d+(?:[A-Za-z0-9.+-]*)', target_python['version'])
            or type(target_python['bits']) is not int or target_python['bits'] not in (32, 64)
            or not isinstance(target_python['machine'], str) or not 1 <= len(target_python['machine']) <= 100):
        raise RuntimeBuildError('invalid target interpreter metadata')
    require_pe_version(plan)
    result = runner(plan['command'], cwd=str(source), env=env, check=False)
    require_pe_version(plan)
    if result.returncode != 0:
        raise RuntimeBuildError('minimal worker build failed; failed output retained for review')
    if source_records(source) != plan['source_inputs'] or file_record(Path(plan['python'])) != plan['python_record']:
        raise RuntimeBuildError('source/interpreter changed during build; output not published as verified')
    modules = _audit_analysis(output, source)
    _audit_collect(output)
    if source_records(source) != plan['source_inputs']:
        raise RuntimeBuildError('source changed during analysis audit; output unverified')
    runtime = Path(plan['runtime_root'])
    entries = inventory_runtime(runtime)
    document = {'schema': 'lingxi.sandbox-runtime.v1', 'product': NAME, 'host_version': plan['version'],
                'worker_api': 1, 'worker_modes': ['--package-mcp-server','--plugin-stage-worker','--plugin-worker'], 'built_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                'python': {'builder_version': sys.version.split()[0], 'builder_bits': struct.calcsize('P') * 8,
                           'target': target_python, 'target_executable_sha256': plan['python_record']['sha256'],
                           'notice': 'Target metadata queried from the explicitly pinned interpreter; frozen runtime execution still untested'},
                'source_inputs': plan['source_inputs'], 'build_plan_fingerprint': plan['fingerprint'],
                'files': entries, 'inventory_sha256': fingerprint(entries), 'analyzed_modules': modules,
                'runtime_executed': False, 'appcontainer_tested': False,
                'notice': 'Integrity inventory is not a signature or an OS-isolation acceptance result.'}
    options = pe_build_options(plan, runtime / (NAME + '.exe'))
    if options is not None:
        document['build_options'] = options
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    require_pe_version(plan)
    if source_records(source) != plan['source_inputs'] or inventory_runtime(runtime) != entries:
        raise RuntimeBuildError('source or runtime changed before manifest publication')
    with (runtime / MANIFEST).open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    verify_runtime(runtime)
    return {'runtime_root': str(runtime), 'manifest': str(runtime / MANIFEST),
            'inventory_sha256': document['inventory_sha256'], 'files': len(entries),
            'bytes': sum(item['size'] for item in entries), 'runtime_executed': False,
            'appcontainer_tested': False}


def verify_runtime(runtime):
    """Verify without self-reference; inventory SHA is policy.inventory_fingerprint compatible."""
    root = checked(runtime)
    path = root / MANIFEST
    record = file_record(path)
    if record['size'] > 16 * 1024**2:
        raise RuntimeBuildError('runtime manifest too large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeBuildError('duplicate manifest field')
            result[key] = value
        return result
    document = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if (document.get('schema') != 'lingxi.sandbox-runtime.v1' or document.get('worker_api') != 1
            or document.get('product') != NAME or document.get('runtime_executed') is not False
            or document.get('appcontainer_tested') is not False):
        raise RuntimeBuildError('unknown runtime manifest')
    # Skip only the exact own manifest, without removing or changing it.
    actual = inventory_runtime(root, allow_manifest=True)
    if actual != document.get('files') or fingerprint(actual) != document.get('inventory_sha256'):
        raise RuntimeBuildError('runtime files/hash inventory mismatch')
    if not any(item['relative_path'] == NAME + '.exe' for item in actual):
        raise RuntimeBuildError('runtime EXE missing')
    verify_pe_options(document, root / (NAME + '.exe'))
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--build-root', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--pe-version-file', type=Path, help='Optional explicit PyInstaller PE version input; pinned in plan')
    parser.add_argument('--build', action='store_true', help='Explicitly run compiler; default is read-only plan')
    args = parser.parse_args(argv)
    plan = plan_build(args.source_root, args.build_root, args.python, pe_version_file=args.pe_version_file)
    result = build(plan) if args.build else plan
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeBuildError, OSError, ValueError, KeyError, SyntaxError) as error:
        print('minimal sandbox-runtime build refused: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
