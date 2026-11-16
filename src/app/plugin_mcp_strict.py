"""Host-only strict MCP driver and private native composition; no legacy fallback.

Import/construction/preview create no process, profile, ACL, worker or directories.
A host preview provider must reserve the exact root/profile/SID/call_id/nonce before
manager approval. Only a consumed manager approval capability admits execution.
Unconfigured permission proxies explicitly refuse; HTTP MCP is never treated as
stdio. Availability evidence is delegated unchanged to a real host query provider.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from contextlib import nullcontext
import hashlib
import json
import math
import re
import secrets
import threading
import time
from types import SimpleNamespace

from app.plugin_execution import ExecutionContext, ExecutionIdentity, ResourceCleanupReceipt
from app.plugin_runtime import RuntimeFailure
from app.plugin_native_channel import NativeBoundedIO, plan_channel_creation
from app.plugin_native_provision import HostPreparedTree, NativeProvisionAdapter
from app.plugin_echo_native_host import OwnedEchoNativeApi, NativeEchoObserverBundle
from app.plugin_sandbox_provision import ProvisionLedger
from app.plugin_sandbox_policy import (LaunchPlan, IpcHandle, MCP_IPC_ROLES,
    SandboxPolicy, SandboxPaths, CallBinding, JobPlan, CREATION_FLAGS, JOB_LIMIT_FLAGS, CPU_RATE_ENABLE, CPU_RATE_HARD_CAP,
    _grants, plan_fingerprint, bind_ipc_handles, semantic_plan_projection,
    inventory_fingerprint, validate_launch_plan)
from app.plugin_windows_observer import WindowsObserver
from app.plugin_windows_launcher import WindowsLauncher, plan_launch
from app.plugin_proxy import BrokerSession, IntentSandboxPolicy
from app.plugin_transport import HostChannelAttestation, TrustedChannel, MAX_TOTAL_BYTES
from app.plugin_wire import (MAX_FRAME, FrameDecoder, encode_json, decode_json, encode_frame)


class McpStrictError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _clone(value):
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _operation(snapshot):
    return {key:_clone(snapshot.get(key)) for key in ('id','expected_revision','digest','mcp_name',
        'declaration','mcp_operation','tool','args','tool_schema','tool_definition','permissions','trust',
        'dependency_binding','config_fingerprint','state_fingerprint')}


def mcp_operation_fingerprint(snapshot):
    """Pure helper for the trusted preview provider; binds manager semantic fields."""
    return _hash(_operation(snapshot))


@dataclass(frozen=True)
class McpLaunchSpec:
    """Trusted preview product. Physical handles are deliberately absent."""
    policy_plan: LaunchPlan
    profile_name: str
    nonce: str
    manifest_json: bytes = field(repr=False)
    executable: str
    environment: tuple[tuple[str, str], ...]
    operation_sha256: str

    def snapshot(self):
        validate_launch_plan(self.policy_plan, require_physical=False)
        plan = self.policy_plan
        if (plan.ipc_stage != 'semantic' or plan.policy.mode != 'stdio_mcp'
                or plan.ipc_roles != MCP_IPC_ROLES or plan.inherited_handles
                or not re.fullmatch(r'Lingxi\.[A-Za-z0-9_.-]{8,100}', self.profile_name)
                or not re.fullmatch(r'[0-9a-f]{32,128}', self.nonce)
                or not re.fullmatch(r'[0-9a-f]{64}', self.operation_sha256)):
            raise McpStrictError('mcp_exact_semantic_spec_required')
        manifest = decode_json(self.manifest_json)
        if (type(manifest) is not dict or hashlib.sha256(encode_json(manifest)).hexdigest() != plan.binding.digest
                or manifest.get('id') != plan.binding.plugin_id
                or any({e.relative_path:e.sha256 for e in plan.code_files}.get(name) != digest
                       for name,digest in manifest.get('files',{}).items())):
            raise McpStrictError('mcp_manifest_inventory_binding')
        plan_launch(plan, self.executable, (self.executable,'--package-mcp-server'),
                    dict(self.environment), semantic_only=True)
        body = {'v':1, 'mode':'stdio_mcp', 'call_id':plan.binding.call_id, 'nonce':self.nonce,
                'policy':_clone(semantic_plan_projection(plan)), 'profile_name':self.profile_name,
                'manifest_sha256':hashlib.sha256(self.manifest_json).hexdigest(),
                'operation_sha256':self.operation_sha256, 'executable':self.executable,
                'environment':dict(self.environment), 'permission_proxy':'unavailable-deny'}
        return {**body, 'fingerprint':_hash(body)}


def make_mcp_launch_spec(*, snapshot, binding, paths, profile_name, appcontainer_sid, nonce,
                         code_files, runtime_files, manifest, executable, environment):
    """Pure exact-root approval intent BEFORE root/profile/ACL creation.

    This deliberately does not invoke build_launch_plan's existing-tree observer:
    an absent future root cannot supply honest PathObservation values. All paths,
    inventories, grants, SID and limits are syntactically/integrity validated here;
    real OS/path/inventory/profile/token checks are still mandatory in prepare and
    WindowsLauncher before Resume. A semantic intent is never execution evidence.
    """
    if type(binding) is not CallBinding or type(paths) is not SandboxPaths:
        raise McpStrictError('mcp_exact_preview_binding_required')
    policy = SandboxPolicy.stdio_mcp()
    from app.plugin_sandbox_policy import canonical_local_path
    canonical_local_path(paths.root)
    if not re.fullmatch(r'sandbox-lab-[0-9a-f]{32}', paths.root.rsplit('\\',1)[-1]):
        raise McpStrictError('mcp_fresh_uuid_root_intent_required')
    raw_manifest = encode_json(manifest,safe=True)
    plan = LaunchPlan(policy,binding,paths,appcontainer_sid,(),(),
        code_files,runtime_files,inventory_fingerprint(runtime_files),_grants(paths,appcontainer_sid),(),
        CREATION_FLAGS,JobPlan(JOB_LIMIT_FLAGS,policy.memory_bytes,policy.memory_bytes,1,
            CPU_RATE_ENABLE|CPU_RATE_HARD_CAP,2000,30),'',ipc_stage='semantic',ipc_roles=MCP_IPC_ROLES)
    plan = replace(plan,fingerprint=plan_fingerprint(plan))
    value = McpLaunchSpec(plan,profile_name,nonce,raw_manifest,executable,
                         tuple(sorted(environment.items())),mcp_operation_fingerprint(snapshot))
    value.snapshot()
    return value


@dataclass(frozen=True, eq=False)
class McpManagerApproval:
    """Opaque manager-consumption capability; a copied JSON/boolean cannot mint it."""
    _owner: object = field(repr=False)
    _token: str = field(repr=False)
    identity: ExecutionIdentity
    spec_fingerprint: str
    approval_fingerprint: str


@dataclass(frozen=True)
class McpPreparedComposition:
    """Only trusted host code can supply these live resource owners after approval.

    prepare_provider must retain/return a HostPreparedTree for the already approved
    root; no replacement root or second scheduler is accepted. On partial failure,
    attach mcp_preparation_cleanup with a real cleanup_gate for ALL partial owners;
    an isolated observation_cleanup is retained but never releases quarantine.
    """
    prepared: HostPreparedTree
    provision_adapter: NativeProvisionAdapter
    ledger: ProvisionLedger
    native_api: OwnedEchoNativeApi
    network_observer: WindowsObserver
    channel_io: NativeBoundedIO
    runtime_revalidator: object
    preparation_cleanup: object = None


class McpStdioDriver:
    """Real MCP newline JSON-RPC with independent BE4 control pipes, one deadline."""
    def __init__(self, channel, io, *, verify_channel, clock=time.monotonic):
        if type(channel) is not TrustedChannel or type(io) is not NativeBoundedIO or not callable(verify_channel):
            raise McpStrictError('mcp_native_channel_required')
        if channel.attestation.mode != 'stdio_mcp':
            raise McpStrictError('mcp_five_handles_required')
        self.channel, self.io, self.verify, self.clock = channel, io, verify_channel, clock
        self.total = 0; self.notifications = 0; self._line = bytearray()
        self._messages = []; self._control = []; self._decoder = FrameDecoder()
        self._eof = {'stdout':False,'proxy':False,'diagnostics':False}
        self._next_id = 0

    def _check(self, deadline, cancel):
        self.io._check(deadline, cancel)
        actual = self.verify(self.channel)
        if type(actual) is not HostChannelAttestation or actual != self.channel.attestation:
            raise McpStrictError('mcp_channel_attestation_changed')

    def _account(self, count):
        self.total += count
        if self.total > MAX_TOTAL_BYTES:
            raise McpStrictError('mcp_wire_budget')

    def _pump(self, deadline, cancel):
        self._check(deadline, cancel)
        active = False
        for stream in ('diagnostics','proxy','stdout'):
            if self._eof[stream]:
                continue
            data, eof = self.io.poll_mcp(self.channel,stream,4096,deadline=deadline,cancel=cancel)
            self._account(len(data)); active = active or bool(data)
            self._eof[stream] = eof
            if stream == 'proxy':
                messages = self._decoder.feed(data)
                if any(type(message) is not dict or message.get('type') == 'mcp_refused' for message in messages):
                    raise McpStrictError('mcp_permission_proxy_or_worker_refused')
                self._control.extend(messages)
                if eof:
                    self._decoder.eof()
            elif stream == 'stdout':
                self._line.extend(data)
                while b'\n' in self._line:
                    line, _, rest = self._line.partition(b'\n')
                    self._line[:] = rest
                    if line.endswith(b'\r'):
                        line = line[:-1]  # Windows text stdio may emit CRLF.
                    if not line or len(line) > MAX_FRAME or b'\r' in line:
                        raise McpStrictError('mcp_stdio_framing')
                    value = decode_json(bytes(line))
                    if type(value) is not dict or value.get('jsonrpc') != '2.0':
                        raise McpStrictError('mcp_jsonrpc')
                    self._messages.append(value)
                if len(self._line) > MAX_FRAME or eof and self._line:
                    raise McpStrictError('mcp_stdio_truncated')
        if not active:
            self.io.sleep(min(.005, max(0,deadline-self.clock())))
        self._check(deadline, cancel)

    def _send(self, stream, data, deadline, cancel):
        self._account(len(data)); offset = 0
        while offset < len(data):
            self._check(deadline, cancel)
            count = self.io.write_mcp(self.channel,stream,data[offset:],deadline=deadline,cancel=cancel)
            if type(count) is not int or not 0 < count <= len(data)-offset:
                raise McpStrictError('mcp_native_write')
            offset += count
            self._pump(deadline,cancel)

    def _control_message(self, kind, init, deadline, cancel):
        while not self._control:
            if self._eof['proxy']:
                raise McpStrictError('mcp_control_eof')
            self._pump(deadline,cancel)
        value = self._control.pop(0)
        expected = {'v','type','call_id','nonce'} | ({'returncode'} if kind == 'mcp_server_exit' else set())
        if (type(value) is not dict or set(value) != expected or type(value.get('v')) is not int
                or value['v'] != 1 or value.get('type') != kind
                or value.get('call_id') != init['call_id'] or value.get('nonce') != init['nonce']):
            raise McpStrictError('mcp_control_or_permission_proxy_refused')
        return value

    def _request(self, method, params, deadline, cancel):
        self._next_id += 1; request_id = self._next_id
        self._send('stdin',encode_json({'jsonrpc':'2.0','id':request_id,'method':method,'params':params})+b'\n',deadline,cancel)
        while True:
            while not self._messages:
                if self._eof['stdout']:
                    raise McpStrictError('mcp_server_eof_before_response')
                if self._control:
                    raise McpStrictError('mcp_server_exited_or_proxy_refused')
                self._pump(deadline,cancel)
            value = self._messages.pop(0)
            if 'method' in value:
                self.notifications += 1
                if ('id' in value or self.notifications > 60 or set(value)-{'jsonrpc','method','params'}
                        or value['method'] not in ('notifications/message','notifications/progress')):
                    raise McpStrictError('mcp_unsolicited_request_or_catalog_changed')
                continue
            if (set(value) not in ({'jsonrpc','id','result'}, {'jsonrpc','id','error'})
                    or type(value.get('id')) is not int or value['id'] != request_id):
                raise McpStrictError('mcp_response_binding')
            if 'error' in value:
                raise McpStrictError('mcp_server_error_no_retry')
            encode_json(value['result'],safe=True)
            return value['result']

    def run(self, init, *, deadline, cancel):
        if (type(deadline) not in (int,float) or not math.isfinite(deadline)
                or not 0 < deadline-self.clock() <= 30):
            raise McpStrictError('mcp_shared_resume_deadline_required')
        if hashlib.sha256(encode_json(init,safe=True)).hexdigest() != self.channel.attestation.approved_init_sha256:
            raise McpStrictError('mcp_init_changed')
        self._send('proxy',encode_frame(init),deadline,cancel)
        self._control_message('mcp_ready',init,deadline,cancel)
        initialized = self._request('initialize', {'protocolVersion':'2024-11-05','capabilities':{},
            'clientInfo':{'name':'LingxiStrictHost','version':'0.5'}}, deadline,cancel)
        if (type(initialized) is not dict or initialized.get('protocolVersion') not in
                ('2024-11-05','2025-03-26','2025-06-18') or type(initialized.get('capabilities')) is not dict
                or type(initialized['capabilities'].get('tools')) is not dict
                or type(initialized.get('serverInfo')) is not dict):
            raise McpStrictError('mcp_initialize_refused')
        self._send('stdin',encode_json({'jsonrpc':'2.0','method':'notifications/initialized'})+b'\n',deadline,cancel)
        tools = []; cursors = set(); params = {}
        for _ in range(101):
            listed = self._request('tools/list',params,deadline,cancel)
            if type(listed) is not dict or type(listed.get('tools')) is not list:
                raise McpStrictError('mcp_tools_list_shape')
            tools.extend(listed['tools'])
            if len(tools) > 100 or len(json.dumps(tools,ensure_ascii=False).encode()) > 196608:
                raise McpStrictError('mcp_catalog_limit')
            cursor = listed.get('nextCursor')
            if cursor is None:
                break
            if type(cursor) is not str or not cursor or cursor in cursors:
                raise McpStrictError('mcp_catalog_cursor')
            cursors.add(cursor); params = {'cursor':cursor}
        else:
            raise McpStrictError('mcp_catalog_limit')
        from app.package_mcp import _tools
        tools = _tools(tools)
        result = {'tools':tools}
        if init['operation'] == 'execute':
            matches = [item for item in tools if item['name'] == init['tool']]
            if len(matches) != 1:
                raise McpStrictError('mcp_tool_disappeared')
            # Exact schema equality is checked by the runtime before this method's
            # tools/call using the approved schema supplied in its closure.
            if (matches[0]['inputSchema'] != self.approved_tool_schema
                    or matches[0] != self.approved_tool_definition):
                raise McpStrictError('mcp_tool_schema_or_definition_changed')
            import jsonschema
            jsonschema.validate(init['args'],matches[0]['inputSchema'])
            called = self._request('tools/call',{'name':init['tool'],'arguments':init['args']},deadline,cancel)
            if type(called) is not dict or type(called.get('content')) is not list:
                raise McpStrictError('mcp_tools_call_shape')
            result = {'result':called}
        # EOF is standard stdio shutdown. A server ignoring it consumes the SAME
        # remaining deadline; watchdog termination never promotes a result to success.
        self.io.close_mcp_stdin(self.channel)
        exited = self._control_message('mcp_server_exit',init,deadline,cancel)
        if type(exited['returncode']) is not int or exited['returncode'] != 0:
            raise McpStrictError('mcp_server_exit_nonzero')
        while not all(self._eof.values()) or self.io.mcp_exit_code(self.channel) is None:
            self._pump(deadline,cancel)
            if self._messages or self._control:
                raise McpStrictError('mcp_trailing_output_or_exit')
        if self._messages or self._control or self._line or self.io.mcp_exit_code(self.channel) != 0:
            raise McpStrictError('mcp_trailing_output_or_exit')
        self._check(deadline,cancel)
        return result


class PluginMcpStrictRuntime:
    strict_isolation = True
    timeout = 30
    memory_limit_bytes = 512 * 1024**2

    def __init__(self, *, preview_provider, prepare_provider, execution_evidence_provider,
                 live_spec_provider, provision_lock=None, provider_cleanup=None, clock=time.monotonic):
        if not all(callable(value) for value in (preview_provider,prepare_provider,
                execution_evidence_provider,live_spec_provider,clock)):
            raise TypeError('explicit host-only MCP providers required')
        self._preview_provider, self._prepare_provider = preview_provider, prepare_provider
        self._evidence_provider, self._live_spec = execution_evidence_provider, live_spec_provider
        self.clock = clock; self._lock = threading.RLock(); self.closed = False
        self._provision_lock=provision_lock
        if provider_cleanup is not None and not callable(provider_cleanup):
            raise TypeError('host cleanup capability must be callable')
        self._provider_cleanup=provider_cleanup
        self._specs = {}; self._approvals = {}; self._receipts = {}; self._owners = {}; self._attempted = set()

    def execution_evidence(self, mode):
        if mode != 'mcp' or self.closed:
            return None
        # Never fabricate host_os_query or verified/adapter_ready values. The shared
        # selection validates every exact field returned by the real query provider.
        return self._evidence_provider(mode)

    @property
    def available(self):
        from app.plugin_runtime_selection import runtime_evidence
        return runtime_evidence(self,'mcp') is not None

    def preview_spec(self, snapshot):
        """Provider runs under manager's preview lock: pure/bounded host planning only.

        It may reserve random IDs and use already verified release/code inventories,
        but must never create/observe a new root, change ACL/profile or await a user.
        """
        if self.closed or snapshot.get('declaration',{}).get('transport') != 'stdio':
            raise RuntimeFailure('mcp_stdio_required','HTTP MCP 不属于严格 stdio 沙箱；未执行')
        request = _clone(snapshot)
        spec = self._preview_provider(request)
        if type(spec) is not McpLaunchSpec or spec.operation_sha256 != mcp_operation_fingerprint(snapshot):
            raise McpStrictError('mcp_host_preview_spec_required')
        public = spec.snapshot(); binding = spec.policy_plan.binding
        if (binding.plugin_id != snapshot['id'] or binding.revision != snapshot['expected_revision']
                or binding.digest != snapshot['digest']):
            raise McpStrictError('mcp_preview_identity')
        with self._lock:
            self._specs = {key:record for key,record in self._specs.items() if record[1] > self.clock()}
            if (len(self._specs) >= 128 or binding.call_id in self._specs or binding.call_id in self._attempted
                    or any(item[0].policy_plan.paths.root.casefold() == spec.policy_plan.paths.root.casefold()
                           or item[0].profile_name.casefold() == spec.profile_name.casefold()
                           or item[0].policy_plan.appcontainer_sid == spec.policy_plan.appcontainer_sid
                           or item[0].nonce == spec.nonce for item in self._specs.values())):
                raise McpStrictError('mcp_preview_capacity_or_reuse')
            self._specs[binding.call_id] = (spec,self.clock()+120)
        return public

    def bind_manager_approval(self, identity, snapshot, *, approval_fingerprint):
        """HOST ONLY: store calls after _take + full freshness, never browser/plugin RPC.

        Exact equality to the consumed approval, including strict_mcp_spec, is checked.
        No user_confirmed boolean and no execute-time root/call_id generation exists.
        """
        if type(identity) is not ExecutionIdentity or _hash(snapshot) != approval_fingerprint:
            raise McpStrictError('mcp_consumed_manager_approval_required')
        with self._lock:
            record = self._specs.get(identity.call_id)
            if record is None or record[1] <= self.clock():
                raise McpStrictError('mcp_preview_missing_or_expired')
            spec = record[0]; public = spec.snapshot()
            if (snapshot.get('strict_mcp_spec') != public or spec.operation_sha256 != _hash(_operation(snapshot))
                    or identity.plugin_id != spec.policy_plan.binding.plugin_id
                    or identity.revision != spec.policy_plan.binding.revision
                    or identity.digest != spec.policy_plan.binding.digest
                    or identity.trust_fingerprint != _hash(snapshot.get('trust'))):
                raise McpStrictError('mcp_manager_approval_changed')
            if identity.call_id in self._attempted or len(self._attempted) >= 4096:
                raise McpStrictError('mcp_approval_attempted_or_capacity')
            self._attempted.add(identity.call_id)
            capability = McpManagerApproval(self,secrets.token_hex(32),identity,public['fingerprint'],approval_fingerprint)
            self._approvals[capability._token] = capability
            return capability

    def _cleanup(self, owners):
        with owners['cleanup_lock']:
            return self._cleanup_locked(owners)

    def _cleanup_locked(self, owners):
        failures = []
        for name in ('running','process_guard','observer_bundle','channel','preparation_cleanup','prepared'):
            item = owners.get(name)
            if item is None:
                continue
            if name in ('prepared','preparation_cleanup') and any(owners.get(key) is not None
                    for key in ('running','process_guard','observer_bundle','channel')):
                failures.append(name); continue
            if name=='prepared' and owners.get('preparation_cleanup') is not None:
                failures.append(name); continue
            try:
                if name == 'process_guard':
                    api, handle, pid = item
                    if (api.process_id(handle) != pid or api.process_exited(handle) is not True
                            or type(api.exit_code(handle)) is not int or api.exit_code(handle) == 259):
                        raise McpStrictError('mcp_exit_cleanup_unverified')
                    api.close(handle)
                elif name == 'observer_bundle':
                    if item.cleanup_gate() is not True:
                        raise McpStrictError('mcp_probe_cleanup_pending')
                elif name == 'channel':
                    owners['io'].close(item)
                elif name == 'prepared':
                    item.lease.close()
                elif name == 'preparation_cleanup':
                    # Provider-specific owner must verify cleanup of ALL partial
                    # tree/probe/native resources, not just close one observation.
                    if not callable(getattr(item,'cleanup_gate',None)) or item.cleanup_gate() is not True:
                        raise McpStrictError('mcp_preparation_cleanup_unverified')
                    owners['preparation_pending'] = False
                else:
                    item.close()
                    if name == 'running' and item.closed is not True:
                        raise McpStrictError('mcp_job_cleanup_unverified')
                owners[name] = None
            except BaseException:
                failures.append(name)
        io = owners.get('io')
        if io is not None and io._orphaned:
            try: io.retry_orphan_cleanup()
            except BaseException: failures.append('orphaned_handles')
        if owners.get('cancel_pending'):
            failures.append('cancel_pending')
        if owners.get('preparation_pending'):
            # A provider that failed before handing over its actual owners cannot
            # be certified clean from the mere absence of a returned Python value.
            failures.append('preparation_ownership_unknown')
        return failures

    def run_managed(self, context, register_cancel):
        try:
            return self._run_managed(context,register_cancel)
        except BaseException:
            # Preflight refuses before a resource-creating provider is entered.
            # Do not quarantine a genuinely never-started call merely for bad input.
            if type(context) is ExecutionContext:
                with self._lock:
                    if context.identity.call_id not in self._owners and context.identity.call_id not in self._receipts:
                        self._receipts[context.identity.call_id] = ResourceCleanupReceipt(
                            context.identity,True,True,True,True,'host_os_query')
            raise

    def _run_managed(self, context, register_cancel):
        if type(context) is not ExecutionContext or not callable(register_cancel):
            raise McpStrictError('mcp_execution_context_required')
        identity = context.identity; prepared_request = context.prepared
        capability = prepared_request.get('strict_mcp_approval')
        with self._lock:
            if (self.closed or type(capability) is not McpManagerApproval or capability._owner is not self
                    or self._approvals.pop(capability._token,None) is not capability or capability.identity != identity):
                raise McpStrictError('mcp_manager_approval_capability_required')
            record = self._specs.pop(identity.call_id,None)
        if record is None or record[1] <= self.clock():
            raise McpStrictError('mcp_preview_expired')
        spec = record[0]; snapshot = prepared_request['approval_snapshot']; public = spec.snapshot()
        from app.plugin_runtime_selection import runtime_evidence
        current_evidence = runtime_evidence(self,'mcp')
        if (current_evidence is None
                or current_evidence['runtime_inventory_sha256'] != spec.policy_plan.runtime_manifest_sha256):
            raise McpStrictError('mcp_runtime_evidence_unavailable')
        if (public != snapshot.get('strict_mcp_spec') or _hash(snapshot) != capability.approval_fingerprint
                or spec.operation_sha256 != _hash(_operation(snapshot))
                or self._live_spec(identity,public) != public):
            raise McpStrictError('mcp_live_approval_changed')
        lease = context.admission_lease
        if (lease is None or lease.call_id != identity.call_id or lease.plugin_id != identity.plugin_id):
            raise McpStrictError('mcp_shared_admission_required')
        scheduler = lease._scheduler
        with scheduler._condition:
            if scheduler.max_concurrent != 10 or scheduler._running.get(identity.call_id) is not lease or lease._released:
                raise McpStrictError('mcp_shared_admission_not_owned')
        owners = {'identity':identity,'io':None,'running':None,'channel':None,'prepared':None,
                  'process_guard':None,'observer_bundle':None,'preparation_cleanup':None,
                  'preparation_pending':False,'cancel_pending':False,'active':True,
                  'cancel_event':context.cancel_event,'job_control':None,'cleanup_lock':threading.RLock()}
        with self._lock:
            self._owners[identity.call_id] = owners
        error = None; result = None
        try:
            if context.cancel_event.is_set():
                raise McpStrictError('mcp_cancelled_before_native')
            owners['preparation_pending'] = True
            composition = self._prepare_provider(context,spec)
            if type(composition) is not McpPreparedComposition:
                raise McpStrictError('mcp_host_composition_required')
            tree = composition.prepared; adapter = composition.provision_adapter; ledger = composition.ledger
            io = composition.channel_io; api = composition.native_api
            owners.update(prepared=tree,io=io,preparation_cleanup=composition.preparation_cleanup)
            if (type(tree) is not HostPreparedTree or tree.plan != spec.policy_plan or tree.creation_nonce != spec.nonce
                    or type(adapter) is not NativeProvisionAdapter or adapter.prepared is not tree or not adapter.defer_child_access
                    or type(ledger) is not ProvisionLedger or ledger.adapter is not adapter
                    or type(io) is not NativeBoundedIO or type(api) is not OwnedEchoNativeApi
                    or type(composition.network_observer) is not WindowsObserver or not callable(composition.runtime_revalidator)):
                raise McpStrictError('mcp_owned_native_composition_required')
            policy = IntentSandboxPolicy(spec.policy_plan.appcontainer_sid,spec.policy_plan.runtime_manifest_sha256,
                inventory_fingerprint(spec.policy_plan.code_files),_hash(asdict(spec.policy_plan.paths)),
                mode='mcp',wall_seconds=30,memory_limit_bytes=512*1024**2)
            native_plan = SimpleNamespace(policy_plan=spec.policy_plan,intent=SimpleNamespace(policy=policy))
            bundle = NativeEchoObserverBundle(plan=native_plan,prepared=tree,ledger=ledger,native_api=api,
                channel_io=io,scheduler_lease=lease,runtime_revalidator=composition.runtime_revalidator,
                live_intent_provider=lambda _intent:None,network_observer=composition.network_observer)
            owners['observer_bundle'] = bundle
            owners['preparation_pending'] = False
            if context.cancel_event.is_set() or self.closed or self._live_spec(identity,public) != public:
                raise McpStrictError('mcp_cancelled_or_revoked_before_native')
            creation = plan_channel_creation(identity.call_id,mode='stdio_mcp')
            state = io.create_channel(creation,user_confirmed=True,fingerprint=creation.fingerprint)
            owners['channel'] = state
            with state.lock:
                if tuple(role for role,_ in state.child_ends) != MCP_IPC_ROLES or len(state.owned) != 10:
                    raise McpStrictError('mcp_five_handles_required')
                hosts = (state.request_write,state.result_read,state.diagnostics_read,state.proxy_request_write,state.proxy_result_read)
                for values,inheritable in ((hosts,False),(tuple(value for _,value in state.child_ends),True)):
                    for handle in values:
                        if io.api.verify_pipe(handle,inheritable=inheritable) is not None:
                            raise McpStrictError('mcp_pipe_query_refused')
                physical = bind_ipc_handles(spec.policy_plan,tuple(IpcHandle(value,role,True,True,True) for role,value in state.child_ends))
            physical_tree = replace(tree,plan=physical)
            adapter.prepared = physical_tree; adapter.plan = physical; owners['prepared'] = physical_tree
            if context.cancel_event.is_set() or self.closed or self._live_spec(identity,public) != public:
                raise McpStrictError('mcp_cancelled_or_revoked_before_provision')
            with self._provision_lock if self._provision_lock is not None else nullcontext():
                preview = ledger.preview(physical,profile_name=spec.profile_name,expected_revision=ledger.snapshot()['revision'])
                adapter.authorize_writes(intent=preview['snapshot'],fingerprint=preview['fingerprint'],user_confirmed=True)
                provisioned = ledger.commit(preview['token'],preview['fingerprint'],user_confirmed=True)
            handles = dict(state.child_ends)
            launch = plan_launch(physical,spec.executable,(spec.executable,'--package-mcp-server',
                str(handles['proxy_request_read']),str(handles['proxy_result_write'])),dict(spec.environment))
            def host_verify(current):
                if context.cancel_event.is_set() or self._live_spec(identity,public) != public:
                    raise McpStrictError('mcp_live_approval_revoked')
                proof = ledger.readonly_verify(provisioned['id'],physical,profile_name=spec.profile_name)
                return bundle.host_evidence(current,proof,physical_tree,lease)
            bound = {}; job_control = [None]
            def created(child):
                duplicate = io.api.duplicate(child.process,inheritable=False)
                owners['process_guard'] = (io.api,duplicate,child.pid)
                io.api.verify_noninheritable(duplicate)
                if io.api.process_id(duplicate) != child.pid:
                    raise McpStrictError('mcp_process_identity_changed')
                io.mark_child_spawned(state)
            def stop():
                owners['cancel_pending'] = True
                try:
                    context.cancel_event.set()
                    if job_control[0] is not None:
                        job_control[0].cancel()
                finally:
                    owners['cancel_pending'] = False
            def suspended(child,observation,evidence,control):
                job_control[0] = control
                owners['job_control'] = control
                register_cancel(stop)
                if context.cancel_event.is_set():
                    stop(); raise McpStrictError('mcp_cancelled_before_resume')
                # Existing native implementation probes the held real child token,
                # all ancestors/runtime/code/scratch and reobserves the same Job.
                api.prepare_child_access()
                io.bind_process(state,child.process,child.pid,cancel_job=control.cancel)
                binding = BrokerSession(identity.plugin_id,identity.revision,identity.digest,identity.call_id,
                    child.pid,physical.appcontainer_sid,identity.trust_fingerprint)
                init = {'v':1,'type':'mcp_init','plugin_id':identity.plugin_id,'revision':identity.revision,
                    'digest':identity.digest,'call_id':identity.call_id,'nonce':spec.nonce,'package_root':physical.paths.code,
                    'manifest':decode_json(spec.manifest_json),'declaration':snapshot['declaration'],
                    'operation':snapshot['mcp_operation'],'tool':snapshot.get('tool'),'args':snapshot.get('args',{}),
                    'permission_proxy':'unavailable-deny'}
                attestation = HostChannelAttestation(binding,child.pid,physical.appcontainer_sid,'job_'+identity.call_id,
                    state.child_ends,physical.runtime_manifest_sha256,hashlib.sha256(encode_json(init,safe=True)).hexdigest(),
                    source=observation.source,is_appcontainer=observation.is_appcontainer,capabilities=observation.capabilities,
                    job_limits_verified=observation.job_membership_verified and observation.job==physical.job,
                    handle_whitelist_verified=observation.handle_whitelist_verified,
                    network_isolation_verified=evidence.network_isolation_verified,mode='stdio_mcp')
                bound.update(init=init,channel=TrustedChannel(binding,secrets.token_urlsafe(32),
                    state.result_read,state.request_write,state.diagnostics_read,attestation))
            running = WindowsLauncher(api=api,clock=self.clock).execute(launch,user_confirmed=True,
                fingerprint=launch.fingerprint,host_verifier=host_verify,on_child_created=created,on_verified_suspended=suspended)
            owners['running'] = running
            def verify_channel(channel):
                if self._live_spec(identity,public) != public:
                    raise McpStrictError('mcp_live_approval_revoked')
                return api.channel_evidence(channel,running.child.process,launch,io)
            driver = McpStdioDriver(bound['channel'],io,verify_channel=verify_channel,clock=self.clock)
            driver.approved_tool_schema = snapshot.get('tool_schema')
            driver.approved_tool_definition = snapshot.get('tool_definition')
            result = driver.run(bound['init'],deadline=running.runtime_deadline,cancel=context.cancel_event)
        except BaseException as failure:
            error = failure
            if getattr(failure,'sandbox_cleanup',None) is not None:
                owners['running'] = failure.sandbox_cleanup
            partial=owners.get('preparation_cleanup')
            absorb=getattr(partial,'absorb',None)
            if callable(absorb):
                absorb(failure)
            elif getattr(failure,'mcp_preparation_cleanup',None) is not None:
                owners['preparation_cleanup'] = failure.mcp_preparation_cleanup
            elif getattr(failure,'observation_cleanup',None) is not None:
                # A raw observation lease alone cannot prove all preparation clear.
                owners['preparation_cleanup'] = failure.observation_cleanup
        finally:
            with owners['cleanup_lock']:
                owners['active'] = False
                faults = self._cleanup_locked(owners)
            if not faults:
                with self._lock:
                    self._owners.pop(identity.call_id,None)
                    self._receipts[identity.call_id] = ResourceCleanupReceipt(identity,True,True,True,True,'host_os_query')
        if faults:
            raise RuntimeFailure('mcp_cleanup_unconfirmed','严格 MCP 清理未核验；保留共享槽位，不自动重试')
        if error is not None:
            raise RuntimeFailure(getattr(error,'code','strict_mcp_refused'),'严格 MCP 执行拒绝或结果未知；不自动重试') from None
        return result

    def cleanup_receipt(self, identity):
        with self._lock:
            receipt = self._receipts.get(identity.call_id)
        return receipt if receipt is not None and receipt.identity == identity else None

    def retry_cleanup(self, identity):
        with self._lock:
            owners = self._owners.get(identity.call_id)
        if owners is None or owners['identity'] != identity:
            raise McpStrictError('mcp_cleanup_unconfirmed')
        with owners['cleanup_lock']:
            if owners['active'] or self._cleanup_locked(owners):
                raise McpStrictError('mcp_cleanup_unconfirmed')
        receipt = ResourceCleanupReceipt(identity,True,True,True,True,'host_os_query')
        with self._lock:
            self._owners.pop(identity.call_id,None); self._receipts[identity.call_id] = receipt
        return receipt

    def dependency_evidence(self, _requirement):
        return None

    def close(self):
        with self._lock:
            self.closed = True; self._specs.clear(); self._approvals.clear()
            owners = tuple(self._owners.values())
        for item in owners:
            with item['cleanup_lock']:
                if item['active']:
                    # Do not race a live prepare/launch by closing its borrowed
                    # resources. Cancel the same held Job; its executing owner does
                    # final cleanup and publishes the receipt when actually safe.
                    item['cancel_event'].set()
                    control = item.get('job_control')
                    if control is not None:
                        try: control.cancel()
                        except BaseException: pass
                else:
                    self._cleanup_locked(item)  # Failed owners remain for retry.
        if self._provider_cleanup is not None and self._provider_cleanup() is not True:
            raise McpStrictError('mcp_provider_cleanup_pending')


def make_mcp_strict_runtime(*, preview_provider, prepare_provider, execution_evidence_provider,
                            live_spec_provider, provision_lock=None, provider_cleanup=None, clock=time.monotonic):
    """Private package_host composition: StrictRuntimeSelection(mcp=this return value).

    Missing real providers should leave BlockedStrictRuntime in selection, not invent
    evidence or fill fields with True. No provider is imported from package/config.
    """
    return PluginMcpStrictRuntime(preview_provider=preview_provider,prepare_provider=prepare_provider,
        execution_evidence_provider=execution_evidence_provider,live_spec_provider=live_spec_provider,
        provision_lock=provision_lock,provider_cleanup=provider_cleanup,clock=clock)
