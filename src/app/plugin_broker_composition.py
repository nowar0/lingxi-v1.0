"""Host-only ordinary broker composition; importing/building performs no I/O.

``build_runtime_broker_services(manager_provider)`` returns the *actual*
RuntimeBrokerServices accepted by PluginStrictRuntime, not a subclass or an empty
service. ``services.composition`` is a Python host capability, NEVER an RPC route.

Host integration, in this order:
* In the authenticated, explicitly selected-object preview handler call
  ``composition.select_preview_objects(**package_input)``. This records only the
  selected concrete targets, not a directory permission. Do not call it on worker
  input, at execution time, or silently for all manifest permissions.
* For HTTP call prepare_http_body and prepare_http_request (or the explicit
  GET/HEAD convenience method), then put the returned *descriptor* in the explicit
  broker request. A source_id selects this host registry; no plugin body/IP/header
  is accepted. DNS is performed only by that explicit host preview method.
* Model sources are selected from a real HostModelRuntime pending registry through
  prepare_model_source; browser-created CompiledSubmission objects are not inputs.
* Display and human-confirm the existing complete manager sandbox_spec. Selection
  and DNS preview are NOT an execution approval or child grant.

The stock manager calls preview inside its current BEGIN IMMEDIATE transaction.
Observation consequently uses a read-only registry connection under its real lock,
never a nested manager writer. Actual effect bookkeeping exclusively uses _db,
commits before yielding, and retains dirty/epochs on every uncertain outcome.
No key, backup, process, ACL, profile, shell, request or filesystem object is created
by construction. Source selection/preview and execution below are explicit calls.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import base64
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from uuid import UUID

from app.model_host import HostModelRuntime, HostUiPrincipal
from app.plugin_data_transactions import SCHEMA_VERSION
from app.plugin_execution import ExecutionIdentity
from app.plugin_file_proxy import MAX_OBJECT_BYTES, PreparedWrite
from app.plugin_http_proxy import HttpTarget, MAX_BYTES, _endpoint, system_resolver
from app.plugin_manifest import canonical_json
from app.plugin_migrations import safe_document
from app.plugin_proxy import (BrokerSession, TrustedOperation, decode_json, encode_json)
from app.plugin_runtime_brokers import (ObjectScope, PreparedHttpRequest,
    PreparedModelRequest, RuntimeBrokerServices, request_contract)
from app.plugin_scheduler import SchedulerLease
from app.ui_auth import current_principal, sessions

_FIELDS = ('manifest', 'tool', 'args', 'config', 'state', 'plugin_id', 'revision',
           'digest', 'trust_fingerprint', 'dependency_binding')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_LIMIT = 4096


class BrokerCompositionError(ValueError):
    """Bounded diagnostic only: never expose paths, content, key or auth cookies."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise BrokerCompositionError(code)


def _plain(value):
    if hasattr(value, 'items'):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


def _bytes(value):
    return encode_json(_plain(value), safe=True)


def _copy(value):
    return decode_json(_bytes(value))


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _document_hash(value):
    # PluginManager's config/state/manifest fingerprints are canonical JSON, not
    # RPC frames: a host-owned document may legitimately exceed one RPC frame.
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _source(value):
    if any(key not in value for key in _FIELDS):
        _fail('composition_complete_preview_source_required')
    result = _copy({key: value[key] for key in _FIELDS})
    if (any(type(result[key]) is not dict for key in ('manifest', 'args', 'config', 'state'))
            or type(result['dependency_binding']) is not list
            or type(result['revision']) is not int or result['revision'] < 1
            or result['manifest'].get('id') != result['plugin_id']
            or _document_hash(result['manifest']) != result['digest']
            or type(result['trust_fingerprint']) is not str
            or not _SHA.fullmatch(result['trust_fingerprint'])):
        _fail('composition_preview_source_invalid')
    request_contract(result['manifest'], result['tool'], result['args'])
    return result


def _path(value):
    # Lexical check first: never resolve or open an arbitrary worker-provided path.
    if (type(value) is not str or not value or '\x00' in value
            or value.startswith(('\\\\', '//')) or not Path(value).is_absolute()
            or '..' in Path(value).parts or os.path.abspath(value) != value):
        _fail('composition_explicit_canonical_absolute_path_required')
    path = Path(value)
    for part in path.parts[1:]:
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or any(ord(char) < 32 or char in ':<>"|?*' for char in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            _fail('composition_path_alias_forbidden')
    return path


def _directory(path):
    # No enumeration and no ACL changes. FileObjectRegistry separately holds exact
    # no-reparse ancestry/handles and verifies security/content fingerprints.
    path = _path(str(path))
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            _fail('composition_directory_identity_unavailable')
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def _selected_file(path):
    parent = _directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400
            or not 0 <= info.st_size <= MAX_OBJECT_BYTES):
        _fail('composition_existing_ordinary_file_required')
    return {'parent_identity': parent,
            'file_identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]}


@dataclass
class _Selection:
    source_sha256: str
    request_sha256: str
    principal: HostUiPrincipal = field(repr=False)
    authority: dict = field(repr=False)
    target: str
    identity: dict
    expires: float
    claimed_call_id: str | None = None


@dataclass
class _Body:
    source_id: str
    principal: HostUiPrincipal = field(repr=False)
    authority: dict = field(repr=False)
    content: bytes = field(repr=False)
    expires: float


@dataclass
class _Http:
    source_id: str
    principal: HostUiPrincipal = field(repr=False)
    authority: dict = field(repr=False)
    target: HttpTarget
    body: _Body = field(repr=False)
    fingerprint: str
    expires: float


@dataclass
class _Model:
    source_id: str
    principal: HostUiPrincipal = field(repr=False)
    authority: dict = field(repr=False)
    runtime: HostModelRuntime = field(repr=False)
    scope_id: str
    request_id: str
    scope: object = field(repr=False)
    pending: object = field(repr=False)
    params_json: bytes = field(repr=False)
    expires: float


@dataclass
class _Call:
    call_id: str
    source: dict = field(repr=False)
    manager: object = field(repr=False)
    principal: HostUiPrincipal = field(repr=False)
    authority: dict = field(repr=False)
    initial: dict
    epochs: tuple
    expires: float
    plan: object = field(default=None, repr=False)
    startup_noted: bool = False
    epoch_receipts: list = field(default_factory=list, repr=False)
    objects: list = field(default_factory=list, repr=False)
    effect_lock: object = field(default_factory=threading.RLock, repr=False)


class RuntimeBrokerComposition:
    """Private trusted Python facade, not deserializable and not a plugin service.

    manager_provider() must return the exact live PluginManager. model_provider(),
    when supplied, returns the actual server-owned HostModelRuntime. It is read on
    registration AND every model revalidation; missing context never means True.
    principal_provider is used only to capture authenticated host preview ownership.
    Running native callbacks always use the captured owner plus the real reservation,
    never whichever current_principal happens to run on a worker thread.
    """
    def __init__(self, manager_provider, *, model_provider=None,prepared_model_request_provider=None,
                 request_contract_approved=False, clock=time.monotonic,
                 principal_provider=None, live_principal=None, resolver=None):
        if (not callable(manager_provider) or not callable(clock)
                or any(value is not None and not callable(value) for value in
                       (model_provider,prepared_model_request_provider, principal_provider, live_principal, resolver))):
            raise TypeError('explicit trusted host providers required')
        if type(request_contract_approved) is not bool:raise TypeError('explicit SDK contract decision required')
        self.manager_provider, self.model_provider, self.clock = manager_provider, model_provider, clock
        self.prepared_model_request_provider=prepared_model_request_provider
        self.principal_provider = principal_provider or current_principal.get
        self.live_principal = live_principal or sessions.live
        self.resolver = resolver or system_resolver
        self._lock = threading.RLock()
        self._context = ContextVar('lingxi_broker_composition_preview', default=None)
        self._calls, self._selections, self._bodies, self._http, self._models = {}, {}, {}, {}, {}
        self._effect_leases = {}
        self._dns_slots=threading.BoundedSemaphore(4)
        self._closed = False
        self.services = RuntimeBrokerServices(object_scope=self.object_scope,
            http_request=self.http_request,
            model_request=self.model_request if model_provider is not None or prepared_model_request_provider is not None else None,
            effect_guard=self.effect_guard,request_contract_approved=request_contract_approved, clock=clock)
        # PluginStrictRuntime deliberately requires exact RuntimeBrokerServices.
        # Per-instance host wrappers retain that type and the original real adapters.
        self._prepare = self.services.prepare
        self._invocation = self.services.invocation
        self._close = self.services.close
        self.services.prepare = self.prepare
        self.services.invocation = self.invocation
        self.services.close = self.close
        self.services.composition = self

    def _manager(self):
        from app.plugin_store import PluginManager
        manager = self.manager_provider()
        if (not isinstance(manager, PluginManager) or not manager._initialized
                or not manager._registry_ready or manager._closing or manager._lease is None
                or manager._authority_nonce is None):
            _fail('composition_live_manager_required')
        if self._closed:
            _fail('composition_closed')
        return manager

    def _owner(self):
        principal = self.principal_provider()
        self._owner_live(principal)
        return principal

    def _owner_live(self, principal):
        if type(principal) is not HostUiPrincipal or self.live_principal(principal) != principal:
            _fail('composition_authenticated_preview_owner_required')

    def _same_authority(self, manager, expected):
        if self._manager() is not manager or manager.ownership_authority() != expected:
            _fail('composition_manager_ownership_changed')

    def _registry(self, manager, source):
        """Actual committed registry observation; never a nested manager writer.

        _call_snapshot invokes this while the stock manager already owns its writer.
        All callers hold manager._lock; a read-only connection cannot accidentally
        mutate epochs, run an upgrade or create a missing registry. No credentials
        column is selected or decoded.
        """
        if not getattr(manager._db_depth, 'value', 0):
            manager._sync_trust()
        uri = manager.db_path.as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            row = db.execute('''SELECT id,revision,installed,enabled,digest,manifest,config,state,
                active_generation,data_schema,data_write_epoch,data_effect_epoch,
                data_inventory_dirty,migration_id FROM plugins WHERE id=?''',
                (source['plugin_id'],)).fetchone()
            if row is None or version != SCHEMA_VERSION:
                _fail('composition_registry_or_plugin_unavailable')
            row = dict(row)
            if (not row['installed'] or not row['enabled'] or row['migration_id']
                    or row['revision'] != source['revision'] or row['digest'] != source['digest']
                    or json.loads(row['manifest']) != source['manifest']
                    or json.loads(row['config']) != source['config']
                    or json.loads(row['state']) != source['state']
                    or manager._trust_faulted
                    or _hash(manager._trust_binding()) != source['trust_fingerprint']):
                _fail('composition_preview_source_changed')
            if manager._signature(source['manifest'])['status'] in ('invalid', 'revoked'):
                _fail('composition_source_trust_changed')
            if manager._execution_dependency_binding(db, source['manifest']) != source['dependency_binding']:
                _fail('composition_dependency_binding_changed')
            epochs = (row['data_write_epoch'], row['data_effect_epoch'])
            if any(type(value) is not int or not 0 <= value < 2**53 for value in epochs):
                _fail('composition_epoch_invalid')
            generation = None
            if row['active_generation']:
                if str(UUID(row['active_generation'])) != row['active_generation']:
                    _fail('composition_generation_invalid')
                entry = db.execute('''SELECT * FROM plugin_data_generations
                    WHERE plugin_id=? AND generation=?''',
                    (row['id'], row['active_generation'])).fetchone()
                if entry is None:
                    _fail('composition_active_generation_missing')
                generation = dict(entry)
                if (generation['role'] != 'active' or generation['code_digest'] != row['digest']
                        or generation['schema'] != row['data_schema']
                        or generation['config_sha'] != _document_hash(safe_document(source['config']))
                        or generation['state_sha'] != _document_hash(safe_document(source['state']))):
                    _fail('composition_active_generation_binding_changed')
            elif row['data_schema'] is not None:
                _fail('composition_schema_without_active_generation')
            return {'registry_schema': version, 'active_generation': row['active_generation'],
                    'data_schema': row['data_schema'], 'generation': generation,
                    'write_epoch': epochs[0], 'effect_epoch': epochs[1],
                    'data_inventory_dirty': row['data_inventory_dirty']}
        finally:
            db.close()

    @staticmethod
    def _stable(observation):
        return {key: value for key, value in observation.items()
                if key not in ('write_epoch', 'effect_epoch', 'data_inventory_dirty')}

    def select_object_source(self, source, request):
        """Host-only exact user selection BEFORE any content is read.

        source is the complete runtime package input, not a worker envelope. The
        selection authorizes snapshotting exactly ONE matching request/target for
        preview, not executing it, sibling access, an ACL or a reusable root grant.
        """
        source = _source(source)
        requests, _ = request_contract(source['manifest'], source['tool'], source['args'])
        request = _copy(request)
        if request not in requests or request['op'] not in ('file.read', 'file.write', 'data.read', 'data.write'):
            _fail('composition_exact_declared_object_selection_required')
        owner, manager = self._owner(), self._manager()
        path = _path(request['target'])
        with manager._lock:
            authority = manager.ownership_authority()
            observation = self._registry(manager, source)
            data = _path(str(manager.root / 'data' / source['plugin_id']))
            if request['op'].startswith('data.'):
                if (not observation['active_generation'] or path == data or not path.is_relative_to(data)):
                    _fail('composition_exact_active_data_root_required')
                _directory(data)
            elif path == manager.root or path.is_relative_to(manager.root):
                _fail('composition_managed_paths_require_data_scope')
            identity = _selected_file(path)
            self._same_authority(manager, authority)
        key = (_hash(source), _hash(request))
        with self._lock:
            self._purge_sources()
            if len(self._selections) >= _LIMIT and key not in self._selections:
                _fail('composition_selection_capacity')
            self._selections[key] = _Selection(key[0], key[1], owner, authority, str(path),
                                                identity, self.clock() + 120)
        return {'selection_id': _hash({'source': key[0], 'request': key[1], 'identity': identity}),
                'target': str(path), 'operation': request['op'], 'identity': _copy(identity),
                'scope': 'one_explicit_object_snapshot_only', 'execution_authorized': False}

    def select_preview_objects(self, **package_input):
        """Convenience for a host handler that has explicitly selected THESE objects.

        Never install this as an automatic worker-path resolver. HTTP/model source
        registration is separate; permissions and manifest paths are not selection.
        """
        source = _source(package_input)
        requests, _ = request_contract(source['manifest'], source['tool'], source['args'])
        return [self.select_object_source(source, request) for request in requests
                if request['op'].startswith(('file.', 'data.'))]

    def _purge_sources(self):
        now = self.clock()
        for table in (self._selections, self._bodies, self._http, self._models):
            for key, entry in tuple(table.items()):
                if entry.expires <= now:
                    table.pop(key)

    def _reservation(self, record, *, running=False):
        manager = record.manager
        reservation = manager._manager_execution_bindings.get(record.call_id)
        if reservation is None:
            return None
        identity = reservation.get('identity')
        source = record.source
        if (type(identity) is not ExecutionIdentity or identity.call_id != record.call_id
                or identity.plugin_id != source['plugin_id'] or identity.revision != source['revision']
                or identity.digest != source['digest'] or identity.trust_fingerprint != source['trust_fingerprint']
                or record.call_id not in manager._manager_call_ids or reservation.get('mode') != 'ordinary'):
            _fail('composition_exact_manager_reservation_required')
        keys = [key for key, value in manager._execution_reservations.items() if value is reservation]
        if len(keys) != 1 or manager._ui_approval_owners.get(keys[0]) != record.principal:
            _fail('composition_consumed_preview_owner_changed')
        snapshot = reservation.get('snapshot', {})
        spec = snapshot.get('sandbox_spec', {})
        if (record.plan is None or spec.get('call_id') != record.call_id
                or spec.get('broker_scope') != record.plan.projection()
                or snapshot.get('id') != source['plugin_id']
                or snapshot.get('expected_revision') != source['revision']
                or snapshot.get('digest') != source['digest'] or snapshot.get('args') != source['args']
                or snapshot.get('tool') != source['tool']
                or snapshot.get('config_fingerprint') != _document_hash(source['config'])
                or snapshot.get('state_fingerprint') != _document_hash(source['state'])
                or snapshot.get('dependency_binding') != source['dependency_binding']
                or _hash(snapshot.get('trust')) != source['trust_fingerprint']):
            _fail('composition_consumed_fullspec_changed')
        coordinator = manager.execution_coordinator
        with coordinator._lock:
            call = coordinator._calls.get(record.call_id)
            if (coordinator._closed or call is None or call.identity != identity or call.finalized
                    or call.cancel_event.is_set() or call.quarantined):
                _fail('composition_coordinator_reservation_changed')
            if running and (not call.running or not call.started or not call.resources_active
                    or not reservation.get('started') or type(call.lease) is not SchedulerLease):
                _fail('composition_actual_running_call_lease_required')
            if running:
                scheduler = coordinator.scheduler
                with scheduler._condition:
                    if (call.lease._scheduler is not scheduler or call.lease._released
                            or call.lease._quarantined
                            or scheduler._running.get(record.call_id) is not call.lease
                            or getattr(call.lease, 'cancel_event', None) is not call.cancel_event):
                        _fail('composition_actual_running_call_lease_required')
        return reservation, call

    def _live(self, record):
        self._owner_live(record.principal)
        manager = record.manager
        with manager._lock:
            self._same_authority(manager, record.authority)
            reservation = self._reservation(record)
            if reservation is None:
                if self.clock() >= record.expires or self.principal_provider() != record.principal:
                    _fail('composition_preview_owner_or_expiry_changed')
            observation = self._registry(manager, record.source)
            if self._stable(observation) != self._stable(record.initial):
                _fail('composition_root_generation_or_schema_changed')
            epochs = (observation['write_epoch'], observation['effect_epoch'])
            # The real manager performs exactly this conservative advance before
            # runtime entry. Adopt it ONCE, only for this exact started reservation.
            # No arbitrary changed epoch, even a larger epoch, is ever followed.
            if (not record.startup_noted and reservation is not None
                    and reservation[0].get('started') and reservation[1].started):
                delta = 1 if record.initial['active_generation'] else 0
                expected = tuple(value + delta for value in record.epochs)
                if epochs != expected or delta and observation['data_inventory_dirty'] != 1:
                    _fail('composition_managed_start_epoch_changed')
                record.epochs = expected
                record.startup_noted = True
                record.epoch_receipts.append({'kind': 'exact_manager_note_managed_execution',
                                             'write_epoch': expected[0], 'effect_epoch': expected[1]})
            if epochs != record.epochs:
                _fail('composition_unowned_epoch_change')
            for path, parent_identity in record.objects:
                if _directory(Path(path).parent) != parent_identity:
                    _fail('composition_selected_parent_identity_changed')
            return True

    def prepare(self, **package_input):
        source = _source(package_input)
        call_id = package_input.get('call_id')
        if type(call_id) is not str or not re.fullmatch(r'[0-9a-f]{32}', call_id):
            _fail('composition_exact_call_id_required')
        manager = self._manager()
        # Lock ordering is manager -> composition. Never hold a SQLite writer
        # across an external callback/effect. The stock prepare remains real.
        with manager._lock:
            with self._lock:
                record = self._calls.get(call_id)
            if record is not None:
                if record.manager is not manager or record.source != source:
                    _fail('composition_prestart_source_changed')
                self._live(record)
                context=self._context.set(record)
                try:return self._prepare(**package_input)
                finally:self._context.reset(context)
            owner = self._owner()
            authority = manager.ownership_authority()
            observation = self._registry(manager, source)
            record = _Call(call_id, source, manager, owner, authority, observation,
                           (observation['write_epoch'], observation['effect_epoch']), self.clock() + 120)
            with self._lock:
                if self._closed or call_id in self._calls or len(self._calls) >= _LIMIT:
                    _fail('composition_call_capacity_or_duplicate')
                self._calls[call_id] = record
            token = self._context.set(record)
            try:
                plan = self._prepare(**package_input)
                public = plan.projection()
                public['host_composition'] = {
                    'schema': 'lingxi.ordinary-broker-composition.v1',
                    'owner_session_id_sha256': hashlib.sha256(owner.owner_session_id.encode()).hexdigest(),
                    'owner_authority_fingerprint': owner.authority_fingerprint,
                    'manager_authority': _copy(authority), 'generation_binding': _copy(observation),
                    'epoch_policy': 'exact managed start +1/+1 for registered generation, then only this call durable effect receipts',
                    'objects_require_explicit_host_selection': True,
                    'http_exact_bodies': [
                        {'source_id': prepared.target.request_descriptor,
                         'method': prepared.target.method, 'url': prepared.target.url,
                         'headers': [list(pair) for pair in prepared.target.headers],
                         'body_base64': base64.b64encode(prepared.body).decode('ascii'),
                         'body_sha256': prepared.target.body_sha256,
                         'body_bytes': len(prepared.body),
                         'body_text_utf8': self._utf8_preview(prepared.body),
                         'allow_private': False, 'allow_plain_http': False,
                         'allow_redirects': False, 'no_proxy_retry_or_host_auth_material': True}
                        for prepared in plan.http],
                    'unknown_outcomes_remain_dirty': True, 'execution_grant_minted': False}
                plan = replace(plan, public_json=_bytes(public))
                with self.services._lock:
                    self.services._plans[call_id] = plan
                record.plan = plan
                self._live(record)
                return plan
            except BaseException:
                # Retain the failed call ID/owners; no retry can silently reuse a
                # partially observed/registered plan or a new owner.
                raise
            finally:
                self._context.reset(token)

    @staticmethod
    def _utf8_preview(body):
        try:
            text = body.decode('utf-8')
            return None if '\x00' in text else text
        except UnicodeDecodeError:
            return None  # Base64 remains the complete byte-for-byte preview.

    def _provider_call(self, source, request):
        record = self._context.get()
        if record is None or record.source != _source(source) or source.get('call_id',record.call_id)!=record.call_id:
            _fail('composition_host_preview_context_required')
        self._live(record)
        requests, _ = request_contract(source['manifest'], source['tool'], source['args'])
        if request not in requests:
            _fail('composition_exact_declared_request_required')
        return record

    def object_scope(self, source, request):
        record = self._provider_call(source, request)
        key = (_hash(record.source), _hash(request))
        for prior_request,prior_scope in getattr(record,'_object_scopes',[]):
            if prior_request==_hash(request):
                prior_scope.revalidate()
                return prior_scope
        with self._lock:
            selection = self._selections.get(key)
        if (selection is None or selection.principal != record.principal
                or selection.authority != record.authority or selection.expires <= self.clock()
                or selection.claimed_call_id is not None
                or selection.target != request['target']):
            _fail('composition_explicit_object_selection_required')
        with self._lock:
            # One selection permits one preview snapshot, not repeated calls from
            # a remembered path/parent. A new call needs a fresh explicit selection.
            if selection.claimed_call_id is not None:
                _fail('composition_object_selection_already_claimed')
            selection.claimed_call_id = record.call_id
        path = _path(selection.target)
        if _selected_file(path) != selection.identity:
            _fail('composition_selected_file_identity_changed')
        kind = request['op'].split('.')[0]
        data = _path(str(record.manager.root / 'data' / source['plugin_id']))
        if kind == 'data':
            if (not record.initial['active_generation'] or path == data or not path.is_relative_to(data)):
                _fail('composition_exact_active_data_root_required')
            # FileObjectRegistry requires data_root to be strictly INSIDE root.
            root, data_root = str(data.parent), str(data)
            data_identity = _directory(data)
        else:
            if path == record.manager.root or path.is_relative_to(record.manager.root):
                _fail('composition_managed_paths_require_data_scope')
            root, data_root, data_identity = str(path.parent), None, None
        binding = {'schema': 'lingxi.exact-object-scope.v1', 'call_id': record.call_id,
            'manager_authority': record.authority, 'plugin_id': source['plugin_id'],
            'revision': source['revision'], 'digest': source['digest'],
            'owner_session_id_sha256': hashlib.sha256(record.principal.owner_session_id.encode()).hexdigest(),
            'owner_authority_fingerprint': record.principal.authority_fingerprint,
            'generation_binding': record.initial, 'kind': kind, 'target': str(path),
            'operation': request['op'], 'params_sha256': _hash(request['params']),
            'selected_identity': selection.identity, 'data_root_identity': data_identity,
            'root_is_broker_ancestry_not_directory_permission': True}
        frozen = _bytes(binding)
        record.objects.append((str(path), selection.identity['parent_identity']))
        def live(scope):
            try:
                if (scope.binding_json != frozen or scope.approved_root != root
                        or scope.data_root != data_root or self._live(record) is not True):
                    return False
                return data_root is None or _directory(Path(data_root)) == data_identity
            except Exception:
                return False
        value=ObjectScope(root, 'scope_' + _hash(binding), frozen, live, data_root)
        if not hasattr(record,'_object_scopes'):record._object_scopes=[]
        record._object_scopes.append((_hash(request),value))
        return value

    def prepare_http_body(self, body):
        """Explicit host-only immutable bytes registration; no DNS or sending."""
        if type(body) is not bytes or len(body) > MAX_BYTES:
            _fail('composition_exact_bounded_http_body_required')
        owner, manager = self._owner(), self._manager()
        with manager._lock:
            authority = manager.ownership_authority()
        source_id = 'body_' + secrets.token_hex(16)
        entry = _Body(source_id, owner, authority, body, self.clock() + 120)
        with self._lock:
            self._purge_sources()
            if len(self._bodies) >= _LIMIT:
                _fail('composition_http_body_capacity')
            self._bodies[source_id] = entry
        return {'source_id': source_id, 'body_sha256': hashlib.sha256(body).hexdigest(),
                'body_bytes': len(body), 'sent': False}

    def prepare_http_request(self, *, body_source_id, url, method, max_response_bytes,
                             headers=()):
        """Explicit host DNS preview + exact request registry, NEVER an HTTP send.

        Plaintext, private, multicast and unspecified destinations are forbidden.
        A separate future explicit user exception flow is needed to relax policy;
        no JSON boolean, environment proxy, guessed method or default body does so.
        """
        owner, manager = self._owner(), self._manager()
        with self._lock:
            body = self._bodies.get(body_source_id)
        if body is None or body.principal != owner or self.clock() >= body.expires:
            _fail('composition_host_http_body_source_required')
        with manager._lock:
            self._same_authority(manager, body.authority)
        scheme, host, port, _, _ = _endpoint(url)
        if scheme != 'https':
            _fail('composition_plain_http_requires_separate_user_approval')
        if method not in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'):
            _fail('composition_explicit_http_method_required')
        if method in ('GET', 'HEAD') and body.content:
            _fail('composition_get_head_body_must_be_empty')
        # This function is intentionally never called by construction or by the
        # runtime provider. It is the host's explicit, user-selected DNS preview.
        if not self._dns_slots.acquire(blocking=False):_fail('composition_dns_preview_capacity')
        ready=threading.Event();resolved={}
        def resolve():
            try:resolved['values']=self.resolver(host,port)
            except BaseException:resolved['failed']=True
            finally:self._dns_slots.release();ready.set()
        threading.Thread(target=resolve,name='lingxi-preview-dns',daemon=True).start()
        if not ready.wait(5):_fail('composition_dns_preview_timeout')
        if resolved.get('failed'):_fail('composition_dns_preview_unavailable')
        values=resolved['values']
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 16:
            _fail('composition_exact_dns_set_required')
        try:
            ips = tuple(sorted({str(ipaddress.ip_address(value)) for value in values
                                if type(value) is str and '%' not in value}))
        except ValueError:
            _fail('composition_dns_address_invalid')
        if len(ips) != len(set(values)):
            _fail('composition_dns_address_invalid')
        source_id = 'http_' + secrets.token_hex(16)
        target = HttpTarget(source_id, url, method, hashlib.sha256(body.content).hexdigest(),
                            max_response_bytes, ips, body.source_id, headers=tuple(headers))
        self._owner_live(owner)
        with manager._lock:
            self._same_authority(manager, body.authority)
        entry = _Http(source_id, owner, body.authority, target, body, target.fingerprint,
                      min(body.expires, self.clock() + 120))
        with self._lock:
            self._purge_sources()
            if len(self._http) >= _LIMIT:
                _fail('composition_http_source_capacity')
            self._http[source_id] = entry
        return {'source_id': source_id, 'params': target.descriptor(), 'sent': False,
                'dns_preview_only': True, 'separate_complete_call_approval_required': True}

    def prepare_https_empty_request(self, *, url, method, max_response_bytes):
        """Host convenience; caller must explicitly choose GET or HEAD."""
        if method not in ('GET', 'HEAD'):
            _fail('composition_explicit_get_or_head_required')
        body = self.prepare_http_body(b'')
        return self.prepare_http_request(body_source_id=body['source_id'], url=url,
            method=method, max_response_bytes=max_response_bytes)

    def http_request(self, source, request):
        record = self._provider_call(source, request)
        source_id = request['params'].get('request_descriptor')
        with self._lock:
            entry = self._http.get(source_id)
        if entry is None:
            _fail('composition_host_registered_http_source_required')
        params = _copy(request['params'])
        def live(prepared):
            try:
                with self._lock:
                    current = self._http.get(source_id)
                    body = self._bodies.get(entry.body.source_id)
                if (current is not entry or body is not entry.body or entry.principal != record.principal
                        or entry.authority != record.authority or self.clock() >= entry.expires
                        or prepared.target is not entry.target or prepared.body is not entry.body.content
                        or entry.target.fingerprint != entry.fingerprint
                        or entry.target.descriptor() != params or entry.target.url != request['target']
                        or hashlib.sha256(prepared.body).hexdigest() != params.get('body_sha256')
                        or entry.target.method in ('GET', 'HEAD') and prepared.body):
                    return False
                return self._live(record)
            except Exception:
                return False
        prepared = PreparedHttpRequest(entry.target, entry.body.content, live)
        prepared.validate()
        return prepared

    def _host_model(self):
        if self.model_provider is None:
            _fail('composition_real_host_model_runtime_required')
        runtime = self.model_provider()
        if type(runtime) is not HostModelRuntime:
            _fail('composition_real_host_model_runtime_required')
        return runtime

    def prepare_model_source(self, *, scope_id, request_id, max_input_tokens):
        """Select a real server pending record, never browser compiled payloads.

        The UI may select the returned source_id as params.prompt_id. Registration
        does not approve final model sending or create a plugin runtime invocation.
        """
        owner, manager, runtime = self._owner(), self._manager(), self._host_model()
        if type(max_input_tokens) is not int or not 1 <= max_input_tokens <= 1000000:
            _fail('composition_exact_model_input_limit_required')
        with manager._lock:
            authority = manager.ownership_authority()
        with runtime._lock:
            scope = runtime._scope(owner, scope_id)
            pending = scope.requests.get(request_id)
            if (pending is None or pending.started or pending.finished or pending.token is not None
                    or pending.invalidated or pending.cancel.is_set()):
                _fail('composition_real_unconsumed_model_pending_required')
            runtime._recheck(scope, pending)
            compiled = pending.compiled
            if compiled.ready_for_verified_adapter is not True:
                _fail('composition_verified_model_materials_required')
            manifest = json.loads(compiled.submission_manifest_bytes)
            source_id = 'model_' + secrets.token_hex(16)
            route = compiled.route
            params = {'profile_id': route.profile_id, 'provider': route.provider_id, 'model': route.model,
                'prompt_id': source_id, 'prompt_sha256': manifest['text_sha256'],
                'max_input_tokens': max_input_tokens, 'max_output_tokens': route.max_output_tokens}
            entry = _Model(source_id, owner, authority, runtime, scope_id, request_id, scope, pending,
                           _bytes(params), min(scope.expires, pending.expires, self.clock() + 60))
        with self._lock:
            self._purge_sources()
            if len(self._models) >= _LIMIT:
                _fail('composition_model_source_capacity')
            self._models[source_id] = entry
        # Validation uses the actual server callback, even for text-only inputs.
        self._prepared_model(entry, None).validate(params)
        return {'source_id': source_id, 'params': params, 'sent': False,
                'final_send_requires_separate_ui_approval': True}

    def _prepared_model(self, entry, record):
        def revalidate(compiled):
            try:
                self._owner_live(entry.principal)
                manager = self._manager()
                with manager._lock:
                    self._same_authority(manager, entry.authority)
                    if record is not None and self._live(record) is not True:
                        return False
                runtime = self._host_model()
                if runtime is not entry.runtime or self.clock() >= entry.expires:
                    return False
                with self._lock:
                    if self._models.get(entry.source_id) is not entry:
                        return False
                with runtime._lock:
                    scope = runtime._scope(entry.principal, entry.scope_id)
                    pending = scope.requests.get(entry.request_id)
                    if (scope is not entry.scope or pending is not entry.pending
                            or pending.compiled is not compiled or pending.started or pending.finished
                            or pending.token is not None or pending.invalidated or pending.cancel.is_set()):
                        return False
                    return runtime._recheck(scope, pending) is True
            except Exception:
                return False
        return PreparedModelRequest(entry.pending.compiled, entry.params_json,
                                    entry.principal.owner_session_id, revalidate)

    def model_request(self, source, request):
        record = self._provider_call(source, request)
        if self.prepared_model_request_provider is not None:
            prepared=self.prepared_model_request_provider({**record.source,'call_id':record.call_id},request)
            if type(prepared) is not PreparedModelRequest:
                _fail('composition_real_host_model_runtime_required')
            prepared.validate(request['params'])
            return prepared
        source_id = request['params'].get('prompt_id')
        with self._lock:
            entry = self._models.get(source_id)
        if (entry is None or entry.principal != record.principal
                or entry.authority != record.authority or entry.params_json != _bytes(request['params'])):
            _fail('composition_host_registered_model_source_required')
        prepared = self._prepared_model(entry, record)
        prepared.validate(request['params'])
        return prepared

    def invocation(self, plan, *, live_authority):
        with self._lock:
            record = self._calls.get(getattr(plan, 'call_id', None))
        if record is None or record.plan is not plan or not callable(live_authority):
            _fail('composition_exact_prepared_plan_required')
        with record.manager._lock:
            self._live(record)
            if self._reservation(record, running=True) is None:
                _fail('composition_actual_running_reservation_required')
        def authority():
            try:
                return self._live(record) is True and live_authority() is True
            except Exception:
                return False
        return self._invocation(plan, live_authority=authority)

    def _effect_operation(self, record, operation, prepared):
        if type(operation) is not TrustedOperation or type(operation.binding) is not BrokerSession:
            _fail('composition_trusted_operation_required')
        if operation.op not in ('file.write', 'data.write', 'http.request'):
            _fail('composition_effect_operation_invalid')
        if not math.isfinite(operation.deadline) or self.clock() >= operation.deadline:
            _fail('composition_effect_deadline')
        with self.services._lock:
            adapter = self.services._active.get(record.call_id)
        if (adapter is None or adapter.plan is not record.plan or adapter.binding != operation.binding
                or adapter.cancel is None or adapter.cancel.is_set()
                or adapter.deadline is None or operation.deadline > adapter.deadline
                or adapter.live_binding(operation.binding) != operation.binding):
            _fail('composition_actual_native_binding_required')
        # TrustedOperation has params, NOT params_json. Hash the real canonical
        # mapping; no serialized prepared/live flag authorizes an operation.
        raw = _bytes(operation.params)
        if (hashlib.sha256(raw).hexdigest() != operation.params_sha256
                or not any(item.op == operation.op and item.params_json == raw
                           for item in record.plan.operations)):
            _fail('composition_effect_outside_consumed_intent')
        if operation.op == 'http.request':
            if type(prepared) is not PreparedHttpRequest or prepared not in record.plan.http:
                _fail('composition_exact_prepared_http_required')
            prepared.validate()
            if prepared.target.descriptor() != _plain(operation.params):
                _fail('composition_effect_http_descriptor_changed')
        elif prepared is not None:
            if (type(prepared) is not PreparedWrite
                    or prepared.snapshot.get('params_sha256') != operation.params_sha256
                    or prepared.snapshot.get('old_descriptor_sha256') != operation.object_descriptor_sha256):
                _fail('composition_effect_prepared_write_changed')
        self._live(record)
        return adapter

    @contextmanager
    def _call_data_lease(self, record):
        """Borrow the real running call reservation, NOT an idle/self-lock check.

        The existing admission lease remains owned by its coordinator. _mutating
        prevents new call registration for this plugin during external I/O; exact
        inspection excludes this one running identity but rejects every other live
        same-plugin call/reservation. No global manager/coordinator lock is held
        across I/O, so cancellation/trust invalidation cannot deadlock on this guard.
        """
        manager, pid = record.manager, record.source['plugin_id']
        token = object()
        with manager._lock:
            self._live(record)
            reservation = self._reservation(record, running=True)
            if reservation is None:
                _fail('composition_actual_call_data_lease_required')
            coordinator = manager.execution_coordinator
            with coordinator._lock:
                if pid in coordinator._mutating:
                    _fail('composition_plugin_data_lease_busy')
                if any(other is not reservation[1] and other.identity.plugin_id == pid
                       and (not other.finalized or other.resources_active)
                       for other in coordinator._calls.values()):
                    _fail('composition_other_plugin_call_active')
                if any(other is not reservation[0] and other['identity'].plugin_id == pid
                       for other in manager._manager_execution_bindings.values()):
                    _fail('composition_other_manager_reservation_active')
                with self._lock:
                    if pid in self._effect_leases:
                        _fail('composition_plugin_data_lease_busy')
                    self._effect_leases[pid] = (token, record.call_id, reservation[1].lease)
                coordinator._mutating.add(pid)
        try:
            yield
        finally:
            with manager._lock, coordinator._lock, self._lock:
                owned = self._effect_leases.get(pid)
                if owned is None or owned[0] is not token or pid not in coordinator._mutating:
                    # Unknown ownership is not evidence that another lease is free.
                    _fail('composition_data_lease_owner_changed')
                del self._effect_leases[pid]
                coordinator._mutating.remove(pid)

    @contextmanager
    def effect_guard(self, operation, prepared):
        call_id = getattr(getattr(operation, 'binding', None), 'call_id', None)
        with self._lock:
            record = self._calls.get(call_id)
        if record is None:
            _fail('composition_effect_plan_unknown')
        with record.effect_lock, self._call_data_lease(record):
            self._effect_operation(record, operation, prepared)
            manager, pid = record.manager, record.source['plugin_id']
            write_delta = int(operation.op == 'data.write')
            effect_delta = int(operation.op in ('file.write', 'http.request'))
            with manager._lock:
                if getattr(manager._db_depth, 'value', 0):
                    _fail('composition_effect_requires_separate_durable_transaction')
                self._live(record)
                old = record.epochs
                new = (old[0] + write_delta, old[1] + effect_delta)
                if max(new) >= 2**53:
                    _fail('composition_effect_epoch_exhausted')
                # Do NOT call data_transactions().note_write(): it opens another
                # _db and cannot run in the manager's current writer callback.
                # This is the identical conservative durable counter/dirty update,
                # with a stricter exact-generation/epoch CAS on the real writer.
                with manager._db() as db:
                    if db.execute('PRAGMA synchronous').fetchone()[0] < 2:
                        _fail('composition_durable_registry_sync_required')
                    self._same_authority(manager, record.authority)
                    current = db.execute('''SELECT revision,digest,active_generation,data_schema,
                        data_write_epoch,data_effect_epoch,migration_id FROM plugins WHERE id=?''',
                        (pid,)).fetchone()
                    if (current is None or current['revision'] != record.source['revision']
                            or current['digest'] != record.source['digest']
                            or current['active_generation'] != record.initial['active_generation']
                            or current['data_schema'] != record.initial['data_schema']
                            or current['migration_id']
                            or (current['data_write_epoch'], current['data_effect_epoch']) != old):
                        _fail('composition_effect_owner_generation_or_epoch_changed')
                    changed = db.execute('''UPDATE plugins SET data_write_epoch=?,data_effect_epoch=?,
                        data_inventory_dirty=1 WHERE id=? AND revision=? AND digest=?
                        AND active_generation=? AND data_write_epoch=? AND data_effect_epoch=?
                        AND migration_id='' ''', (new[0], new[1], pid, record.source['revision'],
                        record.source['digest'], record.initial['active_generation'], old[0], old[1]))
                    if changed.rowcount != 1:
                        _fail('composition_effect_epoch_cas_failed')
                    manager._audit(db, pid, 'strict_broker_effect', 'intent_durable_before_io',
                        _hash({'call_id': call_id, 'op': operation.op,
                               'params_sha256': operation.params_sha256,
                               'write_epoch': new[0], 'effect_epoch': new[1]}))
                # Advance only AFTER a successful real commit. Never undo either
                # side if liveness/cancellation/publication subsequently fails.
                record.epochs = new
                record.epoch_receipts.append({'kind': operation.op,
                    'params_sha256': operation.params_sha256, 'write_epoch': new[0], 'effect_epoch': new[1]})
            self._effect_operation(record, operation, prepared)
            try:
                yield
            finally:
                # Registry epoch/dirty/audit persist on timeout, cancellation,
                # failed staging, unknown HTTP response and post-publication error.
                # No inventory hash refresh, clean flag, rollback or epoch restore.
                self._effect_operation(record, operation, prepared)

    def revoke_source(self, source_id):
        """Host-only owner revocation; does not revoke/undo already performed I/O."""
        owner = self._owner()
        with self._lock:
            for table in (self._bodies, self._http, self._models):
                entry = table.get(source_id)
                if entry is not None:
                    if entry.principal != owner:
                        _fail('composition_source_owner_changed')
                    table.pop(source_id)
                    return True
        return False

    def close(self):
        with self._lock:
            self._closed = True
        # The real service retires adapters and pending models; never release the
        # coordinator admission lease, delete stages or restore registry epochs.
        self._close()


def build_runtime_broker_services(manager_provider, *, model_provider=None,prepared_model_request_provider=None,
                                  request_contract_approved=False,clock=time.monotonic, principal_provider=None,
                                  live_principal=None, resolver=None):
    """Build real services for PluginStrictRuntime(broker_services=...), with no IO.

    Keep the returned instance on the host. ``services.composition`` is its explicit
    preview/source-selection capability, NOT an attribute to export over RPC. A
    missing model_provider disables only model pending material preparation; actual
    file/data and pinned HTTPS adapters plus durable effect guards remain installed.
    """
    return RuntimeBrokerComposition(manager_provider, model_provider=model_provider,
        prepared_model_request_provider=prepared_model_request_provider,request_contract_approved=request_contract_approved,
        clock=clock, principal_provider=principal_provider, live_principal=live_principal,
        resolver=resolver).services
