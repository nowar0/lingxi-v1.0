"""Production-only strict-worker inventory and create-only tree preparation.

Import has no filesystem/native effects. Read functions never create directories,
profiles, ACLs or workers. The build manifest is DATA: frozen hosts do not import a
packaging script. Creation helpers are callable only after the bridge has consumed
and checked the manager's exact authorizing snapshot. Unknown owners and created
objects are retained, never recursively removed, adopted or automatically retried.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat

from app.plugin_archive import scan_package
from app.plugin_sandbox_policy import InventoryEntry, canonical_local_path, inventory_fingerprint
from app.plugin_windows_observer import NativeObserverApi, ObservationLease, WindowsObserver, _ancestors
from app.plugin_native_provision import (capture_missing_scratch, create_run_root_once,
                                         root_creation_plan)

RUNTIME_MANIFEST = 'runtime-inventory.json'
WORKER_EXE = 'LingxiPluginWorker.exe'
RUNTIME_SCHEMA = 'lingxi.sandbox-runtime.v1'
MAX_OBJECTS = 50000
MAX_RUNTIME_BYTES = 256 * 1024**2 - 32 * 1024**2
MAX_MANIFEST_BYTES = 16 * 1024**2
_ALLOWED_APP = {'app', 'app.plugin_wire', 'app.plugin_sandbox_worker', 'app.plugin_mcp_sandbox_worker', 'app.plugin_stage_worker'}
_FORBIDDEN = {'keyring', 'numpy', 'onnxruntime', 'tokenizers', 'playwright', 'cryptography',
              'fastapi', 'uvicorn', 'openai', 'httpx', 'httpx2', 'mcp', 'asyncssh',
              'config', 'models', 'attachments', 'characters', 'plugin-packages'}


class PreparationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError('strict_runtime_duplicate_json_field')
        result[key] = value
    return result


def parse_json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              PreparationError('strict_runtime_nonfinite_json')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise PreparationError('strict_runtime_invalid_json') from error


def checked_local(value, *, directory=False):
    """Existing ordinary local source, including every actual ancestor."""
    text = os.fspath(value)
    canonical_local_path(text)
    path = Path(text)
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or not stat.S_ISDIR(info.st_mode) and item != path):
            raise PreparationError('strict_runtime_source_reparse_or_special_path')
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise PreparationError('strict_runtime_source_directory_required')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PreparationError('strict_runtime_ordinary_single_link_file_required')
    return path


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def file_record(path, *, limit=MAX_RUNTIME_BYTES, read=False):
    path = checked_local(path)
    before = path.lstat()
    if before.st_size > limit:
        raise PreparationError('strict_runtime_file_size_limit')
    hasher = hashlib.sha256()
    chunks = [] if read else None
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise PreparationError('strict_runtime_source_changed')
        remaining = before.st_size
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise PreparationError('strict_runtime_source_incomplete')
            hasher.update(block)
            if read:
                chunks.append(block)
            remaining -= len(block)
        if stream.read(1) or _identity(os.fstat(stream.fileno())) != _identity(before):
            raise PreparationError('strict_runtime_source_changed')
    if _identity(checked_local(path).lstat()) != _identity(before):
        raise PreparationError('strict_runtime_source_changed')
    record = {'sha256': hasher.hexdigest(), 'size': before.st_size}
    return (record, b''.join(chunks)) if read else record


def _names(entries):
    result = set()
    prefixes = {}
    for entry in entries:
        pieces = entry.relative_path.split('/')
        result.add(entry.relative_path)
        for index in range(1, len(pieces) + 1):
            prefix = '/'.join(pieces[:index])
            old = prefixes.setdefault(prefix.casefold(), prefix)
            if old != prefix:
                raise PreparationError('strict_runtime_case_alias')
            if index < len(pieces):
                result.add(prefix + '/')
    return tuple(sorted(result))


def _enumerate(root):
    names = []
    def failed(_error):
        raise PreparationError('strict_runtime_inventory_unreadable')
    for folder, directories, files in os.walk(root, followlinks=False, onerror=failed):
        for name in directories:
            path = checked_local(Path(folder) / name, directory=True)
            names.append(path.relative_to(root).as_posix() + '/')
        for name in files:
            path = checked_local(Path(folder) / name)
            names.append(path.relative_to(root).as_posix())
        if len(names) > MAX_OBJECTS:
            raise PreparationError('strict_runtime_inventory_object_limit')
    if len({name.casefold() for name in names}) != len(names):
        raise PreparationError('strict_runtime_case_alias')
    return tuple(sorted(names))


@dataclass(frozen=True)
class RuntimeInventory:
    root: str
    entries: tuple[InventoryEntry, ...]
    inventory_sha256: str
    manifest_entry: InventoryEntry
    manifest_sha256: str


def read_portable_runtime(runtime_source):
    """Verify actual portable runtime bytes; never execute or trust builder code.

    The exact own manifest is observed but is NOT payload. No optional third-party
    dependency, host module, DB, config, key or arbitrary data file is accepted.
    The inventory is integrity evidence, not a publisher signature or OS acceptance.
    """
    root = checked_local(runtime_source, directory=True)
    if root.name != 'plugin-runtime':
        raise PreparationError('dedicated_portable_plugin_runtime_required')
    record, raw = file_record(root / RUNTIME_MANIFEST, limit=MAX_MANIFEST_BYTES, read=True)
    document = parse_json(raw)
    if (type(document) is not dict or document.get('schema') != RUNTIME_SCHEMA
            or document.get('product') != 'LingxiPluginWorker'
            or type(document.get('worker_api')) is not int or document['worker_api'] != 1
            or document.get('runtime_executed') is not False
            or document.get('appcontainer_tested') is not False
            or document.get('worker_modes') != ['--package-mcp-server','--plugin-stage-worker','--plugin-worker']
            or type(document.get('python')) is not dict
            or type(document.get('python', {}).get('target')) is not dict
            or type(document.get('files')) is not list
            or not 1 <= len(document['files']) <= MAX_OBJECTS
            or not isinstance(document.get('host_version'), str)
            or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', document['host_version'])
            or not isinstance(document.get('build_plan_fingerprint'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', document['build_plan_fingerprint'])):
        raise PreparationError('strict_runtime_unknown_build_inventory')
    target = document['python']['target']
    if (set(target) != {'version', 'bits', 'machine'}
            or not isinstance(target['version'], str)
            or not re.fullmatch(r'3\.\d+\.\d+(?:[A-Za-z0-9.+-]*)', target['version'])
            or type(target['bits']) is not int or target['bits'] not in (32, 64)
            or not isinstance(target['machine'], str) or not 1 <= len(target['machine']) <= 100
            or not isinstance(document['python'].get('target_executable_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', document['python']['target_executable_sha256'])):
        raise PreparationError('strict_runtime_target_metadata_invalid')
    inputs = document.get('source_inputs')
    expected_inputs = {'app/__init__.py', 'app/plugin_sandbox_worker.py', 'app/plugin_wire.py',
                       'app/plugin_mcp_sandbox_worker.py','app/plugin_stage_worker.py',
                       'pyproject.toml', 'packaging/sandbox_worker_entry.py', 'packaging/build_sandbox_runtime.py'}
    if type(inputs) is not dict or set(inputs) != expected_inputs:
        raise PreparationError('strict_runtime_build_input_inventory_invalid')
    for record_input in inputs.values():
        if (type(record_input) is not dict or set(record_input) != {'sha256', 'size'}
                or type(record_input['sha256']) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', record_input['sha256'])
                or type(record_input['size']) is not int or record_input['size'] < 0):
            raise PreparationError('strict_runtime_build_input_inventory_invalid')
    # Optional compiler provenance is separate from the strict source key set.
    # Legacy no-version inventories remain valid under their original rules.
    if 'build_options' in document:
        options = document['build_options']
        if type(options) is not dict or set(options) != {'pe_version_file', 'pe_version_readback'}:
            raise PreparationError('strict_runtime_pe_build_options_invalid')
        pin, readback = options['pe_version_file'], options['pe_version_readback']
        if (type(pin) is not dict or set(pin) != {'name', 'version', 'size', 'sha256'}
                or type(pin.get('name')) is not str or not pin['name']
                or ntpath.basename(pin['name']) != pin['name'] or '/' in pin['name']
                or pin['name'] in {'.', '..'} or pin['name'].endswith((' ', '.'))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in pin['name'])
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', pin['name'])
                or pin.get('version') != document['host_version']
                or any(int(part) > 65535 for part in document['host_version'].split('.'))
                or type(pin.get('size')) is not int or pin['size'] < 1
                or type(pin.get('sha256')) is not str or not re.fullmatch('[0-9a-f]{64}', pin['sha256'])
                or type(readback) is not dict or set(readback) != {'numeric', 'strings'}
                or type(readback.get('numeric')) is not list
                or any(type(part) is not int for part in readback['numeric'])
                or readback['numeric'] != [*map(int, document['host_version'].split('.')), 0]
                or type(readback.get('strings')) is not list or not readback['strings']):
            raise PreparationError('strict_runtime_pe_version_pin_invalid')
        for values in readback['strings']:
            if (type(values) is not dict or set(values) != {'FileVersion', 'ProductVersion'}
                    or any(type(value) is not str or value not in {pin['version'], pin['version'] + '.0'}
                           for value in values.values())):
                raise PreparationError('strict_runtime_pe_version_readback_invalid')
    modules = document.get('analyzed_modules')
    if (type(modules) is not list or any(type(name) is not str for name in modules)
            or modules != sorted(set(modules)) or not _ALLOWED_APP <= set(modules)
            or any(name.split('.')[0] in _FORBIDDEN
                   or name.split('.')[0] == 'app' and name not in _ALLOWED_APP for name in modules)):
        raise PreparationError('strict_runtime_host_module_inventory_refused')
    entries = []
    total = 0
    for item in document['files']:
        if type(item) is not dict or set(item) != {'relative_path', 'sha256', 'size'}:
            raise PreparationError('strict_runtime_inventory_entry_invalid')
        entry = InventoryEntry(**item)
        name = entry.relative_path
        suffix = ntpath.splitext(name)[1].casefold()
        if (any(part.casefold() in _FORBIDDEN for part in name.split('/'))
                or not (name == WORKER_EXE or name.startswith('_internal/')
                        and (suffix in {'.dll', '.pyd'} or name == '_internal/base_library.zip'))):
            raise PreparationError('strict_runtime_unapproved_runtime_artifact')
        actual = file_record(root.joinpath(*name.split('/')))
        if actual != {'sha256': entry.sha256, 'size': entry.size}:
            raise PreparationError('strict_runtime_inventory_bytes_changed')
        with root.joinpath(*name.split('/')).open('rb') as stream:
            if stream.read(16) == b'SQLite format 3\x00':
                raise PreparationError('strict_runtime_database_payload_refused')
        entries.append(entry)
        total += entry.size
        if total > MAX_RUNTIME_BYTES:
            raise PreparationError('strict_runtime_prepared_payload_limit')
    entries = tuple(entries)
    actual_sha = inventory_fingerprint(entries)
    if (tuple(sorted(entries, key=lambda item: item.relative_path)) != entries
            or document.get('inventory_sha256') != actual_sha
            or not any(entry.relative_path == WORKER_EXE for entry in entries)):
        raise PreparationError('strict_runtime_inventory_binding_changed')
    metadata = InventoryEntry(RUNTIME_MANIFEST, record['sha256'], record['size'])
    inventory_fingerprint((*entries, metadata))
    if _enumerate(root) != _names((*entries, metadata)):
        raise PreparationError('strict_runtime_inventory_extra_or_missing_object')
    if file_record(root / RUNTIME_MANIFEST, limit=MAX_MANIFEST_BYTES) != record:
        raise PreparationError('strict_runtime_manifest_changed')
    return RuntimeInventory(str(root), entries, actual_sha, metadata, record['sha256'])


def read_code_inventory(package_root, manifest, digest_sha256):
    root = checked_local(package_root, directory=True)
    package = scan_package(root)
    if package.manifest != manifest or package.digest != digest_sha256:
        raise PreparationError('strict_runtime_package_binding_changed')
    entries = tuple(InventoryEntry(name, hashlib.sha256(blob).hexdigest(), len(blob))
                    for name, blob in sorted(package.blobs.items()))
    record, raw = file_record(root / 'manifest.json', limit=65536, read=True)
    if parse_json(raw) != manifest:
        raise PreparationError('strict_runtime_code_manifest_changed')
    entries = tuple(sorted((*entries, InventoryEntry('manifest.json', **record)),
                           key=lambda item: item.relative_path))
    inventory_fingerprint(entries)
    if _enumerate(root) != _names(entries):
        raise PreparationError('strict_runtime_package_extra_or_missing_object')
    return entries


def observe_source(root, entries):
    """Hold actual native readonly ancestors/directories/files and exact names."""
    canonical_local_path(root)
    inventory_fingerprint(entries)
    api = NativeObserverApi()
    lease = ObservationLease(api)
    lease._names = {}
    try:
        for directory in (*_ancestors(root), root):
            handle = lease._open(directory)
            evidence = lease._evidence(directory, handle)
            if evidence.kind != 'directory':
                raise PreparationError('strict_runtime_source_ancestor_not_directory')
            lease._files.append((handle, evidence))
        names = _names(entries)
        if tuple(sorted(api.enumerate_tree(root))) != names:
            raise PreparationError('strict_runtime_source_inventory_changed')
        for name in (name for name in names if name.endswith('/')):
            path = root + '\\' + name[:-1].replace('/', '\\')
            handle = lease._open(path)
            evidence = lease._evidence(path, handle)
            if evidence.kind != 'directory':
                raise PreparationError('strict_runtime_source_not_directory')
            lease._files.append((handle, evidence))
        for entry in entries:
            path = root + '\\' + entry.relative_path.replace('/', '\\')
            handle = lease._open(path, True)
            evidence = lease._evidence(path, handle, read=True)
            if evidence.kind != 'file' or evidence.sha256 != entry.sha256 or evidence.size != entry.size:
                raise PreparationError('strict_runtime_held_source_bytes_changed')
            lease._files.append((handle, evidence))
        lease._names[root] = names
        lease._directories[root] = inventory_fingerprint(entries)
        lease.revalidate()
        return lease
    except BaseException as error:
        try:
            lease.close()
        except BaseException:
            error.observation_cleanup = lease
        raise


class PreparationOwner:
    """In-process ownership; never a fake success receipt or deletion authority."""
    def __init__(self):
        self.leases = []
        self.streams = []
        self.prepared = None
        self.network_observer = None
        self.created_root = None
        self.created_paths = []
        self.creation_uncertain = False

    def retain(self, lease):
        if lease is not None and not any(item is lease for item in self.leases):
            self.leases.append(lease)
        return lease

    def absorb(self, error):
        seen=set();pending=[error]
        while pending:
            current=pending.pop()
            if id(current) in seen:continue
            seen.add(id(current))
            self.retain(getattr(current,'observation_cleanup',None))
            pending.extend(item for item in (current.__cause__,current.__context__) if item is not None)

    def close_streams(self):
        failed = []
        for stream in reversed(self.streams):
            try:
                stream.close()
                if not stream.closed:
                    failed.append(stream)
            except BaseException:
                failed.append(stream)
        self.streams = list(reversed(failed))
        return not self.streams

    def close(self):
        observer = self.network_observer
        api = getattr(observer, '_api', None) if observer is not None else None
        provider = getattr(api, '_loopback_provider', None) if api is not None else None
        integrity_owner=getattr(api,'_integrity_cleanup',None) if api is not None else None
        if integrity_owner is not None and not integrity_owner.closed:
            try:integrity_owner.close()
            except BaseException:return False
        pending = getattr(provider, 'pending_cleanup', None) if provider is not None else None
        if pending is not None:
            try:
                pending.close()
                if pending.closed is not True:
                    return False
            except BaseException:
                return False
        streams_closed = self.close_streams()
        if not streams_closed:
            return False  # Keep the identity/path leases while a writer is unknown.
        failed = []
        for lease in reversed(self.leases):
            if lease.closed:
                continue
            try:
                lease.close()
                if lease.closed is not True:
                    failed.append(lease)
            except BaseException:
                failed.append(lease)
        self.leases = list(reversed(failed))
        return not self.leases


def _hold_directory(owner, api, path):
    lease = ObservationLease(api)
    owner.retain(lease)  # Before the first native open can fail.
    handle = lease._open(path)
    evidence = lease._evidence(path, handle)
    if evidence.kind != 'directory':
        raise PreparationError('strict_runtime_created_path_not_directory')
    lease._files.append((handle, evidence))
    return lease


def prepare_tree(plan, *, approved_parent, creation_nonce, root_intent,
                 runtime_source, package_root, owner, authorized, cancel_event):
    """Prepare only the approved fresh tree; all names/nonces already exist in spec.

    No parent creation, preexisting destination adoption, runtime metadata copying,
    profile, ACL or worker execution occurs here. Collision consumes the attempt.
    Already created objects remain for explicit review after any ambiguous failure.
    """
    if (authorized is not True or type(owner) is not PreparationOwner
            or root_creation_plan(approved_parent=approved_parent, root=plan.paths.root,
                                  creation_nonce=creation_nonce) != root_intent):
        raise PreparationError('strict_runtime_exact_preparation_authority_required')
    parent = checked_local(approved_parent, directory=True)
    observer = WindowsObserver(plan.paths.root, observation_approved=authorized)
    owner.network_observer = observer
    if not observer.current_integrity().verified:
        raise PreparationError('strict_runtime_medium_required')
    api = observer.api
    parent_lease = ObservationLease(api)
    owner.retain(parent_lease)
    for directory in (*_ancestors(str(parent)), str(parent)):
        handle = parent_lease._open(directory)
        evidence = parent_lease._evidence(directory, handle)
        if evidence.kind != 'directory':
            raise PreparationError('strict_runtime_parent_not_directory')
        parent_lease._files.append((handle, evidence))

    def check():
        if cancel_event.is_set():
            raise PreparationError('strict_runtime_cancelled_during_preparation')
        parent_lease.revalidate()
        for lease in owner.leases:
            lease.revalidate()

    def refresh_parent(path):
        # Creating an approved child intentionally changes only its parent's
        # timestamps. Keep the held identity and every other attribute exact.
        parent_path = ntpath.dirname(path)
        for lease in owner.leases:
            for index, (handle, before) in enumerate(lease._files):
                if before.path != parent_path or before.kind != 'directory':
                    continue
                current = lease._evidence(before.path, handle)
                stable = lambda item: (item.path, item.kind, item.volume, item.file_id,
                                       item.attributes, item.links, item.final_path)
                if stable(current) != stable(before):
                    raise PreparationError('strict_runtime_created_parent_identity_changed')
                lease._files[index] = (handle, current)

    def create_root(path, nonce):
        check()
        if path != plan.paths.root or nonce != creation_nonce:
            raise PreparationError('strict_runtime_root_creation_changed')
        owner.creation_uncertain = True
        Path(path).mkdir(exist_ok=False)
        refresh_parent(path)
        owner.created_root = path
        owner.created_paths.append(path)
        _hold_directory(owner, api, path)
        owner.creation_uncertain = False

    check()
    receipt = create_run_root_once(root_intent, observer=observer, root_creator=create_root,
                                  user_confirmed=authorized, fingerprint=root_intent['fingerprint'])
    created = {plan.paths.root}

    def create_directory(path):
        if path in created:
            return
        if not path.casefold().startswith(plan.paths.root.casefold() + '\\'):
            raise PreparationError('strict_runtime_preparation_path_escape')
        parent_path = ntpath.dirname(path)
        if parent_path not in created:
            create_directory(parent_path)
        check()
        owner.creation_uncertain = True
        Path(path).mkdir(exist_ok=False)
        refresh_parent(path)
        owner.created_paths.append(path)
        _hold_directory(owner, api, path)
        created.add(path)
        owner.creation_uncertain = False

    # MissingScratchWitness needs its parent to exist but the call directory absent.
    for directory in (plan.paths.runtime, plan.paths.code, ntpath.dirname(plan.paths.scratch)):
        create_directory(directory)
    for source, destination, entries in ((runtime_source, plan.paths.runtime, plan.runtime_files),
                                          (package_root, plan.paths.code, plan.code_files)):
        for entry in entries:
            check()
            relative = entry.relative_path.replace('/', '\\')
            target = destination + '\\' + relative
            create_directory(ntpath.dirname(target))
            record = file_record(Path(source).joinpath(*entry.relative_path.split('/')),
                                 limit=max(entry.size, 1))
            if record != {'sha256': entry.sha256, 'size': entry.size}:
                raise PreparationError('strict_runtime_copy_source_changed')
            source_path = checked_local(Path(source).joinpath(*entry.relative_path.split('/')))
            hasher = hashlib.sha256()
            count = 0
            owner.creation_uncertain = True
            incoming = source_path.open('rb')
            owner.streams.append(incoming)
            try:
                outgoing = Path(target).open('xb')
                owner.streams.append(outgoing)
                owner.created_paths.append(target)
                while True:
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > entry.size or cancel_event.is_set():
                        raise PreparationError('strict_runtime_copy_cancelled_or_changed')
                    hasher.update(block)
                    if outgoing.write(block) != len(block):
                        raise PreparationError('strict_runtime_copy_write_incomplete')
                outgoing.flush()
                os.fsync(outgoing.fileno())
            finally:
                if not owner.close_streams():
                    raise PreparationError('strict_runtime_copy_stream_cleanup_unknown')
            refresh_parent(target)
            if count != entry.size or hasher.hexdigest() != entry.sha256:
                raise PreparationError('strict_runtime_copy_bytes_changed')
            if file_record(Path(target), limit=max(entry.size, 1)) != record:
                raise PreparationError('strict_runtime_copied_payload_unverified')
            owner.creation_uncertain = False
    check()
    witness = capture_missing_scratch(plan, approved_parent=approved_parent,
        creation_nonce=creation_nonce, observer=observer, observation_approved=authorized,
        root_creation_receipt=receipt)
    owner.retain(witness.lease)

    def create_scratch(path, nonce):
        if path != plan.paths.scratch or nonce != creation_nonce:
            raise PreparationError('strict_runtime_scratch_creation_changed')
        # The witness owns a missing-path observation; it validates immediately
        # before this callback. Do not revalidate it AFTER intentional creation.
        if cancel_event.is_set():
            raise PreparationError('strict_runtime_cancelled_before_scratch')
        parent_lease.revalidate()
        owner.creation_uncertain = True
        Path(path).mkdir(exist_ok=False)
        refresh_parent(path)
        owner.created_paths.append(path)
        owner.creation_uncertain = False

    prepared = witness.prepare(create_scratch, user_confirmed=authorized,
                               fingerprint=witness.fingerprint)
    owner.prepared = prepared
    owner.retain(prepared.lease)
    # Its missing-path assertion has served its one purpose. The new lease owns
    # and observes the created scratch and all payload identities instead.
    witness.lease.close()
    return prepared, observer
