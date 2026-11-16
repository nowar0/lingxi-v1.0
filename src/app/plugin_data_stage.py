"""Host-only, two-confirmation migration A candidate service.

Import/construction/preview never create a profile, process, directory or journal.
Only explicit A may register immutable candidate code and a new stage generation;
active code/data/config/state are never published here. B consumes prepared() via
HostPluginDataApi. Nothing in this module is a browser-selected executor/receipt.

Production execution requires NativeStageExecutor backed by the connected native
strict runtime, with held token/Job/channel query providers. There is no subprocess,
ordinary-worker, synthetic-attestation or unknown-schema-as-zero fallback.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from uuid import uuid4

from app.plugin_archive import scan_package
from app.plugin_manifest import canonical_json, validate_id, validate_data_migrations
from app.plugin_migrations import (ActivationTuple, CompatibilityAudit, InventoryFile,
    MigrationPlan, StageResult, inventory_fingerprint, safe_document,
    MAX_DATA_BYTES, MAX_FILES, PERMISSIONS)
from app.plugin_data_transactions import (DataTransactionError, checked, inspect_tree,
    _held_file, _identity, _relative)
from app.plugin_wire import encode_json

CHUNK = 12000
MAX_REQUESTS = 100000
MAX_WIRE_BYTES = 1024 * 1024 * 1024
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r'^[0-9a-f]{64}$')
_OPAQUE = re.compile(r'^[A-Za-z0-9_-]{32,100}$')


class StageError(DataTransactionError):
    def __init__(self, code):
        super().__init__(code); self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(value if type(value) is bytes else _json(value)).hexdigest()


def _strict(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise StageError('stage_exact_fields_required')


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise StageError('stage_bounded_integer_required')
    return value


def _sha(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise StageError('stage_sha256_required')
    return value


def _opaque(value):
    if type(value) is not str or not _OPAQUE.fullmatch(value):
        raise StageError('stage_server_id_required')
    return value


def _ordinary(path, *, directory=False):
    path = checked(path); info = path.lstat()
    if ((directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
        raise StageError('stage_ordinary_object_required')
    return info


@contextmanager
def _parents_held(path):
    """Prevent ancestor replacement while performing host-controlled leaf IO."""
    path = checked(path)
    with ExitStack() as stack:
        for ancestor in reversed(path.parents):
            _ordinary(ancestor, directory=True)
            stack.enter_context(_held_file(ancestor, directory=True))
        yield path


def _mkdirs(path):
    path = checked(path)
    if path.exists():
        _ordinary(path, directory=True); return
    parent = path.parent
    if parent == path:
        raise StageError('stage_directory_parent_missing')
    _mkdirs(parent)
    with _parents_held(path):
        path.mkdir(exist_ok=False)
        _ordinary(path, directory=True)


def _fsync_directory(path):
    # On Windows held directory handles prevent namespace replacement. File fsync
    # is mandatory; the native filesystem adapter must provide power-loss semantics
    # beyond the OS's successful flush/atomic-replace contract if required.
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_json(path, body, *, create=False):
    raw = _json(body)
    if len(raw) > MAX_RECORD_BYTES:
        raise StageError('stage_journal_too_large')
    path = checked(path); _mkdirs(path.parent)
    with _parents_held(path):
        if path.exists():
            if create:
                raise StageError('stage_journal_already_exists')
            _ordinary(path)
        temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(16) + '.tmp')
        with temporary.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        # No automatic deletion/recovery if replace fails: preserve ownership evidence.
        os.replace(temporary, path); _fsync_directory(path.parent)


def _read_record(path):
    _ordinary(path)
    with _parents_held(path), _held_file(path) as stream:
        before = os.fstat(stream.fileno()); raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES or _identity(before) != _identity(os.fstat(stream.fileno())):
            raise StageError('stage_journal_changed')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StageError('stage_journal_duplicate')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(StageError('stage_journal_nonfinite')))
    if _json(value) != raw or type(value) is not dict:
        raise StageError('stage_journal_not_canonical')
    return value


@dataclass(frozen=True, eq=False)
class HostCompatibilityApproval:
    """Owner-identity capability. A browser dictionary/dataclass copy is not valid."""
    _owner: object = field(repr=False)
    _token: str = field(repr=False)
    audit: CompatibilityAudit
    reviewer: str
    evidence_sha: str


class HostCompatibilityAuthority:
    """Explicit host/manual audit authority, not an OS attestation adapter.

    validate_evidence(old, target_digest, resolved_schema, evidence, reviewer) must
    independently approve a real review. Never wire it to bool(browser_dict). The
    UI must show exact old tuple/inventory/config+state hashes, target manifest/code
    hashes, migration declaration and the review evidence before a separate human
    audit confirmation. No schema is inferred from plugin/app SemVer or missing data.
    """
    def __init__(self, *, validate_evidence):
        if not callable(validate_evidence):
            raise TypeError('trusted compatibility evidence validator required')
        self._validate = validate_evidence; self._issued = {}; self._lock = threading.RLock()

    def approve(self, old, target_code_digest, resolved_schema, *, evidence, reviewer, user_confirmed):
        if type(old) is not ActivationTuple or old.schema is not None or user_confirmed is not True:
            raise StageError('explicit_unknown_schema_audit_required')
        _sha(target_code_digest); _integer(resolved_schema, 0, 2147483647)
        if type(reviewer) is not str or not 1 <= len(reviewer) <= 200 or type(evidence) is not dict or len(_json(evidence)) > 65536:
            raise StageError('bounded_named_compatibility_review_required')
        if self._validate(old, target_code_digest, resolved_schema, json.loads(_json(evidence)), reviewer) is not True:
            raise StageError('compatibility_evidence_not_approved')
        evidence_sha = _hash({'reviewer': reviewer, 'evidence': evidence})
        audit = CompatibilityAudit(_hash(asdict(old)), resolved_schema, target_code_digest, evidence_sha)
        with self._lock:
            if len(self._issued) >= 128:
                raise StageError('compatibility_capacity')
            result = HostCompatibilityApproval(self, secrets.token_urlsafe(32), audit, reviewer, evidence_sha)
            self._issued[result._token] = result
            return result

    def resolve(self, capability, old, target_code_digest):
        with self._lock:
            if (type(capability) is not HostCompatibilityApproval or capability._owner is not self
                    or self._issued.get(capability._token) is not capability
                    or capability.audit.old_tuple_sha != _hash(asdict(old))
                    or capability.audit.target_code_digest != target_code_digest):
                raise StageError('host_compatibility_capability_required')
            return capability.audit


@dataclass(frozen=True, eq=False)
class _HostStageApproval:
    """Consumed A capability; identity in the service registry is the authority.

    This object is never serialized, returned by an endpoint, or reconstructed from
    an approval snapshot. Possessing an equal-looking dictionary is insufficient.
    """
    _owner: object = field(repr=False)
    _token: str = field(repr=False)
    fingerprint: str
    snapshot_json: bytes = field(repr=False)


@dataclass(frozen=True)
class StageExecutionRequest:
    """Host-only request; native adapter must bind all fields before package import."""
    plan: MigrationPlan
    manifest_json: bytes = field(repr=False)
    migration_json: bytes = field(repr=False)
    code_root: str
    candidate_id: str
    call_id: str
    native_spec_json: bytes = field(repr=False, default=b'{}')
    worker_mode: str = '--plugin-stage-worker'

    def __post_init__(self):
        if type(self.plan) is not MigrationPlan or self.worker_mode != '--plugin-stage-worker':
            raise StageError('stage_native_request_required')
        _opaque(self.candidate_id); _opaque(self.call_id)
        if not self.call_id.startswith('stage_'):
            raise StageError('stage_host_call_binding_required')
        native_spec = json.loads(self.native_spec_json)
        if type(native_spec) is not dict or _json(native_spec) != self.native_spec_json:
            raise StageError('stage_native_policy_preview_required')
        manifest = json.loads(self.manifest_json); migration = json.loads(self.migration_json)
        if (_json(manifest) != self.manifest_json or _json(migration) != self.migration_json
                or _hash(manifest) != self.plan.target_code_digest
                or manifest.get('id') != self.plan.plugin_id or manifest.get('data_schema') != self.plan.target_schema
                or migration not in manifest.get('migrations', [])
                or migration.get('entrypoint', {}).get('path') != self.plan.hook_path
                or manifest.get('files', {}).get(self.plan.hook_path) != self.plan.hook_sha):
            raise StageError('stage_native_request_binding')

    def init(self, *, session):
        _opaque(session)
        migration = json.loads(self.migration_json)
        value = {'v': 1, 'type': 'stage_init', 'plugin_id': self.plan.plugin_id,
                 'revision': self.plan.expected_revision, 'digest': self.plan.target_code_digest,
                 'call_id': self.call_id, 'request_id': self.candidate_id,
                 'plan_fingerprint': self.plan.fingerprint, 'session': session,
                 'package_root': self.code_root, 'manifest': json.loads(self.manifest_json),
                 'migration': migration, 'source_schema': migration['from_schema'],
                 'target_schema': self.plan.target_schema, 'stage_generation': self.plan.stage_generation,
                 'permissions': list(PERMISSIONS)}
        encode_json(value)  # Fail closed BEFORE launch for an oversized worker init.
        return value


class StageDataProxy:
    """Bounded migration-only host file broker; old path never appears on the wire.

    The native runtime must invoke handle() only from the attested held channel and
    query its process identity before/after every request. bind() is host-only. This
    proxy additionally checks the live manager/old tuple/epochs/trust/ownership before
    and after IO. It offers no network/model/general filesystem/config host operation.
    """
    def __init__(self, request, *, old_root, stage_root, temporary_root, config_json,
                 state_json, assert_current, host_lock, clock=time.monotonic):
        if (type(request) is not StageExecutionRequest or not callable(assert_current)
                or not callable(getattr(host_lock, '__enter__', None))):
            raise TypeError('bound host migration request required')
        self.request = request; self.plan = request.plan
        self.old_root, self.stage_root, self.temporary_root = map(checked, (old_root, stage_root, temporary_root))
        self.config_json = safe_document(json.loads(config_json)); self.state_json = safe_document(json.loads(state_json))
        if self.config_json != config_json or self.state_json != state_json:
            raise StageError('stage_canonical_source_documents_required')
        if _hash(config_json) != self.plan.current.config_sha or _hash(state_json) != self.plan.current.state_sha:
            raise StageError('stage_source_document_binding')
        self._assert_current = assert_current; self._host_lock = host_lock; self.clock = clock
        self._session = None; self._sequence = 0; self._deadline = None; self._cancel = None
        self._read_bytes = 0; self._write_bytes = 0; self._wire_bytes = 0
        self._uploads = {}; self._documents = {}; self._finished = None
        self._held = ExitStack(); self._lock = threading.RLock(); self._closed = False
        self._source = {item.path: item for item in self.plan.source_files}
        self._files = {}; self._stage_root_identity = None; self._source_handles = {}

    def __enter__(self):
        try:
            return self._enter_held()
        except BaseException:
            self.close()
            raise

    def _enter_held(self):
        for root in (self.old_root, self.stage_root, self.temporary_root):
            for parent in (*reversed(root.parents), root):
                if parent.exists():
                    self._held.enter_context(_held_file(parent, directory=True))
        self._stage_root_identity = _identity(_ordinary(self.stage_root, directory=True))[:2]
        files, digest, _ = inspect_tree(self.old_root, missing=True)
        if files != self.plan.source_files or digest != self.plan.current.data_inventory_sha:
            raise StageError('stage_old_inventory_changed')
        for item in files:
            path = self._path(item.path)
            stream = self._held.enter_context(_held_file(path))
            info = os.fstat(stream.fileno())
            if info.st_size != item.size or _identity(info) != _identity(_ordinary(path)):
                raise StageError('stage_old_handle_identity_changed')
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item.sha256:
                raise StageError('stage_old_handle_content_changed')
            self._source_handles[item.path] = (stream, _identity(info))
        if any(self.stage_root.iterdir()) or any(self.temporary_root.iterdir()):
            raise StageError('stage_new_empty_generation_required')
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self._lock:
            self._closed = True; self._session = None
            failures = []
            for upload in self._uploads.values():
                try:
                    upload['stream'].close()
                except BaseException:
                    failures.append('upload')
            try:
                self._held.close()
            except BaseException:
                failures.append('held_paths')
            if failures:
                raise StageError('stage_proxy_cleanup_unconfirmed')

    def bind(self, *, session, deadline, cancelled):
        """Called by native runtime only AFTER held child/Job/channel observation."""
        _opaque(session)
        if (self._session is not None or self._closed or type(deadline) not in (int, float)
                or not math.isfinite(deadline) or not 0 < deadline - self.clock() <= 10
                or not callable(getattr(cancelled, 'is_set', None))):
            raise StageError('stage_native_binding_required')
        self._session, self._deadline, self._cancel = session, deadline, cancelled
        return self.request.init(session=session)

    def _live(self):
        if (self._closed or self._session is None or self._cancel.is_set()
                or self.clock() >= self._deadline or self._finished is not None):
            raise StageError('stage_proxy_not_live')
        self._assert_current()
        if _identity(_ordinary(self.stage_root, directory=True))[:2] != self._stage_root_identity:
            raise StageError('stage_generation_identity_changed')

    def _path(self, name, *, stage=False):
        _relative(name)
        return checked((self.stage_root if stage else self.old_root).joinpath(*name.split('/')))

    def _read(self, name, offset, length, *, stage=False):
        _integer(offset, 0, MAX_DATA_BYTES); _integer(length, 1, CHUNK)
        item = (self._files if stage else self._source).get(name)
        if item is None:
            raise StageError('stage_file_not_in_bound_inventory')
        path = self._path(name, stage=stage)
        if not stage:
            stream, identity = self._source_handles[name]
            if identity != _identity(os.fstat(stream.fileno())) or identity != _identity(_ordinary(path)):
                raise StageError('stage_read_identity_changed')
            stream.seek(offset); content = stream.read(min(length, max(0, item.size - offset)))
            if identity != _identity(os.fstat(stream.fileno())):
                raise StageError('stage_read_identity_changed')
        else:
            with _parents_held(path), _held_file(path) as stream:
                before = os.fstat(stream.fileno()); leaf = _ordinary(path)
                if _identity(before) != _identity(leaf) or before.st_size != item.size:
                    raise StageError('stage_read_identity_changed')
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item.sha256:
                    raise StageError('stage_read_content_changed')
                stream.seek(offset); content = stream.read(min(length, max(0, item.size - offset)))
                if _identity(before) != _identity(os.fstat(stream.fileno())) or _identity(before) != _identity(path.lstat()):
                    raise StageError('stage_read_identity_changed')
        self._read_bytes += len(content)
        if self._read_bytes > 2 * MAX_DATA_BYTES:
            raise StageError('stage_read_io_budget')
        return {'content_base64': base64.b64encode(content).decode('ascii'),
                'sha256': _hash(content), 'eof': offset + len(content) >= item.size}

    def _begin(self, name):
        _relative(name)
        if len(self._uploads) >= 1:
            raise StageError('stage_only_one_pending_atomic_write')
        # Validate all spelling/ancestor collisions before creating even a temporary.
        prospective = {path: item for path, item in self._files.items() if path != name}
        prospective[name] = InventoryFile(name, _hash(b''), 0, True, False, 1, True, False)
        inventory_fingerprint(tuple(prospective.values()))
        _, _, observation = inspect_tree(self.stage_root)
        entries = set(observation['entries'])
        parts = name.split('/')
        entries.update('/'.join(parts[:index]) for index in range(1, len(parts) + 1))
        if len(entries) > MAX_FILES:
            raise StageError('stage_filesystem_entry_limit')
        path = self._path(name, stage=True)
        if path.exists() and name not in self._files:
            raise StageError('stage_unregistered_output_exists')
        token = secrets.token_urlsafe(32); temporary = checked(self.temporary_root / token)
        with _parents_held(temporary):
            stream = temporary.open('xb')
        self._uploads[token] = {'path': name, 'temporary': temporary, 'stream': stream,
                                'digest': hashlib.sha256(), 'size': 0}
        return {'upload': token}

    def _append(self, upload, encoded):
        item = self._uploads.get(_opaque(upload))
        if item is None or type(encoded) is not str:
            raise StageError('stage_atomic_upload_unknown')
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise StageError('stage_content_invalid') from error
        if len(content) > CHUNK or base64.b64encode(content).decode('ascii') != encoded:
            raise StageError('stage_content_chunk_limit')
        new_size = item['size'] + len(content)
        total = sum(x.size for name, x in self._files.items() if name != item['path']) + new_size
        self._write_bytes += len(content)
        if total > MAX_DATA_BYTES or self._write_bytes > MAX_DATA_BYTES:
            raise StageError('stage_write_io_or_extent_limit')
        stream = item['stream']; info = os.fstat(stream.fileno())
        if info.st_nlink != 1 or info.st_size != item['size'] or _identity(info) != _identity(_ordinary(item['temporary'])):
            raise StageError('stage_atomic_temporary_changed')
        if stream.write(content) != len(content):
            raise StageError('stage_atomic_short_write')
        stream.flush(); item['digest'].update(content); item['size'] = new_size
        return {'written': len(content)}

    def _commit(self, upload, size, digest):
        item = self._uploads.get(_opaque(upload)); _integer(size, 0, MAX_DATA_BYTES); _sha(digest)
        if item is None or item['size'] != size or item['digest'].hexdigest() != digest:
            raise StageError('stage_atomic_content_binding')
        stream = item['stream']; stream.flush(); os.fsync(stream.fileno())
        temporary = item['temporary']; info = os.fstat(stream.fileno())
        if _identity(info) != _identity(_ordinary(temporary)) or info.st_size != size:
            raise StageError('stage_atomic_temporary_changed')
        stream.close(); path = self._path(item['path'], stage=True); _mkdirs(path.parent)
        with _parents_held(path), _parents_held(temporary):
            if path.exists():
                previous = self._files.get(item['path'])
                if previous is None or _ordinary(path).st_size != previous.size:
                    raise StageError('stage_atomic_destination_changed')
                with _held_file(path) as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != previous.sha256:
                        raise StageError('stage_atomic_destination_changed')
            os.replace(temporary, path); _fsync_directory(path.parent)
            _ordinary(path)
        files, _, _ = inspect_tree(self.stage_root)
        actual = next((x for x in files if x.path == item['path']), None)
        if actual is None or actual.sha256 != digest or actual.size != size:
            raise StageError('stage_atomic_publication_unconfirmed')
        expected = {name: value for name, value in self._files.items() if name != item['path']}
        expected[item['path']] = actual
        if tuple(sorted(expected.values(), key=lambda x: x.path)) != files:
            raise StageError('stage_atomic_other_output_changed')
        self._files = expected; self._uploads.pop(upload)
        return {'path': actual.path, 'size': actual.size, 'sha256': actual.sha256}

    def _delete(self, name):
        item = self._files.get(name)
        if item is None:
            raise StageError('stage_delete_unknown_file')
        path = self._path(name, stage=True)
        with _parents_held(path):
            with _held_file(path) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item.sha256:
                    raise StageError('stage_delete_content_changed')
            _ordinary(path); path.unlink(); _fsync_directory(path.parent)
        self._files.pop(name)
        return {'deleted': True}

    def _dispatch(self, op, params):
        if op in ('old.list', 'stage.list'):
            _strict(params, ('cursor',)); cursor = _integer(params['cursor'], 0, MAX_FILES)
            files = sorted((self._files if op == 'stage.list' else self._source).values(), key=lambda x: x.path)
            if cursor > len(files):
                raise StageError('stage_list_cursor')
            page = files[cursor:cursor + 32]; end = cursor + len(page)
            return {'files': [{'path': x.path, 'sha256': x.sha256, 'size': x.size} for x in page],
                    'next': end if end < len(files) else None}
        if op in ('old.read', 'stage.read'):
            _strict(params, ('path', 'offset', 'length'))
            return self._read(params['path'], params['offset'], params['length'], stage=op == 'stage.read')
        if op == 'stage.begin':
            _strict(params, ('path',)); return self._begin(params['path'])
        if op == 'stage.append':
            _strict(params, ('upload', 'content_base64')); return self._append(params['upload'], params['content_base64'])
        if op == 'stage.commit':
            _strict(params, ('upload', 'size', 'sha256')); return self._commit(params['upload'], params['size'], params['sha256'])
        if op == 'stage.delete':
            _strict(params, ('path',)); return self._delete(params['path'])
        if op == 'document.read':
            _strict(params, ('kind', 'offset')); kind = params['kind']; offset = _integer(params['offset'], 0, 65536)
            if kind not in ('config', 'state'):
                raise StageError('stage_document_kind')
            raw = getattr(self, kind + '_json'); content = raw[offset:offset + CHUNK]
            return {'content_base64': base64.b64encode(content).decode('ascii'), 'eof': offset + len(content) >= len(raw)}
        if op == 'document.begin':
            _strict(params, ('kind',)); kind = params['kind']
            if kind not in ('config', 'state') or kind in self._documents:
                raise StageError('stage_document_kind_or_pending')
            self._documents[kind] = bytearray(); return {'begun': True}
        if op == 'document.append':
            _strict(params, ('kind', 'content_base64')); kind = params['kind']
            if kind not in self._documents or type(params['content_base64']) is not str:
                raise StageError('stage_document_not_pending')
            content = base64.b64decode(params['content_base64'], validate=True)
            if len(content) > CHUNK or len(self._documents[kind]) + len(content) > 65536:
                raise StageError('stage_document_bytes_limit')
            self._documents[kind].extend(content); return {'written': len(content)}
        if op == 'document.commit':
            _strict(params, ('kind', 'sha256')); kind = params['kind']; _sha(params['sha256'])
            if kind not in self._documents:
                raise StageError('stage_document_not_pending')
            raw = bytes(self._documents[kind])
            if _hash(raw) != params['sha256'] or safe_document(json.loads(raw)) != raw:
                raise StageError('stage_safe_canonical_document_required')
            setattr(self, kind + '_json', raw); self._documents.pop(kind)
            return {'sha256': _hash(raw)}
        if op == 'stage.finish':
            _strict(params, ())
            if self._uploads or self._documents:
                raise StageError('stage_unfinished_atomic_write')
            files, digest, evidence = inspect_tree(self.stage_root)
            if files != tuple(sorted(self._files.values(), key=lambda x: x.path)):
                raise StageError('stage_complete_inventory_changed')
            self._finished = {'files': files, 'inventory_sha': digest, 'observation_sha': _hash(evidence),
                              'config_json': self.config_json, 'state_json': self.state_json}
            return {'plan_fingerprint': self.plan.fingerprint, 'data_generation': self.plan.stage_generation,
                    'schema': self.plan.target_schema, 'inventory_sha': digest,
                    'config_sha': _hash(self.config_json), 'state_sha': _hash(self.state_json)}
        raise StageError('migration_operation_not_allowed')

    def handle(self, message):
        """Exactly one correlated request; any error permanently revokes this proxy."""
        # Keep the manager's lifecycle/trust/current tuple stable across each
        # actual broker IO, not merely at two disconnected observations. Native
        # process/cancellation/cleanup still runs outside this short host lock.
        with self._host_lock, self._lock:
            try:
                self._live()
                _strict(message, ('v', 'type', 'session', 'plan_fingerprint', 'seq', 'request_id', 'op', 'params'))
                _opaque(message['request_id'])
                if (type(message['v']) is not int or message['v'] != 1 or message['type'] != 'stage_rpc'
                        or message['session'] != self._session or message['plan_fingerprint'] != self.plan.fingerprint
                        or type(message['seq']) is not int or message['seq'] != self._sequence + 1
                        or self._sequence >= MAX_REQUESTS):
                    raise StageError('stage_request_binding')
                self._sequence += 1; self._wire_bytes += len(encode_json(message))
                if self._wire_bytes > MAX_WIRE_BYTES:
                    raise StageError('stage_wire_budget')
                result = self._dispatch(message['op'], message['params'])
                self._assert_current()
                if self._cancel.is_set() or self.clock() >= self._deadline:
                    raise StageError('stage_cancelled_or_deadline')
                response = {'v': 1, 'type': 'stage_reply', 'seq': message['seq'],
                            'request_id': message['request_id'], 'ok': True, 'result': result}
                self._wire_bytes += len(encode_json(response))
                if self._wire_bytes > MAX_WIRE_BYTES:
                    raise StageError('stage_wire_budget')
                return response
            except BaseException:
                self._closed = True; self._session = None
                raise

    def completed_result(self, receipt_sha):
        """Only after native runtime has independently confirmed full child cleanup."""
        _sha(receipt_sha)
        if self._finished is None or self._uploads or self._documents:
            raise StageError('stage_hook_did_not_finish')
        self._assert_current()
        old_files, old_sha, _ = inspect_tree(self.old_root, missing=True)
        files, digest, evidence = inspect_tree(self.stage_root)
        if (old_files != self.plan.source_files or old_sha != self.plan.current.data_inventory_sha
                or files != self._finished['files'] or digest != self._finished['inventory_sha']
                or _hash(evidence) != self._finished['observation_sha']):
            raise StageError('stage_old_or_output_changed_after_execution')
        return StageResult(self.plan.fingerprint, self.plan.stage_generation, self.plan.target_schema,
                           self.config_json, self.state_json, files, receipt_sha, False)


class NativeStageExecutor:
    """Actual host adapter to the native strict runtime's held stage session.

    PluginStrictRuntime.open_stage(request, proxy=..., cancelled=...) must return
    an OWNER with query_stage_identity(), run_stage(init_factory, handler), close()
    and cleanup_confirmed(). query_stage_identity must make live native queries on
    held process/token/Job/channel resources; it must not deserialize worker JSON.
    run_stage must enforce the original absolute 10-second resume deadline, bind
    only the migration worker, query identity around every handler call, and bind
    init_factory(session=..., deadline=..., cancelled=...) before package import.
    A missing stage-session factory remains explicitly unavailable; the adapter
    never manufactures SID/PID, a successful cleanup flag or an OS receipt.
    """
    def __init__(self, runtime):
        self.runtime = runtime; self._quarantined = {}; self._lock = threading.RLock()

    def available(self):
        try:
            from app.plugin_strict_runtime import PluginStrictRuntime
        except ImportError:
            return False
        return (type(self.runtime) is PluginStrictRuntime
                and callable(getattr(self.runtime, 'open_stage', None))
                and callable(getattr(self.runtime, 'stage_available', None))
                and callable(getattr(self.runtime, 'preview_stage_spec', None))
                and callable(getattr(self.runtime, 'validate_stage_spec', None))
                and self.runtime.stage_available() is True)

    def preview_spec(self, *, host_snapshot):
        if not self.available():
            return None
        spec = self.runtime.preview_stage_spec(host_snapshot=json.loads(_json(host_snapshot)))
        if (type(spec) is not dict or not spec or len(_json(spec)) > 262144
                or spec.get('worker_mode') != '--plugin-stage-worker'
                or spec.get('fallback_allowed') is not False):
            raise StageError('stage_native_policy_preview_invalid')
        return json.loads(_json(spec))

    @staticmethod
    def _query(owner, plan):
        from app.plugin_migrations import ExecutorAttestation
        value = owner.query_stage_identity()
        if (type(value) is not ExecutorAttestation or value.source != 'host_os_query'
                or value.plan_fingerprint != plan.fingerprint
                or type(value.pid) is not int or value.pid <= 0
                or type(value.sandbox_sid) is not str
                or not re.fullmatch(r'S-1-15-2-[0-9]+(?:-[0-9]+)*', value.sandbox_sid)
                or type(value.memory_bytes) is not int or value.memory_bytes != 256 * 1024**2
                or type(value.cpu_rate) is not int or value.cpu_rate != 2000
                or type(value.active_processes) is not int or value.active_processes != 1
                or type(value.wall_seconds) is not int or value.wall_seconds != 10
                or type(value.permissions) is not tuple or value.permissions != PERMISSIONS
                or type(value.network_capabilities) is not tuple or value.network_capabilities != ()
                or value.job_verified is not True or value.token_verified is not True):
            raise StageError('stage_held_native_identity_not_attested')
        return value

    def execute(self, request, *, proxy, cancelled, owner_registered):
        if not self.available() or type(request) is not StageExecutionRequest or type(proxy) is not StageDataProxy:
            raise StageError('strict_migration_executor_unavailable_no_fallback')
        if proxy.request is not request or cancelled.is_set() or not callable(owner_registered):
            raise StageError('stage_native_request_or_cancellation_changed')
        if (not json.loads(request.native_spec_json)
                or self.runtime.validate_stage_spec(json.loads(request.native_spec_json), request=request) is not True):
            raise StageError('stage_approved_native_policy_changed')
        owner = None; before = None; after = None; outcome = None; failure = None
        try:
            owner = self.runtime.open_stage(request, proxy=proxy, cancelled=cancelled)
            if not all(callable(getattr(owner, name, None)) for name in
                       ('query_stage_identity', 'run_stage', 'close', 'cleanup_confirmed')):
                raise StageError('stage_native_owner_incomplete')
            with self._lock:
                self._quarantined[request.call_id] = owner
            owner_registered(owner)
            before = self._query(owner, request.plan)
            outcome = owner.run_stage(init_factory=proxy.bind, handler=proxy.handle)
            after = self._query(owner, request.plan)
            if after != before:
                raise StageError('stage_native_process_identity_changed')
            _strict(outcome, ('v', 'type', 'call_id', 'request_id', 'ok', 'result'))
            if (type(outcome['v']) is not int or outcome['v'] != 1 or outcome['type'] != 'stage_result'
                    or outcome['call_id'] != request.call_id or outcome['request_id'] != request.candidate_id
                    or outcome['ok'] is not True or proxy._finished is None or cancelled.is_set()):
                raise StageError('stage_worker_completion_unconfirmed')
            expected = {'plan_fingerprint': request.plan.fingerprint,
                        'data_generation': request.plan.stage_generation, 'schema': request.plan.target_schema,
                        'inventory_sha': proxy._finished['inventory_sha'],
                        'config_sha': _hash(proxy.config_json), 'state_sha': _hash(proxy.state_json)}
            if outcome['result'] != expected:
                raise StageError('stage_worker_result_not_host_bound')
        except BaseException as error:
            failure = error
            # Native failure must retain any partial resource owner on the exception.
            if owner is None and getattr(error, 'stage_owner', None) is not None:
                owner = error.stage_owner
                with self._lock:
                    self._quarantined[request.call_id] = owner
                owner_registered(owner)
        finally:
            if owner is not None:
                try:
                    owner.close()
                    if owner.cleanup_confirmed() is not True:
                        raise StageError('stage_native_cleanup_unconfirmed')
                    with self._lock:
                        self._quarantined.pop(request.call_id, None)
                except BaseException:
                    failure = StageError('stage_native_cleanup_unconfirmed')
        if failure is not None:
            raise failure
        receipt_sha = _hash({'plan_fingerprint': request.plan.fingerprint,
                             'held_observation_before': asdict(before), 'held_observation_after': asdict(after),
                             'worker_result': outcome, 'cleanup': 'confirmed_by_held_native_owner'})
        # This digest is an integrity binding derived after live native queries; it
        # is not presented as an independent signed or OS-verifiable certificate.
        return proxy.completed_result(receipt_sha)

    def pending_cleanup(self):
        with self._lock:
            return tuple(self._quarantined)

    def retry_cleanup(self, call_id):
        with self._lock:
            owner = self._quarantined.get(call_id)
        if owner is None:
            raise StageError('stage_cleanup_owner_unknown')
        owner.close()
        if owner.cleanup_confirmed() is not True:
            raise StageError('stage_native_cleanup_unconfirmed')
        with self._lock:
            self._quarantined.pop(call_id, None)
        return {'cleanup_confirmed': True, 'migration_replayed': False}


class HostPluginStageService:
    """Persisted candidate entry used by a host/API bridge, never exposed as IPC.

    Keep one service per manager ownership lifetime. The manager must exclude its
    pending_candidates() from enable/delete/backup and retain candidate_code_references()
    during startup cleanup. Interrupted records are review-only: no automatic hook
    restart, data publication, deletion or rollback is performed by this service.
    """
    def __init__(self, manager_provider, *, strict_executor=None, compatibility_authority=None,
                 clock=time.monotonic, ttl=120):
        if (not callable(manager_provider) or not callable(clock)
                or type(ttl) not in (float, int) or not math.isfinite(ttl) or not 0 < ttl <= 300
                or strict_executor is not None and type(strict_executor) is not NativeStageExecutor
                or compatibility_authority is not None and type(compatibility_authority) is not HostCompatibilityAuthority):
            raise TypeError('explicit host-only stage capabilities required')
        self.manager_provider, self.executor = manager_provider, strict_executor
        self.compatibility_authority, self.clock, self.ttl = compatibility_authority, clock, ttl
        self._pending = {}; self._records = {}; self._validated_owners = {}; self._lock = threading.RLock()
        self._consumed = {}; self._executions = {}; self._manager_owner = None
        self._ownership_fingerprint = None; self._closed = False

    def _scheduler(self, manager):
        from app.plugin_execution import ExecutionCoordinator
        from app.plugin_scheduler import PluginCallScheduler
        coordinator = getattr(manager, 'execution_coordinator', None)
        if (type(coordinator) is not ExecutionCoordinator
                or type(coordinator.scheduler) is not PluginCallScheduler
                or not 1 <= coordinator.scheduler.max_concurrent <= 10
                or coordinator.snapshot()['closed'] or coordinator.scheduler.snapshot()['closed']):
            raise StageError('stage_shared_host_scheduler_unavailable')
        return coordinator.scheduler

    def _manager(self):
        manager = self.manager_provider()
        required = ('ownership_authority', '_db', '_row', '_assert_mutation_idle',
                    '_signature', '_trust_binding', '_dependency_blocks', 'data_transactions')
        if not all(callable(getattr(manager, key, None)) for key in required):
            raise StageError('stage_host_manager_unavailable')
        with manager._lock:
            authority = self._authority(manager)
            with self._lock:
                if self._closed:
                    raise StageError('stage_service_closed')
                if self._manager_owner is None:
                    self._manager_owner = manager; self._ownership_fingerprint = authority
                elif self._manager_owner is not manager or self._ownership_fingerprint != authority:
                    raise StageError('stage_manager_ownership_lifetime_changed')
            self._scheduler(manager)
        return manager

    @contextmanager
    def _admission(self, manager, body, capability, cancelled):
        """One actual shared slot from preparation through proved native cleanup.

        The surrounding coordinator mutation_guard excludes ordinary calls for this
        plugin; creating a second scheduler or lexical scheduler.slot is forbidden.
        """
        call_id = body['call_id']; scheduler = self._scheduler(manager)
        execution = {'manager': manager, 'capability': capability, 'body': body,
                     'cancel_event': cancelled, 'lease': None, 'request': None,
                     'proxy': None, 'owner': None, 'native_open_attempted': False,
                     'cancel_hook': None, 'hook_claimed': False, 'hook_completed': False,
                     'hook_failed': False, 'phase': 'queued', 'cleanup_confirmed': False,
                     'host_cleanup_confirmed': True, 'host_handles': None, 'host_cleanup_deferred': False}
        with self._lock:
            if self._closed or call_id in self._executions or len(self._executions) >= 4096:
                raise StageError('stage_execution_registry_unavailable')
            self._consumed[capability._token] = capability
            self._executions[call_id] = execution
        try:
            lease = scheduler.acquire_lease(call_id, body['plugin_id'], cancel=cancelled)
            lease.cancel_event = cancelled
            execution['lease'] = lease; execution['phase'] = 'admitted'
            if cancelled.is_set():
                raise StageError('stage_cancelled_before_native')
            yield execution
        finally:
            lease = execution['lease']; owner = execution['owner']
            confirmed = not execution['native_open_attempted']
            if owner is not None:
                try:
                    confirmed = owner.cleanup_confirmed() is True
                except BaseException:
                    confirmed = False
            with self._lock:
                if (not execution['host_cleanup_confirmed'] or execution['hook_claimed']
                        and (not execution['hook_completed'] or execution['hook_failed'])):
                    confirmed = False
                cancelled_result = execution['phase'] == 'validated' and cancelled.is_set()
                execution['cleanup_confirmed'] = confirmed
                if lease is not None:
                    if confirmed:
                        lease.release()
                    else:
                        lease.quarantine()
                if not confirmed:
                    execution['phase'] = 'quarantined'
                elif execution['phase'] != 'validated' or cancelled_result:
                    execution['phase'] = 'failed_cleaned'
            if not confirmed:
                raise StageError('stage_native_cleanup_unconfirmed')
            if cancelled_result:
                raise StageError('stage_cancelled_result_unknown')

    @contextmanager
    def _host_handles(self, execution):
        """Held code/proxy leases are also owned cleanup, not disposable locals."""
        handles = ExitStack()
        execution['host_handles'] = handles; execution['host_cleanup_confirmed'] = False
        try:
            yield handles
        finally:
            owner = execution['owner']; native_confirmed = not execution['native_open_attempted']
            if owner is not None:
                try:
                    native_confirmed = owner.cleanup_confirmed() is True
                except BaseException:
                    native_confirmed = False
            if not native_confirmed:
                # Keep file handles/old read leases/proxy ownership while a native
                # child or cleanup thread may still exist. A later explicit repair
                # closes this same non-consumed stack only after native proof.
                execution['host_cleanup_deferred'] = True
            else:
                handles.close()
                execution['host_cleanup_confirmed'] = not execution.get('host_cleanup_fault', False)

    def _register_owner(self, execution, owner):
        from app.plugin_stage_native_runtime import StageSession
        if (type(owner) is not StageSession or owner.request is not execution['request']
                or owner.proxy is not execution['proxy'] or owner.cancelled is not execution['cancel_event']
                or not all(callable(getattr(owner, name, None)) for name in
                           ('query_stage_identity', 'run_stage', 'close', 'cleanup_confirmed'))):
            raise StageError('stage_native_owner_incomplete')
        with self._lock:
            previous = execution['owner']
            if previous is not None and previous is not owner:
                raise StageError('stage_native_owner_changed')
            # Retain the real partial owner BEFORE refusing a failed prepare. An
            # absent authority on an exception is not proof of zero native work.
            execution['owner'] = owner
            authority = owner.authority
            if authority is not None and (authority.approval_capability is not execution['capability']
                    or authority.admission_lease is not execution['lease']
                    or authority.cancel_event is not execution['cancel_event']
                    or authority.snapshot_json != execution['capability'].snapshot_json):
                raise StageError('stage_native_owner_authority_changed')

    def _invoke_cancel(self, execution, hook):
        try:
            hook()
        except BaseException:
            with self._lock:
                execution['hook_failed'] = True
        finally:
            with self._lock:
                execution['hook_completed'] = True

    def _register_cancel(self, execution, hook):
        if not callable(hook):
            raise TypeError('actual native stage cancellation hook required')
        invoke = False
        with self._lock:
            if execution['cancel_hook'] is not None or execution['cleanup_confirmed']:
                raise StageError('stage_cancel_hook_already_registered_or_finished')
            execution['cancel_hook'] = hook
            if execution['cancel_event'].is_set():
                execution['hook_claimed'] = True; invoke = True
        if invoke:
            self._invoke_cancel(execution, hook)

    def cancel(self, call_id):
        """Host-only exact-call cancellation; never execute native hooks under locks."""
        _opaque(call_id); hook = None
        with self._lock:
            execution = self._executions.get(call_id)
            if execution is None:
                raise StageError('stage_execution_owner_unknown')
            if execution['cleanup_confirmed']:
                return {'cancel_requested': False, 'cleanup_confirmed': True}
            execution['cancel_event'].set()
            if execution['cancel_hook'] is not None and not execution['hook_claimed']:
                execution['hook_claimed'] = True; hook = execution['cancel_hook']
        if hook is not None:
            self._invoke_cancel(execution, hook)
        return {'cancel_requested': True, 'migration_replayed': False}

    def close(self):
        """Manager lifecycle owner. Unknown cleanup keeps this service and slot alive."""
        with self._lock:
            self._closed = True
            calls = [key for key, item in self._executions.items() if not item['cleanup_confirmed']]
        for call_id in calls:
            self.cancel(call_id)
        if calls:
            raise StageError('stage_execution_cleanup_pending')

    def retry_cleanup(self, call_id):
        """Explicit trusted repair only; never rerun a migration or adopt a receipt."""
        _opaque(call_id)
        with self._lock:
            execution = self._executions.get(call_id)
            if execution is None or execution['phase'] != 'quarantined' or execution['owner'] is None:
                raise StageError('stage_cleanup_owner_unknown')
            if (not execution['host_cleanup_confirmed'] and not execution['host_cleanup_deferred']
                    or execution.get('host_cleanup_fault', False)):
                # An ExitStack retry cannot prove a failed close: callbacks may have
                # already been consumed. Preserve owner/lease for explicit host audit.
                raise StageError('stage_host_handle_cleanup_requires_manual_audit')
            owner, lease = execution['owner'], execution['lease']
        self.cancel(call_id)
        owner.close()
        if (owner.cleanup_confirmed() is not True or execution['hook_failed']
                or execution['hook_claimed'] and not execution['hook_completed']):
            raise StageError('stage_native_cleanup_unconfirmed')
        if self.executor is not None and call_id in self.executor.pending_cleanup():
            self.executor.retry_cleanup(call_id)
        if execution['host_cleanup_deferred']:
            execution['host_cleanup_deferred'] = False
            execution['host_handles'].close()
            execution['host_cleanup_confirmed'] = True
        lease.release()
        with self._lock:
            execution['cleanup_confirmed'] = True; execution['phase'] = 'failed_cleaned'
        return {'cleanup_confirmed': True, 'migration_replayed': False}

    def _validate_native_current(self, request, proxy, capability, snapshot):
        """Requery manager, old inventory, target code and exact consumed A before IO."""
        from app.plugin_scheduler import SchedulerLease
        with self._lock:
            execution = self._executions.get(getattr(request, 'call_id', None))
            if (self._closed or execution is None or type(capability) is not _HostStageApproval
                    or capability._owner is not self or self._consumed.get(capability._token) is not capability
                    or execution['capability'] is not capability or execution['request'] is not request
                    or execution['proxy'] is not proxy or type(proxy) is not StageDataProxy or proxy.request is not request
                    or execution['phase'] != 'running' or execution['cancel_event'].is_set()
                    or _json(snapshot) != capability.snapshot_json
                    or capability.fingerprint != _hash(capability.snapshot_json)):
                raise StageError('stage_consumed_A_capability_required')
            manager, body, lease = execution['manager'], execution['body'], execution['lease']
        with manager._lock:
            if self._manager() is not manager:
                raise StageError('stage_manager_lifecycle_changed')
            scheduler = self._scheduler(manager)
            if (type(lease) is not SchedulerLease or lease._scheduler is not scheduler
                    or lease.call_id != request.call_id or lease.plugin_id != request.plan.plugin_id
                    or getattr(lease, 'cancel_event', None) is not execution['cancel_event']):
                raise StageError('stage_actual_shared_admission_required')
            with scheduler._condition:
                if scheduler._running.get(request.call_id) is not lease or lease._released or lease._quarantined:
                    raise StageError('stage_actual_shared_admission_required')
            if (request.manifest_json != _json(body['manifest'])
                    or request.migration_json != _json(body['migration'])
                    or request.native_spec_json != _json(body['native_spec'])
                    or request.candidate_id != body['candidate_id'] or request.code_root != body['candidate_code_root']
                    or request.plan.fingerprint != execution['plan_fingerprint']):
                raise StageError('stage_consumed_A_request_changed')
            host, current, _, _ = self._current(manager, body['plugin_id'], body['manifest'], body['expected_revision'])
            if host != body['host_binding'] or current != request.plan.current:
                raise StageError('stage_live_binding_changed')
            old_files, old_sha, old_evidence = inspect_tree(manager.root / 'data' / body['plugin_id'], missing=True)
            if (old_files != request.plan.source_files or old_sha != current.data_inventory_sha
                    or _hash(old_evidence) != body['source_observation_sha']):
                raise StageError('stage_live_source_inventory_changed')
            code = checked(request.code_root)
            scanned = scan_package(code, manager.limits); files, digest, evidence = inspect_tree(code)
            if (scanned.digest != request.plan.target_code_digest or scanned.manifest != body['manifest']
                    or files != request.plan.code_files or digest != body['candidate_code_inventory_sha']
                    or _hash(evidence) != execution['code_observation_sha']):
                raise StageError('stage_live_candidate_code_changed')
            compatibility = execution['compatibility']
            if current.schema is None:
                if self.compatibility_authority is None:
                    raise StageError('host_compatibility_capability_required')
                audit = self.compatibility_authority.resolve(compatibility, current, request.plan.target_code_digest)
                if audit != request.plan.compatibility:
                    raise StageError('stage_live_compatibility_changed')
        return True

    def native_authority(self, request, proxy, cancelled):
        """Actual native module authority provider, not a constant-True adapter."""
        from app.plugin_stage_native_runtime import StageHostAuthority
        with self._lock:
            execution = self._executions.get(getattr(request, 'call_id', None))
            if (execution is None or execution['cancel_event'] is not cancelled
                    or execution['request'] is not request or execution['proxy'] is not proxy):
                raise StageError('stage_execution_owner_unavailable')
            capability = execution['capability']
        snapshot = json.loads(capability.snapshot_json)
        self._validate_native_current(request, proxy, capability, snapshot)
        execution['native_open_attempted'] = True
        return StageHostAuthority(snapshot_json=capability.snapshot_json,
            authorizing_fingerprint=capability.fingerprint, approval_capability=capability,
            admission_lease=execution['lease'], cancel_event=cancelled,
            validate_current=self._validate_native_current,
            register_cancel=lambda hook: self._register_cancel(execution, hook))

    @staticmethod
    def _authority(manager):
        value = manager.ownership_authority()
        _strict(value, ('source', 'ownership_nonce', 'plugin_root', 'registry', 'root_identity', 'registry_identity'))
        if (value['source'] != 'host_file_identity' or value['plugin_root'] != str(manager.root)
                or value['registry'] != str(manager.db_path)):
            raise StageError('stage_host_ownership_unavailable')
        _sha(value['ownership_nonce'])
        for key in ('root_identity', 'registry_identity'):
            if type(value[key]) is not list or len(value[key]) != 2:
                raise StageError('stage_host_ownership_unavailable')
            for item in value[key]:
                _integer(item, 0, 2**64-1)
        return _hash(value)

    def _paths(self, manager, pid, candidate_id, generation):
        validate_id(pid); _opaque(candidate_id)
        root = checked(manager.root)
        return (root / 'runtime-safety' / 'stage-records' / (candidate_id + '.json'),
                root / 'runtime-safety' / pid / 'stage' / generation,
                root / 'runtime-safety' / 'stage-io' / pid / candidate_id)

    def _current(self, manager, pid, manifest, expected_revision):
        manager._assert_mutation_idle(pid)
        authority = self._authority(manager)
        with manager._db() as conn:
            row = dict(manager._row(conn, pid, installed=True))
            if row['revision'] != expected_revision:
                raise StageError('stage_revision_changed')
            if (row['enabled'] or row['loaded'] or row['pending_update'] or row['migration_id']
                    or row['data_inventory_dirty'] or not row['active_generation']):
                raise StageError('stage_requires_disabled_idle_registered_clean_generation')
            current = manager.data_transactions().current_from_row(conn, row)
            signature = manager._signature(manifest)
            if type(signature) is not dict or signature.get('status') not in ('absent', 'present_unverified', 'verified_trusted'):
                raise StageError('stage_target_signature_invalid_or_revoked')
            trust = manager._trust_binding()
            # A callback's mutable closure is not a trust revision. The manager owns
            # this verifier for its ownership lifetime; always re-evaluate signature.
            trust_binding = {'trust': trust, 'signature': signature,
                             'verifier_configured': manager.signature_verifier is not None}
            if manager._dependency_blocks(conn, manifest):
                raise StageError('stage_target_dependencies_blocked')
            dependencies = {}; visiting = {pid}
            def visit(document):
                for dependency in document.get('dependencies', []):
                    other_id = dependency['id']
                    if other_id in visiting:
                        raise StageError('stage_dependency_cycle')
                    if other_id in dependencies:
                        continue
                    other = manager._row(conn, other_id, installed=True)
                    other_manifest = json.loads(other['manifest'])
                    dependencies[other_id] = {key: other[key] for key in ('id', 'revision', 'digest', 'enabled', 'manifest')}
                    dependencies[other_id]['signature_status'] = manager._signature(other_manifest)['status']
                    if dependencies[other_id]['signature_status'] in ('invalid', 'revoked'):
                        raise StageError('stage_dependency_signature_invalid')
                    visiting.add(other_id); visit(other_manifest); visiting.remove(other_id)
            visit(manifest)
            dependencies = [dependencies[key] for key in sorted(dependencies)]
            binding = {key: row[key] for key in ('id', 'revision', 'digest', 'enabled', 'loaded',
                       'active_generation', 'data_schema', 'data_write_epoch', 'data_effect_epoch',
                       'migration_id', 'data_inventory_dirty', 'pending_update')}
            config_json = safe_document(json.loads(row['config'])); state_json = safe_document(json.loads(row['state']))
        return {'authority': authority, 'binding': binding, 'old': asdict(current),
                'trust_fingerprint': _hash(trust_binding), 'trust': trust_binding,
                'dependency_fingerprint': _hash(dependencies),
                'config_sha': _hash(config_json), 'state_sha': _hash(state_json)}, current, config_json, state_json

    def _scan(self, manager, pid, source, expected_revision, compatibility):
        source = checked(source)
        package = scan_package(source, manager.limits)
        if package.manifest['id'] != pid:
            raise StageError('stage_candidate_plugin_id_mismatch')
        validate_data_migrations(package.manifest)
        # A cannot guess a target schema or authorize arbitrary code without a hook.
        if type(package.manifest.get('data_schema')) is not int:
            raise StageError('stage_target_schema_unknown')
        host, old, config_json, state_json = self._current(manager, pid, package.manifest, expected_revision)
        files, old_sha, old_evidence = inspect_tree(manager.root / 'data' / pid, missing=True)
        if old_sha != old.data_inventory_sha:
            raise StageError('stage_active_inventory_requires_explicit_reconciliation')
        # Directory inventory identities are real inspect_tree observations. Archive
        # inventories instead come from bounded bytes and carry no fake OS flags.
        if source.is_dir():
            code_files, code_sha, source_evidence = inspect_tree(source)
            if (set(item.path for item in code_files) != set(package.blobs) | {'manifest.json'}
                    or any(item.path != 'manifest.json' and
                           (item.sha256 != _hash(package.blobs[item.path]) or item.size != len(package.blobs[item.path]))
                           for item in code_files)):
                raise StageError('stage_candidate_scan_inventory_disagree')
            again = scan_package(source, manager.limits)
            if again.digest != package.digest or again.manifest != package.manifest or again.blobs != package.blobs:
                raise StageError('stage_candidate_source_changed_during_preview')
        else:
            with _parents_held(source), _held_file(source) as stream:
                info = os.fstat(stream.fileno())
                if info.st_size > manager.limits.max_archive_bytes:
                    raise StageError('stage_source_archive_limit')
                archive_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                if _identity(info) != _identity(_ordinary(source)):
                    raise StageError('stage_source_archive_changed')
            source_evidence = {'archive_identity': _identity(info), 'archive_sha': archive_sha}
            again = scan_package(source, manager.limits)
            if again.digest != package.digest or again.manifest != package.manifest or again.blobs != package.blobs:
                raise StageError('stage_candidate_source_changed_during_preview')
        registered_inventory = [{'path': name, 'sha256': _hash(blob), 'size': len(blob)}
                                for name, blob in sorted(package.blobs.items())]
        manifest_blob = (canonical_json(package.manifest) + '\n').encode('utf-8')
        registered_inventory.append({'path': 'manifest.json', 'sha256': _hash(manifest_blob), 'size': len(manifest_blob)})
        registered_inventory.sort(key=lambda x: x['path'])
        # The approved candidate is the tree package.write_to actually registers,
        # including canonical manifest bytes plus exactly one trailing newline. Raw
        # source spelling/formatting belongs only to the separate source observation.
        code_inventory = registered_inventory; code_sha = _hash(code_inventory)
        candidate_code_root = str(checked(manager.root / 'code' / pid / package.digest))
        resolved = old.schema; audit = None
        if resolved is None:
            if compatibility is None or self.compatibility_authority is None:
                return {'blocked': True, 'reasons': ['host_compatibility_audit_required'],
                        'plugin_id': pid, 'expected_revision': expected_revision,
                        'current_tuple': asdict(old), 'old_tuple_sha': _hash(asdict(old)),
                        'target_code_digest': package.digest, 'target_schema': package.manifest['data_schema'],
                        'candidate_manifest': package.manifest, 'candidate_code_inventory': code_inventory,
                         'candidate_code_inventory_sha': code_sha, 'candidate_code_root': candidate_code_root,
                        'host_binding': host, 'old_inventory': [asdict(x) for x in files],
                        'audit_required_fields': ['resolved_schema', 'reviewer', 'independently_validated_evidence',
                                                  'old_tuple_sha', 'target_code_digest', 'explicit_audit_confirmation'],
                        'schema_inferred': False, 'token': None,
                        'automatic_execution': False, 'enabled_after_activation': False}, None
            audit = self.compatibility_authority.resolve(compatibility, old, package.digest)
            resolved = audit.resolved_schema
        elif compatibility is not None:
            raise StageError('stage_known_schema_cannot_be_overridden')
        if package.manifest['data_schema'] <= resolved:
            raise StageError('stage_schema_must_strictly_increase')
        migrations = [item for item in package.manifest.get('migrations', [])
                      if item['from_schema'] == resolved and item['to_schema'] == package.manifest['data_schema']]
        if len(migrations) != 1:
            raise StageError('stage_exact_declared_direct_migration_required')
        migration = migrations[0]
        fresh_host, fresh_old, fresh_config, fresh_state = self._current(manager, pid, package.manifest, expected_revision)
        if fresh_host != host or fresh_old != old or fresh_config != config_json or fresh_state != state_json:
            raise StageError('stage_host_changed_during_preview')
        body = {'plugin_id': pid, 'expected_revision': expected_revision, 'source_path': str(source),
                'target_code_digest': package.digest, 'target_schema': package.manifest['data_schema'],
                'manifest': package.manifest, 'migration': migration, 'host_binding': host,
                'current_tuple': asdict(old), 'source_inventory': [asdict(x) for x in files],
                'source_observation_sha': _hash(old_evidence),
                'candidate_code_inventory': code_inventory, 'candidate_code_inventory_sha': code_sha,
                'registered_code_inventory': registered_inventory,
                'registered_code_inventory_sha': _hash(registered_inventory),
                'registered_code_root': candidate_code_root, 'candidate_code_root': candidate_code_root,
                'candidate_source_observation_sha': _hash(source_evidence),
                'compatibility': asdict(audit) if audit is not None else None,
                'permissions': list(PERMISSIONS), 'network_capabilities': [], 'external_effects': False,
                'limits': {'data_bytes': MAX_DATA_BYTES, 'filesystem_entries': MAX_FILES,
                           'read_bytes': 2 * MAX_DATA_BYTES, 'write_bytes': MAX_DATA_BYTES,
                           'chunk_bytes': CHUNK, 'wire_bytes': MAX_WIRE_BYTES, 'requests': MAX_REQUESTS,
                           'memory_bytes': 256 * 1024**2, 'cpu_rate': 2000, 'active_processes': 1, 'wall_seconds': 10},
                'enabled_after_activation': False, 'active_tuple_changed_by_A': False,
                'config_bytes_exposed': len(config_json), 'state_bytes_exposed': len(state_json),
                'source_schema': resolved, 'schema_inferred': False}
        return body, (package, old, config_json, state_json, files, audit)

    def preview(self, pid, host_source_path, expected_revision, *, compatibility=None):
        validate_id(pid); _integer(expected_revision, 1, 2**53-1)
        manager = self._manager()
        with manager._lock, self._lock:
            if pending_candidates(manager.root, pid):
                raise StageError('stage_existing_candidate_requires_review_or_B')
            body, details = self._scan(manager, pid, host_source_path, expected_revision, compatibility)
            if details is None:
                return body
            candidate_id = secrets.token_urlsafe(32); generation = str(uuid4())
            body.update(candidate_id=candidate_id, stage_generation=generation,
                        call_id='stage_' + secrets.token_urlsafe(32),
                        approval_kind='migration_A', independent_confirmation_required=True)
            _, stage_root, temporary_root = self._paths(manager, pid, candidate_id, generation)
            body.update(stage_root=str(stage_root), temporary_root=str(temporary_root),
                        child_active_data_access=False, child_stage_direct_access=False,
                        data_access_mode='held_host_migration_proxy_only')
            native_spec = self.executor.preview_spec(host_snapshot=body) if self.executor is not None else None
            body['native_spec'] = native_spec
            if native_spec is None:
                return {'blocked': True, 'reasons': ['strict_migration_executor_unavailable'],
                        'snapshot': json.loads(_json(body)), 'token': None, 'fingerprint': None,
                        'stage_execution_available': False, 'migration_performed': False,
                        'source_registered': False, 'automatic_execution': False,
                        'enabled_after_activation': False}
            raw = _json(body)
            if len(raw) > MAX_PREVIEW_BYTES:
                raise StageError('stage_preview_too_large_no_truncation')
            now = self.clock()
            self._pending = {token: item for token, item in self._pending.items() if item['expires'] > now}
            if len(self._pending) >= 128:
                raise StageError('stage_preview_capacity')
            token = secrets.token_urlsafe(32); fingerprint = _hash(body)
            self._pending[_hash(token.encode('ascii'))] = {'body': body, 'expires': now + self.ttl,
                'compatibility': compatibility, 'fingerprint': fingerprint}
            return {'token': token, 'fingerprint': fingerprint, 'snapshot': json.loads(raw),
                    'candidate_id': candidate_id, 'expires_in': self.ttl, 'approval_required': True,
                    'stage_execution_available': self.executor is not None and self.executor.available(),
                    'automatic_execution': False, 'enabled_after_activation': False,
                    'migration_performed': False, 'source_registered': False}

    def cancel_preview(self, pid, token, fingerprint):
        validate_id(pid); _opaque(token)
        with self._lock:
            record = self._pending.pop(_hash(token.encode('ascii')), None)
            if (record is None or record['body']['plugin_id'] != pid or record['fingerprint'] != fingerprint
                    or self.clock() >= record['expires']):
                raise StageError('stage_A_approval_invalid')
        return {'cancelled': True, 'migration_performed': False, 'files_modified': False}

    @staticmethod
    def _event(path, record, phase, **values):
        record.update(values); record['phase'] = phase; record['updated_at'] = time.time()
        _atomic_json(path, record)

    def _register_code(self, manager, package, body, journal, record):
        pid, digest = package.manifest['id'], package.digest
        target = checked(manager.root / 'code' / pid / digest)
        staging = checked(manager.root / 'runtime-safety' / 'candidate-code' / pid / body['candidate_id'])
        self._event(journal, record, 'code_register_intent')
        if not target.exists():
            _mkdirs(staging.parent)
            with _parents_held(staging):
                package.write_to(staging)
            scanned = scan_package(staging, manager.limits)
            if scanned.digest != digest or scanned.manifest != package.manifest:
                raise StageError('stage_candidate_code_copy_unconfirmed')
            # Flush every copied code file before registering the immutable directory.
            for folder, _, names in os.walk(staging, followlinks=False):
                for name in names:
                    path = checked(Path(folder) / name)
                    with path.open('r+b') as stream:
                        os.fsync(stream.fileno())
            _mkdirs(target.parent)
            with _parents_held(target), _parents_held(staging):
                if target.exists():
                    raise StageError('stage_target_appeared_during_registration')
                os.rename(staging, target); _fsync_directory(target.parent)
        scanned = scan_package(target, manager.limits)
        files, inventory_sha, evidence = inspect_tree(target)
        projected = [{'path': item.path, 'sha256': item.sha256, 'size': item.size} for item in files]
        if (scanned.digest != digest or scanned.manifest != package.manifest
                or projected != body['registered_code_inventory'] or inventory_sha != body['registered_code_inventory_sha']):
            raise StageError('stage_registered_target_code_changed')
        # Store code+versions only. Never call install/update: those switch active code.
        with manager._db() as conn:
            old_version = conn.execute('SELECT manifest FROM versions WHERE plugin_id=? AND digest=?', (pid, digest)).fetchone()
            if old_version is not None and json.loads(old_version['manifest']) != package.manifest:
                raise StageError('stage_existing_version_manifest_changed')
            conn.execute('INSERT OR IGNORE INTO versions(plugin_id,digest,manifest,signature_status,source) VALUES(?,?,?,?,?)',
                         (pid, digest, canonical_json(package.manifest), body['host_binding']['trust']['signature']['status'], package.source))
        self._event(journal, record, 'code_registered', code_inventory_sha=inventory_sha,
                    code_observation_sha=_hash(evidence), code_files=[asdict(x) for x in files])
        return target, files

    def run_stage(self, pid, token, fingerprint, *, user_confirmed=False, cancelled=None):
        """Consume exact A once, write pre-intent, register code, run strict hook only.

        A requires plugin already disabled. It never enables or silently disables an
        enabled plugin; a distinct disable approval is required before preview.
        """
        validate_id(pid); _opaque(token)
        with self._lock:
            pending = self._pending.pop(_hash(token.encode('ascii')), None)
        if (pending is None or user_confirmed is not True or pending['fingerprint'] != fingerprint
                or pending['body']['plugin_id'] != pid or self.clock() >= pending['expires']):
            raise StageError('stage_independent_A_confirmation_required')
        if self.executor is None or not self.executor.available():
            raise StageError('strict_migration_executor_unavailable_no_fallback')
        if cancelled is None:
            cancelled = threading.Event()
        if type(cancelled) is not threading.Event:
            raise TypeError('exact host cancellation Event required')
        manager = self._manager(); body = json.loads(_json(pending['body']))
        snapshot_json = _json(body)
        if _hash(snapshot_json) != fingerprint:
            raise StageError('stage_consumed_A_snapshot_changed')
        capability = _HostStageApproval(self, secrets.token_urlsafe(32), fingerprint, snapshot_json)
        journal = None; record = None; execution = None
        try:
            with self._admission(manager, body, capability, cancelled) as execution:
                # A is a mutation owner, not an ordinary execution. Keep this exact
                # guard for the full lifetime, without holding the manager lock while
                # a native process or its cancellation hook runs.
                with manager.execution_coordinator.mutation_guard(pid):
                    with manager._lock:
                        manager._assert_mutation_idle(pid)
                        if pending_candidates(manager.root, pid):
                            raise StageError('stage_existing_candidate_requires_review_or_B')
                        fresh, details = self._scan(manager, pid, body['source_path'], body['expected_revision'], pending['compatibility'])
                        comparable = {key: value for key, value in body.items()
                                      if key not in ('candidate_id', 'stage_generation', 'call_id', 'native_spec',
                                          'approval_kind', 'independent_confirmation_required', 'stage_root', 'temporary_root',
                                          'child_active_data_access', 'child_stage_direct_access', 'data_access_mode')}
                        if fresh != comparable or details is None:
                            raise StageError('stage_A_source_tuple_epoch_trust_or_ownership_changed')
                        package, old, config_json, state_json, source_files, audit = details
                        journal, stage, temporary = self._paths(manager, pid, body['candidate_id'], body['stage_generation'])
                        record = {'format': 1, 'candidate_id': body['candidate_id'], 'plugin_id': pid,
                                  'approval_fingerprint': fingerprint, 'approval_snapshot': body,
                                  'phase': 'stage_intent', 'created_at': time.time(), 'updated_at': time.time(),
                                  'enabled': False, 'automatic_resume': False, 'active_tuple_published': False,
                                  'result': None, 'error': None}
                        _atomic_json(journal, record, create=True)
                        code_root, code_files = self._register_code(manager, package, body, journal, record)
                        self._event(journal, record, 'generation_create_intent')
                        _mkdirs(stage.parent); _mkdirs(temporary.parent)
                        with _parents_held(stage):
                            stage.mkdir(exist_ok=False)
                        with _parents_held(temporary):
                            temporary.mkdir(exist_ok=False)
                        self._event(journal, record, 'generation_created')
                        plan = MigrationPlan(pid, body['expected_revision'], body['host_binding']['authority'],
                            body['host_binding']['trust_fingerprint'], old, package.digest, body['target_schema'],
                            body['stage_generation'], source_files, code_files, body['migration']['entrypoint']['path'],
                            package.manifest['files'][body['migration']['entrypoint']['path']], body['host_binding']['binding']['data_write_epoch'], audit)
                        request = StageExecutionRequest(plan, _json(package.manifest), _json(body['migration']),
                            str(code_root), body['candidate_id'], body['call_id'], _json(body['native_spec']))
                        request.init(session=secrets.token_urlsafe(32))
                        execution.update(request=request, plan_fingerprint=plan.fingerprint,
                                         code_observation_sha=record['code_observation_sha'],
                                         compatibility=pending['compatibility'], phase='running')
                        self._event(journal, record, 'running', plan_fingerprint=plan.fingerprint,
                                    execution_request={'call_id': request.call_id, 'worker_mode': request.worker_mode})
                    def assert_current():
                        self._validate_native_current(request, execution['proxy'], capability, body)
                    with self._host_handles(execution) as code_lease:
                        for parent in (*reversed(code_root.parents), code_root):
                            code_lease.enter_context(_held_file(parent, directory=True))
                        for item in code_files:
                            code_path = checked(code_root.joinpath(*item.path.split('/')))
                            stream = code_lease.enter_context(_held_file(code_path))
                            if (os.fstat(stream.fileno()).st_size != item.size
                                    or hashlib.file_digest(stream, 'sha256').hexdigest() != item.sha256):
                                raise StageError('stage_target_code_handle_changed')
                        proxy = StageDataProxy(request, old_root=manager.root / 'data' / pid, stage_root=stage,
                                               temporary_root=temporary, config_json=config_json, state_json=state_json,
                                               assert_current=assert_current, host_lock=manager._lock, clock=self.clock)
                        execution['proxy'] = proxy
                        try:
                            code_lease.enter_context(proxy)
                        except BaseException as error:
                            if getattr(error, 'code', None) == 'stage_proxy_cleanup_unconfirmed':
                                execution['host_cleanup_fault'] = True
                            raise
                        assert_current()
                        result = self.executor.execute(request, proxy=proxy, cancelled=cancelled,
                            owner_registered=lambda owner: self._register_owner(execution, owner))
                    with manager._lock:
                        assert_current()
                        if (type(result) is not StageResult or result.plan_fingerprint != plan.fingerprint
                                or execution['owner'] is None or execution['owner'].cleanup_confirmed() is not True
                                or execution['hook_failed'] or execution['hook_claimed'] and not execution['hook_completed']):
                            raise StageError('stage_unbound_or_unclean_executor_result')
                        files, inventory_sha, evidence = inspect_tree(stage)
                        if files != result.files or inventory_sha != result.activation(package.digest).data_inventory_sha:
                            raise StageError('stage_final_inventory_unconfirmed')
                        result_body = {'plan_fingerprint': result.plan_fingerprint, 'data_generation': result.data_generation,
                                       'schema': result.schema, 'config': json.loads(result.config_json),
                                       'state': json.loads(result.state_json), 'files': [asdict(x) for x in result.files],
                                       'receipt_sha': result.receipt_sha, 'external_side_effects': False}
                        self._event(journal, record, 'validated', result=result_body,
                                    stage_observation_sha=_hash(evidence), prepared_binding=_hash(result_body))
                        execution.update(phase='validated', result=result, record_sha=_hash(record))
                        self._records[body['candidate_id']] = record
                        self._validated_owners[body['candidate_id']] = execution
                        response = {'plugin_id': pid, 'candidate_id': body['candidate_id'], 'stage_id': body['candidate_id'],
                                    'phase': 'validated', 'plan_fingerprint': plan.fingerprint,
                                    'result': {'new_tuple': asdict(result.activation(package.digest)),
                                               'receipt_sha': result.receipt_sha, 'files': len(result.files),
                                               'data_bytes': sum(x.size for x in result.files), 'external_side_effects': False},
                                    'code_registered': True, 'active_tuple_published': False, 'enabled': False,
                                    'confirmation_B_required': True, 'automatic_execution': False}
            return response
        except BaseException as error:
            self._validated_owners.pop(body['candidate_id'], None)
            if journal is not None and record is not None:
                code = getattr(error, 'code', None)
                self._event(journal, record, 'needs_review', error=code if type(code) is str else 'stage_outcome_unconfirmed',
                            automatic_resume=False)
            raise

    def _validated_execution(self, manager, candidate_id, record):
        """B must use the SAME actual A/native owner registry, never journal adoption."""
        with self._lock:
            execution = self._validated_owners.get(candidate_id)
            capability = execution['capability'] if execution is not None else None
            if (execution is None or execution['manager'] is not manager
                    or execution['phase'] != 'validated' or not execution['cleanup_confirmed']
                    or self._executions.get(execution['body']['call_id']) is not execution
                    or type(capability) is not _HostStageApproval or capability._owner is not self
                    or self._consumed.get(capability._token) is not capability
                    or capability.snapshot_json != _json(record.get('approval_snapshot'))
                    or capability.fingerprint != record.get('approval_fingerprint')
                    or execution['record_sha'] != _hash(record)
                    or execution['owner'] is None or execution['lease']._released is not True
                    or execution['owner'].authority is None
                    or execution['owner'].authority.approval_capability is not capability
                    or execution['owner'].authority.admission_lease is not execution['lease']
                    or execution['owner'].authority.cancel_event is not execution['cancel_event']):
                raise StageError('stage_server_execution_owner_unavailable_or_record_changed')
        if execution['owner'].cleanup_confirmed() is not True:
            raise StageError('stage_native_cleanup_unconfirmed')
        return execution

    def prepared(self, pid, candidate_id):
        """Trusted stage_provider(pid,id) for existing B; no HTTP receipt parsing."""
        from app.plugin_data_api import PreparedMigration
        validate_id(pid); _opaque(candidate_id); manager = self._manager()
        with manager._lock:
            path = checked(manager.root / 'runtime-safety' / 'stage-records' / (candidate_id + '.json'))
            record = _read_record(path); body = record.get('approval_snapshot')
            owner = self._validated_execution(manager, candidate_id, record)
            if (record.get('format') != 1 or record.get('candidate_id') != candidate_id
                    or record.get('plugin_id') != pid or record.get('phase') != 'validated'
                    or type(body) is not dict or record.get('approval_fingerprint') != _hash(body)
                    or body.get('candidate_id') != candidate_id or body.get('plugin_id') != pid):
                raise StageError('stage_candidate_not_server_validated')
            host, old, _, _ = self._current(manager, pid, body['manifest'], body['expected_revision'])
            if host != body['host_binding'] or asdict(old) != body['current_tuple']:
                raise StageError('stage_candidate_host_binding_changed')
            old_files, old_sha, old_evidence = inspect_tree(manager.root / 'data' / pid, missing=True)
            if (old_files != owner['request'].plan.source_files or old_sha != old.data_inventory_sha
                    or _hash(old_evidence) != body['source_observation_sha']):
                raise StageError('stage_candidate_old_inventory_changed')
            # A new ownership lifetime cannot silently trust an old synthetic receipt.
            # Explicit host recovery/audit is required; never automatic hook replay.
            result_body = record['result']
            if _hash(result_body) != record['prepared_binding']:
                raise StageError('stage_candidate_result_changed')
            result = StageResult(result_body['plan_fingerprint'], result_body['data_generation'], result_body['schema'],
                safe_document(result_body['config']), safe_document(result_body['state']),
                tuple(InventoryFile(**item) for item in result_body['files']), result_body['receipt_sha'], False)
            if result != owner['result'] or host['authority'] != owner['body']['host_binding']['authority']:
                raise StageError('stage_server_held_result_or_ownership_changed')
            code = checked(manager.root / 'code' / pid / body['target_code_digest'])
            scanned = scan_package(code, manager.limits); code_files, code_sha, code_evidence = inspect_tree(code)
            if (scanned.digest != body['target_code_digest'] or scanned.manifest != body['manifest']
                    or code_sha != record['code_inventory_sha'] or _hash(code_evidence) != record['code_observation_sha']
                    or [asdict(x) for x in code_files] != record['code_files']):
                raise StageError('stage_candidate_registered_code_changed')
            _, stage, _ = self._paths(manager, pid, candidate_id, result.data_generation)
            files, _, evidence = inspect_tree(stage)
            if files != result.files or _hash(evidence) != record['stage_observation_sha']:
                raise StageError('stage_candidate_output_changed')
            audit = owner['request'].plan.compatibility
            if body['compatibility'] != (asdict(audit) if audit is not None else None):
                raise StageError('stage_server_compatibility_binding_changed')
            if old.schema is None:
                if (self.compatibility_authority is None
                        or self.compatibility_authority.resolve(owner['compatibility'], old, body['target_code_digest']) != audit):
                    raise StageError('stage_host_compatibility_approval_no_longer_owned')
            return PreparedMigration(pid, old, result, body['target_code_digest'], audit)

    def publication_completed(self, pid, candidate_id, receipt):
        """Host calls AFTER confirmed B; verify actual registry, not a browser receipt."""
        from app.plugin_migrations import PublicationReceipt
        validate_id(pid); _opaque(candidate_id); manager = self._manager()
        with manager._lock:
            path = checked(manager.root / 'runtime-safety' / 'stage-records' / (candidate_id + '.json'))
            record = _read_record(path); body = record.get('approval_snapshot')
            owner = self._validated_execution(manager, candidate_id, record)
            if (record.get('phase') != 'validated' or record.get('candidate_id') != candidate_id
                    or record.get('plugin_id') != pid or type(body) is not dict
                    or _hash(body) != record.get('approval_fingerprint')
                    or _hash(record.get('result')) != record.get('prepared_binding')):
                raise StageError('stage_candidate_not_server_validated')
            old = ActivationTuple(**body['current_tuple']); value = record['result']
            result = StageResult(value['plan_fingerprint'], value['data_generation'], value['schema'],
                safe_document(value['config']), safe_document(value['state']),
                tuple(InventoryFile(**item) for item in value['files']), value['receipt_sha'], False)
            new = result.activation(body['target_code_digest'])
            if (result != owner['result'] or type(receipt) is not PublicationReceipt or receipt.published is not True
                    or receipt.enabled is not False or receipt.previous_retained is not True
                    or receipt.old != old or receipt.new != new
                    or receipt.source != 'host_atomic_tuple_publisher'
                    or self._authority(manager) != body['host_binding']['authority']):
                raise StageError('stage_B_publication_receipt_required')
            with manager._db() as conn:
                row = manager._row(conn, pid, installed=True)
                current = manager.data_transactions().current_from_row(conn, row)
                tx = conn.execute("SELECT phase,old_tuple,new_tuple,snapshot,write_epoch,effect_epoch FROM plugin_data_transactions WHERE id=? AND plugin_id=?",
                                  (receipt.receipt_sha, pid)).fetchone()
                if (current != new or row['enabled'] or row['loaded'] or row['pending_update']
                        or row['data_inventory_dirty'] or row['revision'] != receipt.revision
                        or row['revision'] != body['expected_revision'] + 1 or row['migration_id']
                        or row['data_write_epoch'] != body['host_binding']['binding']['data_write_epoch']
                        or row['data_effect_epoch'] != body['host_binding']['binding']['data_effect_epoch']
                        or json.loads(row['manifest']) != body['manifest']
                        or tx is None or tx['phase'] != 'completed_disabled'
                        or json.loads(tx['old_tuple']) != asdict(old) or json.loads(tx['new_tuple']) != asdict(new)):
                    raise StageError('stage_B_registry_publication_unconfirmed')
                publication = json.loads(tx['snapshot'])
                if (publication.get('old') != asdict(old) or publication.get('new') != asdict(new)
                        or publication.get('stage_result', {}).get('receipt_sha') != result.receipt_sha
                        or publication.get('stage_result', {}).get('plan_fingerprint') != result.plan_fingerprint):
                    raise StageError('stage_B_execution_binding_unconfirmed')
            code = checked(manager.root / 'code' / pid / new.code_digest)
            scanned = scan_package(code, manager.limits); code_files, code_sha, code_evidence = inspect_tree(code)
            active_files, active_sha, _ = inspect_tree(manager.root / 'data' / pid)
            previous = checked(manager.root / 'runtime-safety' / pid / 'previous' / old.data_generation)
            previous_files, previous_sha, _ = inspect_tree(previous, missing=not publication['old_present'])
            if (scanned.digest != new.code_digest or scanned.manifest != body['manifest']
                    or code_sha != record['code_inventory_sha'] or _hash(code_evidence) != record['code_observation_sha']
                    or [asdict(x) for x in code_files] != record['code_files']
                    or active_files != result.files or active_sha != new.data_inventory_sha
                    or previous_files != owner['request'].plan.source_files or previous_sha != old.data_inventory_sha
                    or not publication['old_present'] and previous.exists()):
                raise StageError('stage_B_actual_tuple_files_unconfirmed')
            self._event(path, record, 'completed_disabled', active_tuple_published=True,
                        publication_receipt_sha=receipt.receipt_sha)
            with self._lock:
                owner['phase'] = 'completed_disabled'
                self._validated_owners.pop(candidate_id, None)

    def _publication_restore_candidate(self, manager, pid, candidate_id, review):
        """Historical linkage only. Never reconstruct an A owner, result or receipt."""
        from app.plugin_publication_review import PublicationReviewService, _authority_wire
        if (type(review) is not dict or review.get('kind') != 'publication'
                or review.get('plugin_id') != pid or review.get('known') is not True
                or 'continue_forward' not in review.get('allowed_decisions', ())
                or review.get('reasons') or review.get('resume_phase') not in
                    ('publish_intent', 'old_renamed', 'stage_renamed')
                or review.get('current_authority') != _authority_wire(manager.ownership_authority())):
            raise StageError('stage_B_current_authority_forward_review_required')
        publication = review['intent']; journal = review['journal']
        old, published = PublicationReviewService._validate_intent(journal, publication)
        if (journal['phase'] != 'needs_review' or journal['plugin_id'] != pid
                or review['journal_fingerprint'] != journal['id']
                or review['expected_revision'] != journal['expected_revision']):
            raise StageError('stage_B_original_publication_intent_required')
        path = checked(manager.root / 'runtime-safety' / 'stage-records' / (candidate_id + '.json'))
        record = _read_record(path)
        _strict(record, ('format', 'candidate_id', 'plugin_id', 'approval_fingerprint',
            'approval_snapshot', 'phase', 'created_at', 'updated_at', 'enabled',
            'automatic_resume', 'active_tuple_published', 'result', 'error',
            'code_inventory_sha', 'code_observation_sha', 'code_files', 'plan_fingerprint',
            'execution_request', 'stage_observation_sha', 'prepared_binding'))
        body = record['approval_snapshot']; value = record['result']
        _strict(body, ('plugin_id', 'expected_revision', 'source_path', 'target_code_digest',
            'target_schema', 'manifest', 'migration', 'host_binding', 'current_tuple',
            'source_inventory', 'source_observation_sha', 'candidate_code_inventory',
            'candidate_code_inventory_sha', 'registered_code_inventory', 'registered_code_inventory_sha',
            'registered_code_root', 'candidate_code_root', 'candidate_source_observation_sha',
            'compatibility', 'permissions', 'network_capabilities', 'external_effects', 'limits',
            'enabled_after_activation', 'active_tuple_changed_by_A', 'config_bytes_exposed',
            'state_bytes_exposed', 'source_schema', 'schema_inferred', 'candidate_id',
            'stage_generation', 'call_id', 'approval_kind', 'independent_confirmation_required',
            'stage_root', 'temporary_root', 'child_active_data_access', 'child_stage_direct_access',
            'data_access_mode', 'native_spec'))
        _strict(value, ('plan_fingerprint', 'data_generation', 'schema', 'config', 'state',
                        'files', 'receipt_sha', 'external_side_effects'))
        if (record['format'] != 1 or record['candidate_id'] != candidate_id
                or record['plugin_id'] != pid or record['phase'] != 'validated'
                or record['enabled'] is not False or record['automatic_resume'] is not False
                or record['active_tuple_published'] is not False or record['error'] is not None
                or type(body) is not dict or _hash(body) != record['approval_fingerprint']
                or _hash(value) != record['prepared_binding']
                or body['candidate_id'] != candidate_id or body['plugin_id'] != pid
                or body['expected_revision'] != review['expected_revision']
                or body['current_tuple'] != asdict(old)
                or body['target_code_digest'] != published.code_digest
                or body['manifest'] != publication['manifest']
                or body['compatibility'] != publication['compatibility']
                or body['approval_kind'] != 'migration_A' or body['independent_confirmation_required'] is not True
                or body['permissions'] != list(PERMISSIONS) or body['network_capabilities'] != []
                or body['external_effects'] is not False or body['enabled_after_activation'] is not False
                or body['active_tuple_changed_by_A'] is not False or body['schema_inferred'] is not False
                or body['child_active_data_access'] is not False or body['child_stage_direct_access'] is not False
                or body['data_access_mode'] != 'held_host_migration_proxy_only'
                or type(body['native_spec']) is not dict
                or value['external_side_effects'] is not False
                or body['stage_generation'] != value['data_generation']
                or body['target_schema'] != value['schema']
                or record['execution_request'] != {'call_id': body['call_id'], 'worker_mode': '--plugin-stage-worker'}):
            raise StageError('stage_B_exact_original_candidate_required')
        _opaque(body['call_id']); _sha(value['receipt_sha']); _sha(value['plan_fingerprint'])
        if not body['call_id'].startswith('stage_'):
            raise StageError('stage_B_original_A_request_required')
        _, stage, temporary = self._paths(manager, pid, candidate_id, value['data_generation'])
        code = checked(manager.root / 'code' / pid / published.code_digest)
        if (body['stage_root'] != str(stage) or body['temporary_root'] != str(temporary)
                or body['candidate_code_root'] != str(code) or body['registered_code_root'] != str(code)):
            raise StageError('stage_B_original_candidate_paths_changed')
        def inventory(items):
            if type(items) is not list:
                raise StageError('stage_B_original_inventory_required')
            return [{'path': item['path'], 'sha256': item['sha256'], 'size': item['size']} for item in items]
        source_files = inventory(body['source_inventory']); output_files = inventory(value['files'])
        code_files = inventory(record['code_files'])
        new = ActivationTuple(body['target_code_digest'], value['data_generation'], value['schema'],
            _hash(safe_document(value['config'])), _hash(safe_document(value['state'])), _hash(output_files))
        host = body['host_binding']; binding = host['binding']
        plan = {'plugin_id': pid, 'expected_revision': body['expected_revision'],
            'authority_fingerprint': host['authority'], 'trust_fingerprint': host['trust_fingerprint'],
            'current': body['current_tuple'], 'target_code_digest': body['target_code_digest'],
            'target_schema': body['target_schema'], 'stage_generation': body['stage_generation'],
            'source_files': body['source_inventory'], 'code_files': record['code_files'],
            'hook_path': body['migration']['entrypoint']['path'],
            'hook_sha': body['manifest']['files'][body['migration']['entrypoint']['path']],
            'initial_data_write_epoch': binding['data_write_epoch'],
            'compatibility': body['compatibility'], 'new_empty': False}
        execution_binding = {'plan_fingerprint': value['plan_fingerprint'], 'receipt_sha': value['receipt_sha'],
            'data_generation': new.data_generation, 'schema': new.schema, 'config_sha': new.config_sha,
            'state_sha': new.state_sha, 'inventory_sha': new.data_inventory_sha, 'external_side_effects': False}
        target = next((item for item in review['code'] if item['digest'] == new.code_digest), None)
        if (new != published or host['old'] != asdict(old) or _hash(source_files) != old.data_inventory_sha
                or _hash(plan) != value['plan_fingerprint'] or record['plan_fingerprint'] != value['plan_fingerprint']
                or publication['stage_result'] != execution_binding
                or publication['write_epoch'] != binding['data_write_epoch']
                or publication['effect_epoch'] != binding['data_effect_epoch']
                or publication['old_pending_update'] != binding['pending_update']
                or host['config_sha'] != old.config_sha or host['state_sha'] != old.state_sha
                or any(binding[key] != expected for key, expected in {
                    'id': pid, 'revision': body['expected_revision'], 'digest': old.code_digest,
                    'enabled': 0, 'loaded': 0, 'active_generation': old.data_generation,
                    'data_schema': old.schema, 'migration_id': '', 'data_inventory_dirty': 0,
                    'pending_update': 0}.items())
                or body['source_observation_sha'] != publication['old_observation']
                or record['stage_observation_sha'] != publication['stage_observation']
                or record['code_inventory_sha'] != publication['code_inventory_sha']
                or record['code_observation_sha'] != publication['code_observation']
                or body['registered_code_inventory_sha'] != record['code_inventory_sha']
                or body['registered_code_inventory'] != code_files
                or target is None or target['files'] != code_files or target['manifest'] != body['manifest']):
            raise StageError('stage_B_original_request_source_result_binding_changed')
        linked = {'candidate_id': candidate_id, 'record_sha256': _hash(record),
            'approval_fingerprint': record['approval_fingerprint'], 'prepared_binding': record['prepared_binding'],
            'publication_fingerprint': review['journal_fingerprint'], 'review_fingerprint': _hash(review),
            'source_code_digest':old.code_digest,'target_code_digest':new.code_digest,
            'schema':new.schema,'old_tuple':asdict(old),'new_tuple':asdict(new),
            'stage_result_binding':execution_binding,'current_authority':review['current_authority'],
            'phase_after': 'completed_disabled', 'scope': 'recovered_B_publication_metadata_only',
            'restores_A_owner': False, 'replays_migration_hook': False,
            'notice': '继续发布成功且最终布局完全一致后，仅将此原A持久journal确认为completed_disabled；不重建A执行owner、不重跑hook，也不重新证明A的OS执行身份。'}
        return path, record, linked

    def publication_restore_preview(self, pid, review):
        """Bind the complete original A journal into the NEW, outer B review approval."""
        validate_id(pid); manager = self._manager()
        with manager._lock:
            matches = []
            for item in pending_candidates(manager.root, pid):
                if item['phase'] != 'validated':
                    continue
                try:
                    _, _, linked = self._publication_restore_candidate(manager, pid, item['candidate_id'], review)
                except (DataTransactionError, KeyError, TypeError, ValueError):
                    continue  # B remains independently reviewable; this A stays pending.
                matches.append(linked)
            return matches[0] if len(matches) == 1 else None

    def publication_restored(self, pid, candidate_id, review_result, review_snapshot):
        """Confirm recovered B metadata only; NEVER adopt A or expose prepared().

        Host-only callback after the newly approved forward B call. The immutable B
        intent, its current-authority audit and the exact reviewed A journal must all
        agree with the actual disabled/clean committed registry and final layout.
        """
        from app.plugin_publication_review import PublicationReviewService, REVIEW_MARKER
        validate_id(pid); _opaque(candidate_id); manager = self._manager()
        with manager._lock, manager.execution_coordinator.mutation_guard(pid), ExitStack() as held:
            manager._assert_mutation_idle(pid)
            review = review_snapshot['review']; approved = review_snapshot['linked_stage_confirmation']
            path, record, linked = self._publication_restore_candidate(manager, pid, candidate_id, review)
            if (approved != linked or review_snapshot['review_fingerprint'] != _hash(review)
                    or review_snapshot['host_authority_fingerprint'] != self._authority(manager)):
                raise StageError('stage_B_linked_candidate_reapproval_required')
            transactions = manager.data_transactions()
            verifier = PublicationReviewService(transactions, manager.ownership_authority)
            verifier._assert_authority(review['current_authority'])
            held.enter_context(_held_file(checked(review['current_authority']['plugin_root']), directory=True))
            held.enter_context(_held_file(checked(review['current_authority']['registry']), share_writes=True))
            held.enter_context(_parents_held(path))
            final_layout = verifier._assert_layout(review, 'stage_renamed')
            parents = {parent for item in final_layout.values()
                       for parent in Path(item['path']).parents if parent.exists()}
            for parent in sorted(parents, key=lambda value: len(value.parts)):
                held.enter_context(_held_file(checked(parent), directory=True))
            for item in (*final_layout.values(), *review['code']):
                verifier._hold_fixed_tree(Path(item['path']), item['observation'], held)
            value = record['result']; body = record['approval_snapshot']; publication = review['intent']
            def inventory(items):
                return [{'path': item['path'], 'sha256': item['sha256'], 'size': item['size']} for item in items]
            if (final_layout['active']['files'] != inventory(value['files'])
                    or final_layout['previous']['files'] != inventory(body['source_inventory'])
                    or final_layout['stage']['observation'] != {'missing': True}):
                raise StageError('stage_B_recovered_actual_generation_files_changed')
            with manager._db() as conn:
                row = manager._row(conn, pid, installed=True)
                found = conn.execute('SELECT * FROM plugin_data_transactions WHERE id=? AND plugin_id=?',
                                     (review['journal_fingerprint'], pid)).fetchone()
                tx = dict(found) if found is not None else None
                marker = REVIEW_MARKER + _json({'decision': 'continue_forward',
                    'review_fingerprint': _hash(review)}).decode('utf-8')
                immutable = {key: value for key, value in review['journal'].items()
                             if key not in ('phase', 'updated_at', 'error')}
                generations = [dict(item) for item in conn.execute(
                    'SELECT * FROM plugin_data_generations WHERE plugin_id=? ORDER BY generation', (pid,))]
                expected_generations = []
                for role, activation in (('previous', publication['old']), ('active', publication['new'])):
                    expected_generations.append({'plugin_id': pid, 'generation': activation['data_generation'],
                        'schema': activation['schema'], 'code_digest': activation['code_digest'],
                        'config_sha': activation['config_sha'], 'state_sha': activation['state_sha'],
                        'inventory_sha': activation['data_inventory_sha'], 'role': role})
                if (tx is None or tx['phase'] != 'completed_disabled' or tx['error'] != marker
                        or {key: tx[key] for key in immutable} != immutable
                        or review_result != PublicationReviewService._result(review, tx, 'continue_forward', completed=True)
                        or not conn.execute("SELECT 1 FROM audit WHERE plugin_id=? AND action='plugin_data_restore_review' AND result='continue_forward' AND fingerprint=?",
                                            (pid, _hash(review))).fetchone()
                        or asdict(transactions.current_from_row(conn, row)) != publication['new']
                        or row['revision'] != review['expected_revision'] + 1
                        or row['enabled'] or row['loaded'] or row['pending_update'] or row['migration_id']
                        or row['data_inventory_dirty'] or row['previous'] != publication['old']['code_digest']
                        or row['data_write_epoch'] != publication['write_epoch']
                        or row['data_effect_epoch'] != publication['effect_epoch']
                        or json.loads(row['config']) != value['config'] or json.loads(row['state']) != value['state']
                        or json.loads(row['manifest']) != body['manifest']
                        or sorted(({key: item[key] for key in expected_generations[0]} for item in generations),
                                  key=lambda item: item['generation']) != sorted(expected_generations, key=lambda item: item['generation'])):
                    raise StageError('stage_B_recovered_registry_or_review_unconfirmed')
                verifier._verify_codes(conn, review)
            verifier._assert_authority(review['current_authority'])
            verifier._assert_layout(review, 'stage_renamed')
            if _hash(_read_record(path)) != linked['record_sha256']:
                raise StageError('stage_B_linked_original_journal_changed')
            self._event(path, record, 'completed_disabled', active_tuple_published=True,
                publication_receipt_sha=review['journal_fingerprint'],
                recovery_confirmation={'status': 'recovery-confirmed',
                    'source': 'current_authority_explicit_B_restore_review',
                    'original_record_sha256': linked['record_sha256'], 'review_fingerprint': _hash(review),
                    'host_authority_fingerprint': self._authority(manager),
                    'scope': linked['scope'], 'restores_A_owner': False, 'replays_migration_hook': False})
            with self._lock:
                owner = self._validated_owners.get(candidate_id)
                if owner is not None and owner.get('record_sha') == linked['record_sha256']:
                    owner['phase'] = 'completed_disabled'  # Retire only an existing owner; never create one.
                    self._validated_owners.pop(candidate_id, None)
                self._records.pop(candidate_id, None)
            return '仅确认本次显式复核的B恢复发布已完成，并将原A候选journal记为completed_disabled/recovery-confirmed；未重建或重新证明A执行owner，未重跑迁移hook。'


def pending_candidates(root, pid=None):
    """Read-only host guard. Unknown/corrupt records fail closed, never disappear."""
    if pid is not None:
        validate_id(pid)
    directory = checked(Path(root) / 'runtime-safety' / 'stage-records')
    if not directory.exists():
        return ()
    _ordinary(directory, directory=True); result = []
    for path in sorted(directory.iterdir()):
        if path.suffix != '.json':
            continue  # Atomic-write remnants are not a successful approval record.
        record = _read_record(path)
        if (record.get('format') != 1 or type(record.get('approval_snapshot')) is not dict
                or record.get('phase') not in {'stage_intent', 'code_register_intent', 'code_registered',
                    'generation_create_intent', 'generation_created', 'running', 'validated',
                    'needs_review', 'completed_disabled'}
                or record.get('approval_fingerprint') != _hash(record['approval_snapshot'])
                or record.get('candidate_id') != path.stem):
            raise StageError('stage_unknown_pending_journal')
        validate_id(record['plugin_id']); _opaque(record['candidate_id'])
        _sha(record['approval_snapshot']['target_code_digest'])
        if record.get('phase') != 'completed_disabled' and (pid is None or record.get('plugin_id') == pid):
            result.append({'plugin_id': record['plugin_id'], 'candidate_id': record['candidate_id'],
                           'phase': record['phase'], 'target_code_digest': record['approval_snapshot']['target_code_digest']})
    return tuple(result)


def candidate_code_references(root):
    """Add these pairs to store startup cleanup keep-set; never scans/runs a hook."""
    return {(item['plugin_id'], item['target_code_digest']) for item in pending_candidates(root)}
