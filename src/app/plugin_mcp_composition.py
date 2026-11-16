"""Private production composition for the package-owned strict stdio MCP runtime.

Construction performs no I/O. Preview reads the installed, manifest-validated package
and the independently shipped portable runtime; a prospective root is ONLY intent.
The manager, its consumed opaque approval, its existing coordinator/cancellation
owner and its exact live reservation are required before any preparation mutation.
No HTTP transport, environment inheritance, external permission adapter, legacy
worker, dynamically imported provider, automatic approval or execution retry exists.

Availability below means real read-only Medium/source/API availability, NOT a
successful AppContainer/Job/profile/ACL test. The 30s/512MiB/20%/one-process values
are the fixed MCP POLICY, not observations of a process that has not been created.
Only the existing native launcher can observe/enforce those values on its child.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from app.plugin_execution import ExecutionContext, ExecutionIdentity
from app.plugin_mcp_strict import (McpLaunchSpec, McpManagerApproval,
    McpPreparedComposition, PluginMcpStrictRuntime, make_mcp_launch_spec)
from app.plugin_runtime import RuntimeFailure
from app.plugin_runtime_preparation import (PreparationOwner, WORKER_EXE,
    RUNTIME_MANIFEST, MAX_MANIFEST_BYTES, canonical_bytes, checked_local, digest,
    file_record, observe_source, parse_json, prepare_tree, read_code_inventory,
    read_portable_runtime)
from app.plugin_sandbox_policy import (CallBinding, SandboxPaths, SandboxPolicy,
    canonical_local_path)
from app.plugin_scheduler import SchedulerLease
from app.plugin_strict_runtime import PluginStrictRuntime

_MAX_PREVIEWS = 128
_MAX_CALLS = 4096
_PREVIEW_SECONDS = 120
_MCP_MODULE = 'app.plugin_mcp_sandbox_worker'
_MCP_INPUT = 'app/plugin_mcp_sandbox_worker.py'
_WORKER_MODES = {'--plugin-worker', '--package-mcp-server', '--plugin-stage-worker'}
_NO_EXTERNAL_PERMISSIONS = {'python.execute'}


def _refuse(code):
    raise RuntimeFailure(code, '严格 MCP 宿主绑定不可用或已变化；未回退、未重试或隐式授权外部 I/O')


def _copy(value):
    return json.loads(canonical_bytes(value))


def _inside(path, root):
    return path.casefold() == root.casefold() or path.casefold().startswith(root.casefold() + '\\')


@dataclass(frozen=True)
class _Preview:
    manager: object
    manager_nonce: object
    manager_lease: object
    snapshot: dict
    manifest: dict
    package_root: str
    runtime_inventory: object
    root_intent: dict
    spec: McpLaunchSpec
    expires: float


class _NativeTokenCleanup:
    """Retain the actual read-only query token before any information call."""
    def __init__(self, api, token):
        self.api, self.token = api, token
        self.closed = False

    def close(self):
        if not self.closed:
            try:
                self.api.close(self.token)
                self.closed = True
            except BaseException as error:
                error.observation_cleanup = self
                raise


class _PreparationCleanup:
    """Own ALL source/tree/copy/partial native owners, including wrapped failures.

    PreparationOwner itself must remain its exact production type for prepare_tree.
    This enclosing owner survives both successful composition handoff and failures.
    Closing never deletes a root/profile, resets an ACL, adopts an unknown object,
    releases a scheduler lease, invokes a second worker or promotes an unknown result.
    """
    def __init__(self, providers, context=None):
        self.providers = providers
        self.context = context
        self.tree = PreparationOwner()
        self.source_leases = []
        self.adapter = None
        self.ledger = None
        self.io = None
        self.native = None
        self.bundle = None
        self.foreign = []
        self.unknown = []
        self.producer_active = True
        self.closed = False
        self._lock = threading.RLock()

    def retain_source(self, lease):
        self.tree.retain(lease)
        self.source_leases.append(lease)
        return lease

    def absorb(self, failure):
        """Wrappers such as ProvisionError keep native cleanup on their cause."""
        with self._lock:
            pending = [failure]
            seen = set()
            while pending:
                error = pending.pop()
                if error is None or id(error) in seen:
                    continue
                seen.add(id(error))
                found = False
                observation = getattr(error, 'observation_cleanup', None)
                if observation is not None:
                    self.tree.retain(observation)
                    found = True
                for name in ('mcp_preparation_cleanup', 'sandbox_cleanup', 'probe_cleanup'):
                    item = getattr(error, name, None)
                    if item is not None:
                        found = True
                        if item is not self and not any(held is item for held in self.foreign):
                            self.foreign.append(item)
                # An unowned native-close failure must not be silently converted
                # into a clean receipt. Keep the uncertainty for explicit repair.
                code = str(getattr(error, 'code', ''))
                if not code:
                    code = str(error) if type(error) in (ValueError, RuntimeError) else ''
                if (not found and code in ('close_failed', 'native_close_handle')
                        and not any(item is error for item in self.unknown)):
                    self.unknown.append(error)
                pending.extend((getattr(error, '__cause__', None),
                                getattr(error, '__context__', None)))

    @staticmethod
    def _close_foreign(item):
        gate = getattr(item, 'cleanup_gate', None)
        if callable(gate):
            return gate() is True
        thread = getattr(item, 'thread', None)
        if thread is not None:
            if thread.is_alive() or not item.done.is_set():
                return False
            if item.handles and not item.retry_handle_cleanup():
                return False
            return item.reverted is True and not item.handles
        close = getattr(item, 'close', None)
        if not callable(close):
            return False
        if getattr(item, 'closed', None) is not True:
            close()
        return getattr(item, 'closed', None) is True

    def _runtime_safe(self):
        if self.context is None:
            return True
        runtime = self.providers.runtime
        with runtime._lock:
            owners = runtime._owners.get(self.context.identity.call_id)
        if owners is None:
            return True
        # The runtime closes its actual child/Job/pipe/probe owners first. Never
        # race a live worker/prepare stack, even when runtime.close was requested.
        return (owners.get('active') is False and not owners.get('cancel_pending')
            and all(owners.get(name) is None for name in
                    ('running', 'process_guard', 'observer_bundle', 'channel')))

    def cleanup_gate(self):
        with self._lock:
            if self.closed:
                return True
            if self.producer_active or not self._runtime_safe():
                return False
            failed = []
            for item in self.foreign:
                try:
                    if not self._close_foreign(item):
                        failed.append(item)
                except BaseException:
                    failed.append(item)
            self.foreign = failed
            if failed:
                return False
            if self.bundle is not None:
                try:
                    if self.bundle.cleanup_gate() is not True:
                        return False
                except BaseException as error:
                    self.absorb(error)
                    return False
            if self.io is not None:
                try:
                    with self.io._lock:
                        states = tuple(self.io._states.values())
                    for state in states:
                        if not state.closed:
                            self.io.close(state)
                        if (not state.closed or state.owned or state.writer is not None
                                or state.launch_lock_held):
                            return False
                    if self.io._orphaned:
                        self.io.retry_orphan_cleanup()
                    if self.io._orphaned:
                        return False
                except BaseException as error:
                    self.absorb(error)
                    return False
            if self.unknown:
                return False  # Keep every source/path lease while ownership is unknown.
            # This closes sources AS WELL AS prepared.lease and every earlier
            # parent/witness/copy owner; no source lease disappears on success.
            if self.tree.close() is not True or self.tree.leases or self.tree.streams:
                return False
            self.closed = True
            return True


class _HostProviders:
    def __init__(self, ordinary, manager_provider):
        self.ordinary = ordinary
        self.manager_provider = manager_provider
        self.runtime = None
        self.clock = ordinary.clock
        self.settings = (ordinary.approved_parent, ordinary.runtime_source,
                         ordinary.ledger_path, ordinary.system_root)
        self._lock = threading.RLock()
        self._previews = {}
        self._preparations = {}
        self._inspections = []
        self.closed = False

    def _settings(self):
        ordinary = self.ordinary
        if (self.closed or self.runtime is None or self.runtime.closed or ordinary.closed
                or ordinary.host_parent_approved is not True
                or (ordinary.approved_parent, ordinary.runtime_source,
                    ordinary.ledger_path, ordinary.system_root) != self.settings):
            _refuse('mcp_host_configuration_changed')
        system_root = ordinary._require_configured()
        canonical_local_path(system_root)
        return system_root

    def _manager(self):
        from app.plugin_store import PluginManager
        from app.plugin_runtime_selection import StrictRuntimeSelection
        manager = self.manager_provider()
        if (type(manager) is not PluginManager or manager.require_strict_execution is not True
                or manager.runtime is not self.ordinary
                or type(manager._runtime_selection) is not StrictRuntimeSelection
                or manager._runtime_selection.runtime_for('ordinary') is not self.ordinary
                or manager._runtime_selection.runtime_for('mcp') is not self.runtime
                or manager._closing or not manager._initialized or not manager._registry_ready
                or manager._lease is None or manager._authority_nonce is None
                or not callable(getattr(manager, 'strict_mcp_live_spec', None))):
            _refuse('mcp_exact_host_manager_required')
        return manager

    def _start_inspection(self):
        owner = _PreparationCleanup(self)
        with self._lock:
            # Register BEFORE any readonly token/source handle can be acquired.
            # close() must see in-flight inspections as well as failed close owners.
            self._inspections.append(owner)
        return owner

    def _finish_inspection(self, owner):
        owner.producer_active = False
        try:
            clean = owner.cleanup_gate() is True
        except BaseException as error:
            owner.absorb(error)
            clean = False
        with self._lock:
            if clean:
                self._inspections = [item for item in self._inspections if item is not owner]
            elif not any(item is owner for item in self._inspections):
                self._inspections.append(owner)
        return clean

    def _read_runtime(self):
        inventory = read_portable_runtime(self.ordinary.runtime_source)
        metadata, raw = file_record(Path(inventory.root) / RUNTIME_MANIFEST,
                                   limit=MAX_MANIFEST_BYTES, read=True)
        document = parse_json(raw)
        modes = document.get('worker_modes')
        if (metadata != {'sha256': inventory.manifest_entry.sha256,
                         'size': inventory.manifest_entry.size}
                or type(modes) is not list or any(type(item) is not str for item in modes)
                or modes != sorted(set(modes)) or not set(modes) <= _WORKER_MODES
                or not {'--package-mcp-server', '--plugin-worker'} <= set(modes)
                or _MCP_MODULE not in document.get('analyzed_modules', [])
                or _MCP_INPUT not in document.get('source_inputs', {})):
            _refuse('mcp_portable_worker_mode_not_inventoried')
        return inventory

    def _check_source_separation(self, package_root, inventory):
        parent, _source, ledger, _system = self.settings
        for source in (package_root, inventory.root):
            canonical_local_path(source)
            if _inside(parent, source) or _inside(ledger, source):
                _refuse('mcp_output_overlaps_immutable_source')
        if _inside(package_root, inventory.root) or _inside(inventory.root, package_root):
            _refuse('mcp_runtime_and_package_sources_overlap')

    @staticmethod
    def _operation(snapshot, manifest):
        from app.package_mcp import ADAPTER, _tools
        from app.plugin_store import _contains_credential_key
        declaration = snapshot.get('declaration')
        operation = snapshot.get('mcp_operation')
        if (snapshot.get('action') != 'call' or snapshot.get('adapter') != ADAPTER
                or operation not in ('discover', 'execute') or type(declaration) is not dict
                or set(declaration) != {'name', 'transport', 'entrypoint'}
                or declaration.get('transport') != 'stdio'
                or declaration.get('name') != snapshot.get('mcp_name')
                or not declaration.get('entrypoint', '').endswith('.py')
                or sum(item == declaration for item in manifest.get('mcp', [])) != 1
                or manifest.get('python_dependencies')
                or set(manifest.get('permissions', [])) - _NO_EXTERNAL_PERMISSIONS):
            _refuse('mcp_selfcontained_stdio_required')
        expected = ['mcp.connect', 'mcp.discover'] if operation == 'discover' else ['mcp.connect', 'mcp.call']
        if snapshot.get('permissions') != expected:
            _refuse('mcp_operation_permissions_changed')
        if operation == 'discover':
            if any(key in snapshot for key in ('tool', 'tool_schema', 'tool_definition', 'args')):
                _refuse('mcp_discovery_operation_changed')
        else:
            definition = snapshot.get('tool_definition')
            values = _tools([definition])
            args = snapshot.get('args')
            if (values != [definition] or definition.get('name') != snapshot.get('tool')
                    or definition.get('inputSchema') != snapshot.get('tool_schema')
                    or type(args) is not dict or _contains_credential_key(args)
                    or snapshot.get('targets') != [snapshot.get('target')]
                    or snapshot.get('arguments_are_exact') is not True):
                _refuse('mcp_exact_tool_definition_required')
            import jsonschema
            jsonschema.validate(args, values[0]['inputSchema'])

    def _metadata(self, manager, snapshot):
        """Use a bounded READ-ONLY reader, never a nested manager writer.

        PackageMcp calls preview while its manager._db writer is already open.
        Those preview paths have made no package changes; mode=ro sees the same
        committed current row. The held manager lock prevents host-side mutation.
        This connection issues SELECTs only, with SQLite mode=ro/query_only; it
        never creates a database, parent directory or manager approval.
        """
        from app.package_mcp import PackageMcp
        from app.plugin_manifest import validate_manifest
        with manager._lock:
            if self._manager() is not manager:
                _refuse('mcp_host_manager_changed')
            checked_local(manager.db_path)
            connection = sqlite3.connect(Path(manager.db_path).as_uri() + '?mode=ro',
                                         uri=True, timeout=5)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                PackageMcp(manager)._fresh(connection, snapshot, allow_loaded=True)
                row = manager._row(connection, snapshot['id'], installed=True)
                manifest = parse_json(row['manifest'].encode('utf-8'))
                validate_manifest(manifest, file_names=set(manifest.get('files', {})))
                package_root = str(manager.root / 'code' / row['id'] / row['digest'])
                canonical_local_path(package_root)
                checked_local(package_root, directory=True)
                if (snapshot.get('package_root') != package_root
                        or row['revision'] != snapshot.get('expected_revision')
                        or row['digest'] != snapshot.get('digest')
                        or digest(manifest) != row['digest']
                        or manifest.get('id') != row['id']):
                    _refuse('mcp_installed_source_metadata_changed')
                self._operation(snapshot, manifest)
                entrypoint = snapshot['declaration']['entrypoint']
                expected_target = {'kind': 'local_process',
                    'entrypoint': package_root + '\\' + entrypoint.replace('/', '\\'),
                    'sha256': manifest['files'][entrypoint], 'cwd': package_root,
                    'interpreter': 'dedicated_frozen_single_server',
                    'inherits_user_environment': False}
                if snapshot.get('target') != expected_target:
                    _refuse('mcp_installed_target_changed')
                code = read_code_inventory(package_root, manifest, row['digest'])
                return _copy(manifest), package_root, code
            finally:
                connection.close()

    @staticmethod
    def _medium(observer, owner):
        # WindowsObserver.current_integrity intentionally returns a refusal value
        # on errors. Here retain its real native token explicitly so a failed
        # CloseHandle cannot be swallowed and lose the unknown token owner.
        import ctypes as C
        from app.plugin_windows_observer import SID_AND_ATTRIBUTES, IntegrityEvidence
        api = observer.api
        token = api._token(api.k.GetCurrentProcess())
        token_owner = _NativeTokenCleanup(api, token)
        owner.foreign.append(token_owner)
        try:
            buffer = api._info(token, 25)
            label = C.cast(buffer, C.POINTER(SID_AND_ATTRIBUTES)).contents
            sid = api._sid(label.sid)
            if sid != 'S-1-16-8192':
                _refuse('mcp_medium_host_required')
            return IntegrityEvidence(True, 8192, sid, 'medium_verified', 'host_os_query')
        finally:
            token_owner.close()

    def execution_evidence(self, mode):
        if mode != 'mcp':
            return None
        owner = self._start_inspection()
        value = None
        try:
            self._settings()
            self._manager()
            with self._lock:
                if any(not item.producer_active and not item.closed
                       for item in self._inspections if item is not owner):
                    return None
            from app.plugin_windows_launcher import NativeWindowsApi
            from app.plugin_windows_observer import WindowsObserver
            from app.plugin_native_channel import NativePipeApi
            from app.plugin_sandbox_acl import NativeAclApi
            from app.plugin_token_access import NativeTokenAccessApi
            from app.plugin_native_provision import NativeProfileFolderApi
            # These exact production constructors bind real SDK entrypoints, not
            # injected ordinary evidence with only its mode/limits rewritten.
            native = NativeWindowsApi()
            pipes = NativePipeApi()
            NativeAclApi()
            NativeTokenAccessApi()
            NativeProfileFolderApi()
            integrity = self._medium(WindowsObserver(self.ordinary.approved_parent,
                observation_approved=self.ordinary.host_parent_approved), owner)
            sid = native.derive_sid('Lingxi.Plugin.McpAvailabilityReadOnly')
            if not re.fullmatch(r'S-1-15-2-(?:[0-9]+-)*[0-9]+', sid):
                _refuse('mcp_readonly_sid_derivation_failed')
            inventory = self._read_runtime()
            lease = owner.retain_source(observe_source(inventory.root,
                (*inventory.entries, inventory.manifest_entry)))
            lease.revalidate()
            if self._read_runtime() != inventory:
                _refuse('mcp_runtime_source_changed')
            self._settings()
            self._manager()
            policy = SandboxPolicy.stdio_mcp()
            adapter_ready = all(callable(getattr(native, name, None)) for name in
                ('CreateProfile', 'DeriveProfile', 'CreateProcess', 'InitializeAttributes',
                 'UpdateAttribute', 'SetJob', 'QueryJob', 'AssignJob', 'InJob', 'Resume'))
            adapter_ready = adapter_ready and all(callable(getattr(pipes.k, name, None))
                for name in ('CreatePipe', 'DuplicateHandle', 'GetHandleInformation',
                             'CancelSynchronousIo', 'ReadFile', 'WriteFile'))
            if not adapter_ready:
                _refuse('mcp_native_adapter_not_ready')
            value = {'source': integrity.source, 'verified': integrity.verified,
                'mode': 'mcp', 'runtime_inventory_sha256': inventory.inventory_sha256,
                'wall_seconds': policy.wall_seconds, 'memory_limit_bytes': policy.memory_bytes,
                'cpu_rate': policy.cpu_rate // 100, 'active_process_limit': policy.active_processes,
                'adapter_ready': adapter_ready}
        except Exception as error:
            owner.absorb(error)
            value = None
        finally:
            if not self._finish_inspection(owner):
                value = None
        return value

    def preview(self, snapshot):
        owner = self._start_inspection()
        try:
            system_root = self._settings()
            manager = self._manager()
            with self._lock:
                if any(not item.producer_active and not item.closed
                       for item in self._inspections if item is not owner):
                    _refuse('mcp_readonly_inspection_cleanup_pending')
            manifest, package_root, code = self._metadata(manager, snapshot)
            runtime = self._read_runtime()
            self._check_source_separation(package_root, runtime)
            if sum(item.size for item in (*runtime.entries, *code)) > 256 * 1024**2:
                _refuse('mcp_prepared_payload_limit')
            from app.plugin_windows_launcher import NativeWindowsApi
            from app.plugin_windows_observer import WindowsObserver
            from app.plugin_native_provision import root_creation_plan
            from app.plugin_sandbox_provision import _child_access_paths
            from app.plugin_token_access import MAX_TARGETS
            self._medium(WindowsObserver(self.ordinary.approved_parent,
                observation_approved=self.ordinary.host_parent_approved), owner)
            call_id, run_id, nonce = secrets.token_hex(16), secrets.token_hex(16), secrets.token_hex(32)
            profile = 'Lingxi.Plugin.Mcp.' + run_id
            sid = NativeWindowsApi().derive_sid(profile)  # Derive, NEVER create/query/adopt a profile.
            root = self.ordinary.approved_parent + '\\sandbox-lab-' + run_id
            binding = CallBinding(snapshot['id'], snapshot['expected_revision'], snapshot['digest'], call_id)
            paths = SandboxPaths(root, root + '\\runtime',
                root + '\\code\\' + binding.plugin_id + '\\' + binding.digest,
                root + '\\scratch\\' + call_id)
            environment = {'SYSTEMROOT': system_root, 'WINDIR': system_root,
                'TEMP': paths.scratch, 'TMP': paths.scratch, 'PYTHONIOENCODING': 'utf-8',
                'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1'}
            spec = make_mcp_launch_spec(snapshot=snapshot, binding=binding, paths=paths,
                profile_name=profile, appcontainer_sid=sid, nonce=nonce,
                code_files=code, runtime_files=runtime.entries, manifest=manifest,
                executable=paths.runtime + '\\' + WORKER_EXE, environment=environment)
            if len(_child_access_paths(spec.policy_plan)) > MAX_TARGETS:
                _refuse('mcp_child_access_target_limit')
            root_intent = root_creation_plan(approved_parent=self.ordinary.approved_parent,
                                            root=root, creation_nonce=nonce)
            # No PathObservation is made up for the future root/runtime/scratch.
            # Complete actual inventories and exact names above are semantic intent.
            record = _Preview(manager, manager._authority_nonce, manager._lease,
                              _copy(snapshot), manifest, package_root, runtime,
                              root_intent, spec, self.clock() + _PREVIEW_SECONDS)
            with self._lock:
                now = self.clock()
                self._previews = {key: item for key, item in self._previews.items()
                    if (key not in self._preparations and item.expires > now)
                    or (key in self._preparations and not self._preparations[key].closed)}
                pending = sum(key not in self._preparations for key in self._previews)
                if self.closed or pending >= _MAX_PREVIEWS or call_id in self._previews:
                    _refuse('mcp_host_preview_capacity_or_reuse')
                self._previews[call_id] = record
            return spec
        except BaseException as error:
            owner.absorb(error)
            raise
        finally:
            if not self._finish_inspection(owner):
                _refuse('mcp_preview_inspection_cleanup_pending')

    def _record(self, identity, public):
        if type(identity) is not ExecutionIdentity or type(public) is not dict:
            _refuse('mcp_exact_live_identity_required')
        with self._lock:
            record = self._previews.get(identity.call_id)
        if (record is None or record.spec.snapshot() != public
                or record.spec.policy_plan.binding != CallBinding(identity.plugin_id,
                    identity.revision, identity.digest, identity.call_id)
                or digest(record.snapshot.get('trust')) != identity.trust_fingerprint):
            _refuse('mcp_host_preview_binding_changed')
        return record

    def _reservation(self, manager, record, identity, context=None):
        with manager._lock:
            current = manager._manager_execution_bindings.get(identity.call_id)
            snapshot = {**record.snapshot, 'strict_mcp_spec': record.spec.snapshot()}
            if (manager._closing or current is None or current['identity'] != identity
                    or manager._authority_nonce != record.manager_nonce
                    or manager._lease is not record.manager_lease
                    or current['runtime'] is not self.runtime or current['snapshot'] != snapshot
                    or current['mode'] != ('mcp_discover' if snapshot['mcp_operation'] == 'discover'
                                           else 'mcp_execute')
                    or identity.call_id not in manager._manager_call_ids
                    or not any(item is current for item in manager._execution_reservations.values())):
                _refuse('mcp_manager_reservation_changed')
            coordinator = manager.execution_coordinator
            with coordinator._lock:
                call = coordinator._calls.get(identity.call_id)
                if (call is None or call.identity != identity or call.finalized
                        or call.cancel_event.is_set() or call.lease is None):
                    _refuse('mcp_manager_call_cancelled_or_changed')
                if context is not None and (call.cancel_event is not context.cancel_event
                        or call.lease is not context.admission_lease
                        or current['prepared'] is not context.prepared
                        or not call.started or not call.running):
                    _refuse('mcp_actual_execution_owner_required')
            return snapshot

    def live(self, identity, public):
        try:
            system_root = self._settings()
            manager = self._manager()
            record = self._record(identity, public)
            if (record.manager is not manager
                    or dict(record.spec.environment).get('SYSTEMROOT') != system_root):
                return None
            self._reservation(manager, record, identity)
            self._check_source_separation(record.package_root, record.runtime_inventory)
            if manager.strict_mcp_live_spec(identity, _copy(public)) != public:
                return None
            if (read_code_inventory(record.package_root, record.manifest, identity.digest)
                    != record.spec.policy_plan.code_files or self._read_runtime() != record.runtime_inventory):
                return None
            with self._lock:
                owner = self._preparations.get(identity.call_id)
            if owner is not None:
                if owner.context.cancel_event.is_set():
                    return None
                for lease in owner.source_leases:
                    if lease.closed:
                        return None
                    lease.revalidate()
            self._reservation(manager, record, identity)
            # The manager repeats _fresh under its reservation lock after source IO;
            # this API never creates another preview/call id or grants anything.
            result = manager.strict_mcp_live_spec(identity, _copy(public))
            return _copy(public) if result == public else None
        except Exception as error:
            owner = self._start_inspection()
            owner.absorb(error)
            self._finish_inspection(owner)
            return None

    def _authority(self, context, spec):
        if type(context) is not ExecutionContext or type(spec) is not McpLaunchSpec:
            _refuse('mcp_exact_preparation_context_required')
        identity, prepared = context.identity, context.prepared
        if type(prepared) is not dict or context.cancel_event.is_set():
            _refuse('mcp_cancelled_before_preparation')
        record = self._record(identity, spec.snapshot())
        if record.spec is not spec or record.manager is not self._manager():
            _refuse('mcp_same_exact_preview_required')
        capability = prepared.get('strict_mcp_approval')
        snapshot = self._reservation(record.manager, record, identity, context)
        if (type(capability) is not McpManagerApproval or capability._owner is not self.runtime
                or capability.identity != identity or capability.spec_fingerprint != spec.snapshot()['fingerprint']
                or capability.approval_fingerprint != digest(snapshot)
                or prepared.get('authorizing_snapshot') != snapshot
                or prepared.get('approval_snapshot') != snapshot
                or prepared.get('authorizing_fingerprint') != capability.approval_fingerprint
                or os.fspath(prepared.get('package_root', '')) != record.package_root
                or identity.call_id not in self.runtime._attempted):
            _refuse('mcp_consumed_manager_approval_required')
        with self.runtime._lock:
            native_owner = self.runtime._owners.get(identity.call_id)
            if (self.runtime._approvals.get(capability._token) is not None
                    or identity.call_id in self.runtime._specs
                    or native_owner is None or native_owner['identity'] != identity
                    or native_owner['active'] is not True
                    or native_owner['preparation_pending'] is not True
                    or native_owner['cancel_event'] is not context.cancel_event):
                _refuse('mcp_manager_approval_not_consumed_by_runtime')
        lease = context.admission_lease
        if (type(lease) is not SchedulerLease or lease.call_id != identity.call_id
                or lease.plugin_id != identity.plugin_id
                or getattr(lease, 'cancel_event', None) is not context.cancel_event
                or lease._scheduler is not record.manager.execution_coordinator.scheduler):
            _refuse('mcp_same_actual_admission_required')
        with lease._scheduler._condition:
            if (lease._scheduler.max_concurrent != 10
                    or lease._scheduler._running.get(identity.call_id) is not lease
                    or lease._released or lease._quarantined):
                _refuse('mcp_actual_admission_not_owned')
        if self.live(identity, spec.snapshot()) != spec.snapshot():
            _refuse('mcp_live_approval_changed')
        return record, True

    def prepare(self, context, spec):
        owner = _PreparationCleanup(self, context)
        try:
            # Register before the first native open/creation and retain it even when
            # authority checking fails. Runtime preparation_pending has an owner.
            call_id = getattr(getattr(context, 'identity', None), 'call_id', None)
            with self._lock:
                if (call_id is None or call_id in self._preparations
                        or len(self._preparations) >= _MAX_CALLS):
                    _refuse('mcp_preparation_owner_reused_or_capacity')
                self._preparations[call_id] = owner
            self._settings()
            record, authorized = self._authority(context, spec)
            runtime = record.runtime_inventory
            self._check_source_separation(record.package_root, runtime)
            owner.retain_source(observe_source(runtime.root, (*runtime.entries, runtime.manifest_entry)))
            owner.retain_source(observe_source(record.package_root, spec.policy_plan.code_files))
            if self.live(context.identity, spec.snapshot()) != spec.snapshot():
                _refuse('mcp_sources_changed_before_preparation')
            prepared, observer = prepare_tree(spec.policy_plan,
                approved_parent=self.ordinary.approved_parent, creation_nonce=spec.nonce,
                root_intent=record.root_intent, runtime_source=runtime.root,
                package_root=record.package_root, owner=owner.tree,
                authorized=authorized, cancel_event=context.cancel_event)
            network = observer.network_isolation(spec.policy_plan.appcontainer_sid)
            if (network.source != 'host_os_query' or network.verified is not True
                    or network.sid != spec.policy_plan.appcontainer_sid
                    or network.has_loopback_exemption is not False):
                _refuse('mcp_loopback_state_unverified')
            from app.plugin_native_provision import NativeProvisionAdapter
            from app.plugin_sandbox_provision import ProvisionLedger
            from app.plugin_native_channel import NativeBoundedIO
            from app.plugin_echo_native_host import OwnedEchoNativeApi, NativeEchoObserverBundle
            from types import SimpleNamespace
            owner.adapter = NativeProvisionAdapter(prepared, creation_nonce=spec.nonce,
                observation_approved=authorized, profile_folder_observation_approved=authorized,
                defer_child_access=True)
            owner.ledger = ProvisionLedger(self.ordinary.ledger_path,
                approved_parent=self.ordinary.approved_parent, native_adapter=owner.adapter,
                clock=self.clock, defer_child_access=True)
            owner.io = NativeBoundedIO(clock=self.clock)
            owner.native = OwnedEchoNativeApi(access_request_provider=owner.adapter.child_access_request,
                prepared=prepared, network_observer=observer)
            def revalidate_runtime():
                if self.closed or context.cancel_event.is_set():
                    return False
                for held in owner.source_leases:
                    if held.closed:
                        return False
                    held.revalidate()
                return (self._read_runtime() == runtime
                    and self.live(context.identity, spec.snapshot()) == spec.snapshot())
            # A second *observer wrapper*, not another native resource owner. Both
            # wrappers refer to the exact same native probe/loopback owner; this one
            # keeps partial failures reachable even before runtime builds its bundle.
            owner.bundle = NativeEchoObserverBundle(plan=SimpleNamespace(policy_plan=spec.policy_plan),
                prepared=prepared, ledger=owner.ledger, native_api=owner.native,
                channel_io=owner.io, scheduler_lease=context.admission_lease,
                runtime_revalidator=revalidate_runtime, live_intent_provider=lambda _: None,
                network_observer=observer)
            if revalidate_runtime() is not True:
                _refuse('mcp_preparation_binding_changed')
            return McpPreparedComposition(prepared=prepared, provision_adapter=owner.adapter,
                ledger=owner.ledger, native_api=owner.native, network_observer=observer,
                channel_io=owner.io, runtime_revalidator=revalidate_runtime,
                preparation_cleanup=owner)
        except BaseException as error:
            owner.absorb(error)
            error.mcp_preparation_cleanup = owner
            raise
        finally:
            owner.producer_active = False

    def close(self):
        with self._lock:
            self.closed = True
            owners = tuple(self._preparations.values())
            inspections = tuple(self._inspections)
        clean = True
        for owner in owners:
            if owner.context is not None:
                owner.context.cancel_event.set()  # The existing event, never a new scheduler/Job.
            try:
                if owner.cleanup_gate() is not True:
                    clean = False
            except BaseException as error:
                owner.absorb(error)
                clean = False
        failed = []
        for owner in inspections:
            try:
                if owner.cleanup_gate() is not True:
                    failed.append(owner)
            except BaseException as error:
                owner.absorb(error)
                failed.append(owner)
        with self._lock:
            self._inspections = [item for item in self._inspections
                if not any(item is old for old in inspections)] + failed
            self._previews = {key: item for key, item in self._previews.items()
                              if key in self._preparations}
        return clean and not failed


def build_mcp_runtime(ordinary_runtime, manager_provider):
    """Return the concrete host runtime for package_host's StrictRuntimeSelection.

    manager_provider is the lazy host-owned current-manager getter, not an RPC
    factory. Import/construction do not read settings or touch OS/storage. Share
    ordinary_runtime._provision_lock ONLY for ledger preview/authorize/commit; the
    runtime does not hold it through prepare, child execution, pipes or cleanup.
    """
    if type(ordinary_runtime) is not PluginStrictRuntime or not callable(manager_provider):
        raise TypeError('real ordinary host runtime and lazy manager provider required')
    lock = ordinary_runtime._provision_lock
    if not callable(getattr(lock, '__enter__', None)) or not callable(getattr(lock, '__exit__', None)):
        raise TypeError('shared ordinary provision journal lock required')
    providers = _HostProviders(ordinary_runtime, manager_provider)
    runtime = PluginMcpStrictRuntime(preview_provider=providers.preview,
        prepare_provider=providers.prepare, execution_evidence_provider=providers.execution_evidence,
        live_spec_provider=providers.live, provision_lock=lock,
        provider_cleanup=providers.close, clock=ordinary_runtime.clock)
    providers.runtime = runtime
    return runtime
