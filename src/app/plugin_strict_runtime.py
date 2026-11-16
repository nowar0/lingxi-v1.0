"""Host-only ordinary-plugin production bridge over the real strict native owners.

No RPC-selected provider, unrestricted subprocess fallback, MCP mode, automatic human
confirmation, profile adoption or OS test. Import/construction never creates a run
root/profile/ACL/worker. Previews read sources and derive a SID only; prospective
paths are INTENT, never fabricated PathObservation/OS evidence. A manager-consumed
snapshot and its exact authorizing fingerprint are mandatory before preparation.

Integration contract (all fields are trusted host memory, not a worker envelope):
* preview_spec(**package_input) -> complete JSON sandbox_spec, persisted verbatim in
  the manager call approval; validate_spec(spec, **same_input) performs no creation.
* ExecutionContext.admission_lease is the coordinator's actual SchedulerLease.
* context.prepared contains package_root, manifest, request(tool/args/config/state),
  sandbox_spec, authorizing_snapshot, authorizing_fingerprint, dependency_binding,
  trust_fingerprint. authorizing_snapshot is exactly the consumed manager snapshot,
  including sandbox_spec, and fingerprint is SHA256 of its canonical JSON.
* live_binding_provider(identity, authorizing_snapshot) returns exact True only
  after manager-locked complete package/config/state/dependency/trust revalidation.
* finish calls cleanup_receipt(identity). Missing proof retains quarantine. Explicit
  host repair may call retry_cleanup(identity), then coordinator.release_quarantine.

External IO uses explicit PRESTART broker_requests/targets, full descriptors and
host-composed concrete adapters. Permission declarations alone never mint grants.
Missing source/epoch/HTTP/model providers fail closed. Model pending registration
is not final sending; that still requires independent authenticated UI approval.
"""
from __future__ import annotations

from dataclasses import replace
import ntpath
import os
from pathlib import Path
import re
import secrets
import threading
import time

from app.plugin_execution import ExecutionContext, ExecutionIdentity, ResourceCleanupReceipt
from app.plugin_runtime import RuntimeFailure
from app.plugin_scheduler import SchedulerLease
from app.plugin_sandbox_policy import (CallBinding, SandboxPaths, SandboxPolicy, LaunchPlan,
    JobPlan, CREATION_FLAGS, JOB_LIMIT_FLAGS, CPU_RATE_ENABLE, CPU_RATE_HARD_CAP,
    _grants, canonical_local_path, inventory_fingerprint, plan_fingerprint,
    semantic_plan_projection, validate_launch_plan)
from app.plugin_runtime_preparation import (PreparationOwner,
    WORKER_EXE, canonical_bytes, checked_local, digest, observe_source, prepare_tree,
    read_code_inventory, read_portable_runtime)
from app.plugin_native_provision import root_creation_plan
from app.plugin_runtime_brokers import RuntimeBrokerServices, request_contract, EXTERNAL_OPS
from app.plugin_proxy import PERMISSIONS

SPEC_SCHEMA = 'lingxi.ordinary-strict-runtime.v1'
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ALLOWED_PERMISSIONS = {'python.execute', 'config.read', 'state.read'} | set(PERMISSIONS.values())
_MAX_CALLS = 4096


def _refuse(code):
    raise RuntimeFailure(code, '严格普通插件运行时拒绝执行；没有回退、自动重试或隐式外部 I/O 授权')


def _json_copy(value):
    import json
    return json.loads(canonical_bytes(value))


def _ordinary_host():
    # Import lazily so production packaging has no tools dependency or eager Win32 IO.
    from app.plugin_strict_echo import StrictInvocationHost
    return StrictInvocationHost


def _intended_policy(*, plugin_id, revision, digest_sha256, call_id, root, sid,
                     code_files, runtime_files):
    """Pure intended plan. No checker asserts future paths already exist."""
    policy = SandboxPolicy.normal()
    paths = SandboxPaths(root, root + '\\runtime',
        root + '\\code\\' + plugin_id + '\\' + digest_sha256,
        root + '\\scratch\\' + call_id)
    job = JobPlan(JOB_LIMIT_FLAGS, policy.memory_bytes, policy.memory_bytes, 1,
                  CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, 2000, policy.wall_seconds)
    plan = LaunchPlan(policy, CallBinding(plugin_id, revision, digest_sha256, call_id),
        paths, sid, (), (), code_files, runtime_files, inventory_fingerprint(runtime_files),
        _grants(paths, sid), (), CREATION_FLAGS, job, '', ipc_stage='semantic')
    plan = replace(plan, fingerprint=plan_fingerprint(plan))
    validate_launch_plan(plan, require_physical=False)
    if len(plan.acl_grants) != 7:
        _refuse('strict_runtime_exact_seven_grants_required')
    return plan


def broker_integration_requirements():
    """Concrete shipped wiring and remaining composition, never fake readiness."""
    return {
        'configured':False, 'currently_allowed':['config.read', 'state.read', 'cancel'],
        'denied_io':sorted(EXTERNAL_OPS),
        'implemented_adapters':['file.read', 'file.write', 'data.read', 'data.write', 'http.request'],
        'request_contract':'args.broker_requests explicitly declared by input_schema; each {op,target_argument,params} must match manifest tools.targets permission/kind/argument; never infer an operation from a path or URL',
        'composition':'Inject RuntimeBrokerServices(object_scope,http_request,model_request,effect_guard); missing providers fail closed',
        'file_data':'ObjectScope includes exact root/manager owner/data generation and current live callback; write guard persists epoch/dirty before CAS publication; broker verifies new descriptor receipt and invalidates old grants',
        'http':'PreparedHttpRequest pins real host-approved IPs/HttpTarget/body, ExactHttpProxy + PinnedHttpTransport use actual invocation cancellation/deadline and no secrets/redirect/proxy/retry',
        'model':'Only precompiled real materials accepted; real running child may register pending_models; final send remains independent authenticated UI HostModelApprovals integration, not automatic',
        'notice':'No shell/path/socket ACL grants. Empty Echo operations remain unchanged. Native OS acceptance has not been performed.'}


class PluginStrictRuntime:
    """Explicit in-process host composition, never constructed from plugin/RPC JSON.

    approved_parent is selected by first-run safe host setup or separate user parent
    approval. host_parent_approved is NOT inferred from an old one-shot OS approval.
    runtime_source must be the independently inventoried portable plugin-runtime
    directory. ledger_path's existing parent must be outside every run root.
    No path is created at constructor time, including a missing approved parent.
    """
    strict_isolation = True
    timeout = 10
    memory_limit_bytes = 256 * 1024**2
    mode = 'ordinary'
    reason_code = 'sandbox_unavailable'

    def __init__(self, *, approved_parent=None, runtime_source=None, ledger_path=None,
                 host_parent_approved=False, live_binding_provider=None, system_root=None,
                 rpc_configured=False, clock=time.monotonic, broker_services=None):
        if type(host_parent_approved) is not bool or type(rpc_configured) is not bool or not callable(clock):
            raise TypeError('explicit host approval, RPC configuration and clock required')
        for value in (approved_parent, runtime_source, ledger_path, system_root):
            if value is not None:
                canonical_local_path(os.fspath(value))
        if live_binding_provider is not None and not callable(live_binding_provider):
            raise TypeError('trusted live binding provider required')
        if runtime_source is not None and ntpath.basename(os.fspath(runtime_source)) != 'plugin-runtime':
            raise ValueError('dedicated portable plugin-runtime directory required')
        if approved_parent is not None and ledger_path is not None:
            parent, ledger = os.fspath(approved_parent), os.fspath(ledger_path)
            if ledger.casefold() == parent.casefold() or ledger.casefold().startswith(parent.casefold() + '\\'):
                raise ValueError('provision ledger must stay outside the approved run parent')
        self.approved_parent = os.fspath(approved_parent) if approved_parent is not None else None
        self.runtime_source = os.fspath(runtime_source) if runtime_source is not None else None
        self.ledger_path = os.fspath(ledger_path) if ledger_path is not None else None
        self.host_parent_approved = host_parent_approved
        self.rpc_configured = rpc_configured
        if broker_services is not None and type(broker_services) is not RuntimeBrokerServices:
            raise TypeError('explicit host RuntimeBrokerServices required')
        self.broker_services = broker_services
        self.live_binding_provider = live_binding_provider
        self._provision_lock = threading.RLock()
        self.system_root = os.fspath(system_root) if system_root is not None else None
        self.clock = clock
        self.closed = False
        self._lock = threading.RLock()
        self._calls = {}
        self._used_specs = set()
        self._inspection_owners = []

    @property
    def available(self):
        return (not self.closed and self.host_parent_approved is True
                and all((self.approved_parent, self.runtime_source, self.ledger_path))
                and callable(self.live_binding_provider))

    def configuration_status(self):
        with self._lock:
            inspection_pending = len(self._inspection_owners)
        return {'configured': bool(self.available), 'mode': 'ordinary',
                'readonly_inspection_cleanup_pending': inspection_pending,
                'call_registry_limit': _MAX_CALLS, 'provision_journal_record_limit': 64,
                'provision_journal_automatic_rotation': False,
                'host_approved_parent': self.approved_parent,
                'host_parent_approved': self.host_parent_approved,
                'runtime_source': self.runtime_source, 'ledger_path': self.ledger_path,
                'rpc_configured': self.rpc_configured, 'mcp_configured': False,
                'external_io_brokers_configured': self.broker_services is not None
                    and any(value for key, value in self.broker_services.status().items()
                            if key not in ('model_final_send_integrated', 'request_contract_approved')),
                'broker_capabilities':self.broker_services.status() if self.broker_services is not None else {},
                'model_final_send_integrated':False,
                'os_isolation_acceptance': 'not_performed_by_configuration_or_status',
                'notice': '父目录许可来自宿主首次安全设置或单独用户批准；不沿用某次实验单例批准。'}

    def _require_configured(self):
        if not self.available:
            _refuse('sandbox_unavailable')
        if os.name != 'nt':
            _refuse('strict_runtime_windows_required')
        checked_local(self.approved_parent, directory=True)
        checked_local(ntpath.dirname(self.ledger_path), directory=True)
        if Path(self.ledger_path).exists():
            checked_local(self.ledger_path)
        return self.system_root or os.environ.get('SystemRoot') or os.environ.get('SYSTEMROOT')

    def _retain_inspection_error(self, error):
        owner = getattr(error, 'observation_cleanup', None)
        if owner is not None:
            with self._lock:
                self._inspection_owners.append(owner)

    def execution_evidence(self, mode):
        """Readonly *availability* evidence, never an OS-isolation acceptance claim.

        Observes actual Medium token, source inventory and bound Win32 adapter entry
        points. No process/profile/ACL/Job/pipe is created to manufacture readiness.
        Unknown inspection cleanup blocks availability and remains host-owned.
        """
        if mode != 'ordinary':
            return None
        try:
            system_root = self._require_configured()
            canonical_local_path(system_root)
            with self._lock:
                if self._inspection_owners:
                    return None
            from app.plugin_windows_launcher import NativeWindowsApi
            from app.plugin_windows_observer import WindowsObserver
            from app.plugin_native_channel import NativePipeApi
            from app.plugin_sandbox_acl import NativeAclApi
            from app.plugin_token_access import NativeTokenAccessApi
            from app.plugin_native_provision import NativeProfileFolderApi
            # Concrete production adapters only. Their constructors bind SDK entry
            # points; no injected evidence, Fake adapter or worker is accepted here.
            native = NativeWindowsApi()
            NativePipeApi()
            NativeAclApi()
            NativeTokenAccessApi()
            NativeProfileFolderApi()
            _ordinary_host()  # The generic normal-tool host must actually be shipped.
            observer = WindowsObserver(self.approved_parent, observation_approved=self.host_parent_approved)
            integrity = observer.current_integrity()
            if integrity.source != 'host_os_query' or not integrity.verified or integrity.rid != 8192:
                pending=getattr(observer.api,'_integrity_cleanup',None)
                if pending is not None and not pending.closed:
                    self._inspection_owners.append(pending)
                return None
            # Real readonly SDK SID derivation, no CreateAppContainerProfile.
            sid = native.derive_sid('Lingxi.Plugin.AvailabilityReadOnly')
            if not re.fullmatch(r'S-1-15-2-(?:[0-9]+-)*[0-9]+', sid):
                return None
            inventory = read_portable_runtime(self.runtime_source)
            lease = observe_source(inventory.root, (*inventory.entries, inventory.manifest_entry))
            try:
                lease.revalidate()
            finally:
                try:
                    lease.close()
                except BaseException as error:
                    error.observation_cleanup = lease
                    raise
            return {'source': 'host_os_query', 'verified': True, 'mode': 'ordinary',
                    'runtime_inventory_sha256': inventory.inventory_sha256,
                    'wall_seconds': 10, 'memory_limit_bytes': self.memory_limit_bytes,
                    'cpu_rate': 20, 'active_process_limit': 1, 'adapter_ready': True}
        except Exception as error:
            self._retain_inspection_error(error)
            return None

    def stage_available(self):
        from app.plugin_stage_native_runtime import stage_available
        return stage_available(self)

    def preview_stage_spec(self,*,host_snapshot):
        from app.plugin_stage_native_runtime import preview_stage_spec
        return preview_stage_spec(self,host_snapshot=host_snapshot)

    def validate_stage_spec(self,spec,*,request):
        from app.plugin_stage_native_runtime import validate_stage_spec
        return validate_stage_spec(self,spec,request=request)

    def open_stage(self,request,*,proxy,cancelled):
        from app.plugin_stage_native_runtime import open_stage
        return open_stage(self,request,proxy=proxy,cancelled=cancelled)

    def dependency_evidence(self, _requirement):
        # The independently shipped minimal runtime deliberately has no third-party
        # distributions; host importlib.metadata must never stand in for worker IO.
        return None

    @staticmethod
    def _validate_tool(manifest, tool, args):
        if type(manifest) is not dict or manifest.get('entrypoint', {}).get('type') != 'python':
            _refuse('strict_runtime_python_entrypoint_required')
        declared = [item for item in manifest.get('tools', [])
                    if type(item) is dict and item.get('name') == tool]
        if len(declared) != 1:
            _refuse('strict_runtime_tool_not_declared')
        permissions = set(manifest.get('permissions', [])) | set(declared[0].get('permissions', []))
        if permissions - _ALLOWED_PERMISSIONS:
            _refuse('broker_permission_not_integrated')
        request_contract(manifest, tool, args)
        if manifest.get('python_dependencies'):
            _refuse('strict_runtime_dependency_not_bundled')

    def _build(self, *, package_root, manifest, plugin_id, revision, digest_sha256,
               tool, args, config, state, trust_fingerprint, dependency_binding,
               call_id, run_id, creation_nonce):
        system_root = self._require_configured()
        canonical_local_path(system_root)
        if (not _SHA.fullmatch(trust_fingerprint) or type(dependency_binding) is not list
                or not re.fullmatch(r'[0-9a-f]{32}', call_id)
                or not re.fullmatch(r'[0-9a-f]{32}', run_id)
                or not re.fullmatch(r'[0-9a-f]{64}', creation_nonce)
                or any(type(value) is not dict for value in (args, config, state))):
            _refuse('strict_runtime_exact_package_input_required')
        self._validate_tool(manifest, tool, args)
        requests, _targets = request_contract(manifest, tool, args)
        if digest(manifest) != digest_sha256 or manifest.get('id') != plugin_id:
            _refuse('strict_runtime_manifest_digest_changed')
        broker_plan = None
        if requests:
            if self.broker_services is None:
                _refuse('broker_services_not_integrated')
            broker_plan = self.broker_services.prepare(manifest=manifest, tool=tool, args=args,
                config=config, state=state, plugin_id=plugin_id, revision=revision, digest=digest_sha256,
                trust_fingerprint=trust_fingerprint, dependency_binding=dependency_binding, call_id=call_id)
        runtime = read_portable_runtime(self.runtime_source)
        package_source = os.fspath(package_root)
        for source in (runtime.root, package_source):
            if (self.approved_parent.casefold() == source.casefold()
                    or self.approved_parent.casefold().startswith(source.casefold() + '\\')
                    or self.ledger_path.casefold() == source.casefold()
                    or self.ledger_path.casefold().startswith(source.casefold() + '\\')):
                _refuse('strict_runtime_output_overlaps_immutable_source')
        code = read_code_inventory(package_source, manifest, digest_sha256)
        if sum(item.size for item in (*runtime.entries, *code)) > 256 * 1024**2:
            _refuse('strict_runtime_prepared_payload_limit')
        from app.plugin_windows_launcher import NativeWindowsApi
        from app.plugin_windows_observer import WindowsObserver
        observer = WindowsObserver(self.approved_parent, observation_approved=self.host_parent_approved)
        if observer.current_integrity().verified is not True:
            pending=getattr(observer.api,'_integrity_cleanup',None)
            if pending is not None and not pending.closed:
                self._inspection_owners.append(pending)
            _refuse('strict_runtime_medium_required')
        profile_name = 'Lingxi.Plugin.' + run_id
        sid = NativeWindowsApi().derive_sid(profile_name)
        root = self.approved_parent + '\\sandbox-lab-' + run_id
        plan = _intended_policy(plugin_id=plugin_id, revision=revision,
            digest_sha256=digest_sha256, call_id=call_id, root=root, sid=sid,
            code_files=code, runtime_files=runtime.entries)
        from app.plugin_sandbox_provision import _child_access_paths
        from app.plugin_token_access import MAX_TARGETS
        if len(_child_access_paths(plan)) > MAX_TARGETS:
            _refuse('strict_runtime_child_access_target_limit')
        environment = {'SYSTEMROOT': system_root, 'WINDIR': system_root,
            'TEMP': plan.paths.scratch, 'TMP': plan.paths.scratch,
            'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1'}
        host_type = _ordinary_host()
        invocation = host_type.make_plan(plan, profile_name=profile_name, manifest=manifest,
            tool=tool, args=args, config=config, state=state,
            trust_fingerprint=trust_fingerprint, dependency_fingerprint=digest(dependency_binding),
            executable=plan.paths.runtime + '\\' + WORKER_EXE, environment=environment,
            **({} if broker_plan is None else broker_plan.intent_kwargs()))
        root_intent = root_creation_plan(approved_parent=self.approved_parent,
            root=root, creation_nonce=creation_nonce)
        body = {'schema': SPEC_SCHEMA, 'mode': 'ordinary', 'call_id': call_id,
            'run_id': run_id, 'creation_nonce': creation_nonce,
            'host_approved_parent': self.approved_parent,
            'runtime_source': self.runtime_source, 'package_source': os.fspath(package_root),
            'ledger_path': self.ledger_path, 'profile_name': profile_name,
            'appcontainer_sid': sid, 'root_creation': root_intent,
            'policy': _json_copy(semantic_plan_projection(plan)),
            'policy_fingerprint': plan.fingerprint,
            'runtime_manifest_sha256': runtime.manifest_sha256,
            'runtime_inventory_sha256': runtime.inventory_sha256,
            'code_inventory_sha256': inventory_fingerprint(code),
            'executable': invocation.executable, 'environment': dict(invocation.environment),
            'intent': invocation.intent.projection(), 'invocation_fingerprint': invocation.fingerprint,
            'trust_fingerprint': trust_fingerprint, 'dependency_binding': _json_copy(dependency_binding),
            'dependency_fingerprint': digest(dependency_binding),
            'source_manifest_copied_to_runtime': False,
            'profile_folder_readonly_observation_required': True,
            'child_access_mode': 'deferred_owned_suspended_child',
            'parent_acl_modified': False, 'fallback_allowed': False,
            'external_io_brokers_configured':broker_plan is not None,
            'broker_scope':None if broker_plan is None else broker_plan.projection(),
            'broker_operations':[] if broker_plan is None else [item.op for item in broker_plan.operations],
            'denied_io':sorted(EXTERNAL_OPS - set(item.op for item in invocation.intent.operations)),
            'model_final_send_integrated':False,
            'process_started': False, 'native_mutations_performed': False,
            'os_isolation_acceptance': 'not_performed'}
        spec = {**body, 'fingerprint': digest(body)}
        return spec, plan, invocation, runtime

    def preview_spec(self, *, package_root, manifest, plugin_id, revision, digest,
                     tool, args, config, state, trust_fingerprint, dependency_binding):
        """Readonly full preview; random names are minted HERE, never in execute."""
        try:
            spec, _plan, _invocation, _runtime = self._build(package_root=package_root,
                manifest=manifest, plugin_id=plugin_id, revision=revision, digest_sha256=digest,
                tool=tool, args=args, config=config, state=state,
                trust_fingerprint=trust_fingerprint, dependency_binding=dependency_binding,
                call_id=secrets.token_hex(16), run_id=secrets.token_hex(16),
                creation_nonce=secrets.token_hex(32))
            return _json_copy(spec)
        except RuntimeFailure:
            raise
        except Exception as error:
            self._retain_inspection_error(error)
            _refuse(getattr(error, 'code', 'strict_runtime_preview_unavailable'))

    def _validated(self, spec, **package_input):
        if (type(spec) is not dict or spec.get('schema') != SPEC_SCHEMA
                or spec.get('mode') != 'ordinary' or type(spec.get('fingerprint')) is not str
                or digest({key: value for key, value in spec.items() if key != 'fingerprint'}) != spec['fingerprint']):
            _refuse('strict_runtime_spec_changed')
        package_input = dict(package_input)
        package_input['digest_sha256'] = package_input.pop('digest')
        fresh, plan, invocation, runtime = self._build(**package_input,
            call_id=spec.get('call_id'), run_id=spec.get('run_id'), creation_nonce=spec.get('creation_nonce'))
        if fresh != spec:
            _refuse('strict_runtime_spec_changed')
        return plan, invocation, runtime

    def validate_spec(self, spec, **package_input):
        try:
            self._validated(spec, **package_input)
            return True
        except RuntimeFailure:
            raise
        except Exception as error:
            self._retain_inspection_error(error)
            _refuse(getattr(error, 'code', 'strict_runtime_spec_unavailable'))

    def _live(self, identity, snapshot):
        return (not self.closed and self.host_parent_approved is True
                and snapshot.get('sandbox_spec', {}).get('host_approved_parent') == self.approved_parent
                and snapshot.get('sandbox_spec', {}).get('runtime_source') == self.runtime_source
                and snapshot.get('sandbox_spec', {}).get('ledger_path') == self.ledger_path
                and callable(self.live_binding_provider)
                and self.live_binding_provider(identity, _json_copy(snapshot)) is True)

    def _context_authority(self, context):
        if type(context) is not ExecutionContext or type(context.identity) is not ExecutionIdentity:
            _refuse('strict_runtime_execution_context_required')
        prepared = context.prepared
        if type(prepared) is not dict:
            _refuse('strict_runtime_prepared_request_required')
        required = {'package_root', 'manifest', 'request', 'sandbox_spec',
                    'authorizing_snapshot', 'authorizing_fingerprint',
                    'dependency_binding', 'trust_fingerprint'}
        if not required <= set(prepared):
            _refuse('strict_runtime_consumed_snapshot_required')
        snapshot = prepared['authorizing_snapshot']
        spec = prepared['sandbox_spec']
        identity = context.identity
        request = prepared['request']
        lease = getattr(context, 'admission_lease', None)
        if (type(snapshot) is not dict or type(spec) is not dict or type(request) is not dict
                or type(lease) is not SchedulerLease
                or lease.call_id != identity.call_id or lease.plugin_id != identity.plugin_id
                or getattr(lease, 'cancel_event', None) is not context.cancel_event
                or not callable(getattr(context.cancel_event, 'is_set', None))
                or not callable(getattr(context.cancel_event, 'set', None))):
            _refuse('strict_runtime_shared_admission_context_required')
        scheduler = lease._scheduler
        with scheduler._condition:
            if (scheduler._running.get(identity.call_id) is not lease or lease._released
                    or scheduler.max_concurrent > 10):
                _refuse('strict_runtime_shared_admission_not_owned')
        if (prepared['authorizing_fingerprint'] != digest(snapshot)
                or snapshot.get('sandbox_spec') != spec
                or snapshot.get('action') != 'call' or 'adapter' in snapshot or 'mcp_operation' in snapshot
                or snapshot.get('id') != identity.plugin_id
                or snapshot.get('expected_revision') != identity.revision
                or snapshot.get('digest') != identity.digest
                or spec.get('call_id') != identity.call_id
                or prepared['trust_fingerprint'] != identity.trust_fingerprint
                or digest(snapshot.get('trust')) != identity.trust_fingerprint
                or snapshot.get('dependency_binding') != prepared['dependency_binding']
                or snapshot.get('tool') != request.get('tool')
                or snapshot.get('args') != request.get('args')
                or snapshot.get('config_fingerprint') != digest(request.get('config'))
                or snapshot.get('state_fingerprint') != digest(request.get('state'))
                or snapshot.get('host_rpc_allowlist', []) != []
                or set(snapshot.get('permissions', [])) - _ALLOWED_PERMISSIONS):
            _refuse('strict_runtime_manager_authority_binding_changed')
        package_input = {'package_root': prepared['package_root'], 'manifest': prepared['manifest'],
            'plugin_id': identity.plugin_id, 'revision': identity.revision, 'digest': identity.digest,
            'tool': request.get('tool'), 'args': request.get('args'), 'config': request.get('config'),
            'state': request.get('state'), 'trust_fingerprint': identity.trust_fingerprint,
            'dependency_binding': prepared['dependency_binding']}
        plan, invocation, runtime = self._validated(spec, **package_input)
        _requests, expected_targets = request_contract(prepared['manifest'], request.get('tool'), request.get('args'))
        declared_tool = next(item for item in prepared['manifest']['tools'] if item['name'] == request['tool'])
        expected_permissions = sorted({'python.execute'} | set(prepared['manifest'].get('permissions', []))
                                      | set(declared_tool.get('permissions', [])))
        if (snapshot.get('targets', []) != list(expected_targets)
                or snapshot.get('permissions', []) != expected_permissions):
            _refuse('strict_runtime_manager_scope_changed')
        # No boolean comes from RPC; confirmation is the complete mechanically
        # checked manager-consumed snapshot + current host binding, not a default.
        authorized = (self._live(identity, snapshot) is True and not context.cancel_event.is_set())
        if not authorized:
            _refuse('strict_runtime_live_authority_changed')
        return prepared, snapshot, spec, plan, invocation, runtime, lease, authorized

    def run_managed(self, context, register_cancel):
        if not callable(register_cancel):
            _refuse('strict_runtime_cancel_registration_required')
        identity = getattr(context, 'identity', None)
        if type(identity) is not ExecutionIdentity:
            _refuse('strict_runtime_execution_context_required')
        owner = PreparationOwner()
        record = {'identity': identity, 'owner': owner, 'host': None, 'receipt': None,
                  'host_entered': False, 'host_attempted': False, 'observer_bundle': None,
                  'channel_io': None, 'profile_adapter': None, 'ledger': None,
                  'finished': False, 'stage': 'authority', 'broker_adapters':None}
        with self._lock:
            if self.closed or identity.call_id in self._calls or len(self._calls) >= _MAX_CALLS:
                _refuse('strict_runtime_call_already_attempted_or_capacity')
            self._calls[identity.call_id] = record
        try:
            prepared_input, snapshot, spec, plan, invocation, runtime, lease, authorized = self._context_authority(context)
            with self._lock:
                if spec['fingerprint'] in self._used_specs:
                    _refuse('strict_runtime_spec_already_attempted')
                self._used_specs.add(spec['fingerprint'])
            if self.execution_evidence('ordinary') is None:
                _refuse('sandbox_unavailable')
            record['stage'] = 'sources'
            owner.retain(observe_source(runtime.root, (*runtime.entries, runtime.manifest_entry)))
            owner.retain(observe_source(os.fspath(prepared_input['package_root']), plan.code_files))
            if not self._live(identity, snapshot) or context.cancel_event.is_set():
                _refuse('strict_runtime_live_authority_changed')
            record['stage'] = 'prepare_tree'
            prepared, observer = prepare_tree(plan, approved_parent=self.approved_parent,
                creation_nonce=spec['creation_nonce'], root_intent=spec['root_creation'],
                runtime_source=runtime.root, package_root=os.fspath(prepared_input['package_root']),
                owner=owner, authorized=authorized, cancel_event=context.cancel_event)
            if observer.network_isolation(plan.appcontainer_sid).verified is not True:
                _refuse('strict_runtime_loopback_state_unverified')
            from app.plugin_native_provision import NativeProvisionAdapter
            from app.plugin_sandbox_provision import ProvisionLedger
            from app.plugin_native_channel import NativeBoundedIO
            from app.plugin_windows_launcher import WindowsLauncher
            from app.plugin_echo_native_host import OwnedEchoNativeApi, NativeEchoObserverBundle
            adapter = NativeProvisionAdapter(prepared, creation_nonce=spec['creation_nonce'],
                observation_approved=authorized, profile_folder_observation_approved=authorized,
                defer_child_access=True)
            record['profile_adapter'] = adapter
            ledger = ProvisionLedger(self.ledger_path, approved_parent=self.approved_parent,
                native_adapter=adapter, clock=self.clock, defer_child_access=True)
            record['ledger'] = ledger
            channel_io = NativeBoundedIO(clock=self.clock)
            record['channel_io'] = channel_io
            native = OwnedEchoNativeApi(access_request_provider=adapter.child_access_request,
                prepared=prepared, network_observer=observer)
            host = _ordinary_host()(clock=self.clock)
            record['host'] = host
            preview = host.preview(invocation)
            def revalidate_runtime():
                for held in owner.leases:
                    if not held.closed:
                        held.revalidate()
                return (read_portable_runtime(runtime.root) == runtime
                        and self._live(identity, snapshot) is True
                        and not context.cancel_event.is_set())
            bundle = NativeEchoObserverBundle(plan=invocation, prepared=prepared, ledger=ledger,
                native_api=native, channel_io=channel_io, scheduler_lease=lease,
                runtime_revalidator=revalidate_runtime,
                live_intent_provider=lambda current: invocation.intent if current == invocation.intent
                    and self._live(identity, snapshot) and not context.cancel_event.is_set() else None,
                network_observer=observer)
            record['observer_bundle'] = bundle
            if (preview['fingerprint'] != spec['invocation_fingerprint']
                    or not revalidate_runtime() or context.cancel_event.is_set()):
                host.cancel_preview(preview['token'], preview['fingerprint'])
                _refuse('strict_runtime_prelaunch_binding_changed')
            broker_adapters = None
            if invocation.intent.operations:
                broker_plan = self.broker_services.prepare(**{
                    'manifest':prepared_input['manifest'], 'tool':prepared_input['request']['tool'],
                    'args':prepared_input['request']['args'], 'config':prepared_input['request']['config'],
                    'state':prepared_input['request']['state'], 'plugin_id':identity.plugin_id,
                    'revision':identity.revision, 'digest':identity.digest,
                    'trust_fingerprint':identity.trust_fingerprint,
                    'dependency_binding':prepared_input['dependency_binding'], 'call_id':identity.call_id})
                if broker_plan.projection() != spec.get('broker_scope'):
                    _refuse('strict_runtime_broker_scope_changed')
                broker_adapters = self.broker_services.invocation(broker_plan,
                    live_authority=lambda:self._live(identity, snapshot) is True and not context.cancel_event.is_set())
                record['broker_adapters'] = broker_adapters
            record['stage'] = 'native_invocation'
            # Serialize only the shared journal preview/commit, not child runtime.
            # All admitted calls retain their own Resume-derived budget/Job/pipe owner.
            if not revalidate_runtime():
                host.cancel_preview(preview['token'], preview['fingerprint'])
                _refuse('strict_runtime_authority_changed_before_native')
            record['host_entered'] = True
            record['host_attempted'] = True
            outcome = host.execute(preview['token'], preview['fingerprint'], user_confirmed=authorized,
                prepared=prepared, provision_adapter=adapter, ledger=ledger,
                channel_io=channel_io, native_launcher=WindowsLauncher(native, clock=self.clock),
                observers=bundle.observers(), admission_lease=lease,provision_lock=self._provision_lock,
                cancel_event=context.cancel_event, register_cancel=register_cancel,
                broker_adapters=broker_adapters)
            receipt = outcome.get('cleanup_receipt')
            if self._valid_receipt(identity, receipt):
                record['receipt'] = receipt
            if outcome.get('ok') is not True:
                _refuse(outcome.get('code') or 'strict_runtime_outcome_unknown')
            return outcome.get('result')
        except BaseException as error:
            owner.absorb(error)
            if isinstance(error, RuntimeFailure):
                raise
            _refuse(getattr(error, 'code', 'strict_runtime_outcome_unknown'))
        finally:
            record['finished'] = True
            if record['broker_adapters'] is not None:
                record['broker_adapters'].close()
            # Only owned handles are closed, never a created profile/tree/ACL. An
            # outstanding native probe must keep all prepared/source path leases.
            bundle = record['observer_bundle']
            safe = bundle is None
            if bundle is not None:
                try:
                    safe = bundle.cleanup_gate() is True
                except BaseException:
                    safe = False
            host = record['host']
            if host is not None and identity.call_id in host.pending_resources():
                safe = False
            if record['host_attempted'] and not self._valid_receipt(identity, record['receipt']):
                safe = False
            if safe:
                owner.close()

    @staticmethod
    def _valid_receipt(identity, receipt):
        return (type(receipt) is ResourceCleanupReceipt and receipt.identity == identity
            and receipt.source == 'host_os_query' and receipt.job_terminated_or_exited is True
            and receipt.pipes_closed is True and receipt.writer_threads_stopped is True
            and receipt.cancelhooks_completed is True)

    def cleanup_receipt(self, identity):
        """Never turn missing/unknown native ownership into a synthetic OS receipt."""
        with self._lock:
            record = self._calls.get(getattr(identity, 'call_id', None))
        if record is None or record['identity'] != identity or not record['finished']:
            return None
        if record['owner'].leases or record['owner'].streams:
            return None
        with self._lock:
            if self._inspection_owners:
                return None
        host = record['host']
        if host is not None and identity.call_id in host.pending_resources():
            return None
        if self._valid_receipt(identity, record['receipt']):
            return record['receipt']
        # Before strict-host entry there cannot have been a child/pipe/Job: this
        # exact production owner observed its own stage and closed every actual
        # preparation handle. Created paths/profiles are not silently deleted.
        if not record['host_attempted']:
            bundle = record['observer_bundle']
            if bundle is not None:
                try:
                    if bundle.cleanup_gate() is not True:
                        return None
                except BaseException:
                    return None
            io = record['channel_io']
            if io is not None and (io._orphaned or any(
                    not state.closed or state.owned or state.writer is not None
                    or state.launch_lock_held for state in io._states.values())):
                return None
            record['receipt'] = ResourceCleanupReceipt(identity, True, True, True, True, 'host_os_query')
            return record['receipt']
        return None

    def retry_inspection_cleanup(self):
        """Explicit repair for readonly status/preview leases, without runtime retry."""
        with self._lock:
            owners = tuple(self._inspection_owners)
        failed = []
        for owner in owners:
            try:
                owner.close()
                if owner.closed is not True:
                    failed.append(owner)
            except BaseException:
                failed.append(owner)
        with self._lock:
            # Retain owners concurrently registered by another readonly query.
            self._inspection_owners = [item for item in self._inspection_owners
                if not any(item is old for old in owners)] + failed
            return not self._inspection_owners

    def retry_cleanup(self, identity):
        """Explicit host repair only; no execution/creation retry or result rewrite."""
        with self._lock:
            record = self._calls.get(getattr(identity, 'call_id', None))
        if record is None or record['identity'] != identity or not record['finished']:
            _refuse('strict_runtime_cleanup_owner_unknown')
        host = record['host']
        if host is not None and identity.call_id in host.pending_resources():
            receipt = host.retry_cleanup(identity.call_id)
            if self._valid_receipt(identity, receipt):
                record['receipt'] = receipt
        bundle = record['observer_bundle']
        if bundle is not None and bundle.cleanup_gate() is not True:
            return None
        if not record['owner'].close():
            return None
        return self.cleanup_receipt(identity)

    def pending_resources(self):
        broker_pending = {} if self.broker_services is None else self.broker_services.pending_resources()
        with self._lock:
            return {call_id: {'stage': record['stage'],
                'held_observation_leases': len(record['owner'].leases),
                'held_copy_streams': len(record['owner'].streams),
                'created_root': record['owner'].created_root,
                'creation_uncertain': record['owner'].creation_uncertain,
                'native_cleanup_pending': bool(record['host'] and call_id in record['host'].pending_resources()),
                'broker_file_ownership':broker_pending.get(call_id, []),
                'profile_or_acl_removed': False}
                for call_id, record in self._calls.items()
                if record['owner'].leases or record['owner'].streams or call_id in broker_pending
                or record['host'] and call_id in record['host'].pending_resources()}

    def close(self):
        # Coordinator owns cancellation. Closing a selection never invents a second
        # event/lease, kills unrelated resources, drops unknown owners or retries IO.
        with self._lock:
            self.closed = True
        if self.broker_services is not None:
            self.broker_services.close()
        remaining=[]
        for owner in self._inspection_owners:
            try:
                owner.close()
                if getattr(owner,'closed',None) is not True:remaining.append(owner)
            except BaseException:remaining.append(owner)
        self._inspection_owners=remaining
        if remaining:_refuse('strict_runtime_inspection_cleanup_pending')
