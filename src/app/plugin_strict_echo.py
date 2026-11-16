"""Host-only one-shot strict Echo wiring; import/preview do not perform native IO.

No root creation/copying, arbitrary operations, legacy runtime, or implicit observers.
Only an explicitly prepared tree and complete trusted native observers can execute.
Fake-component tests are not OS acceptance. The producer of every observer must
query held resources; Python receipt types alone cannot establish OS identity.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from contextlib import nullcontext
import hashlib
import json
import math
import re
import threading
import time

from app.plugin_execution import ExecutionIdentity, ResourceCleanupReceipt
from app.plugin_native_channel import NativeBoundedIO, plan_channel_creation
from app.plugin_native_provision import HostPreparedTree, NativeProvisionAdapter
from app.plugin_proxy import (HostOperationIntent, IntentApprovalStore, IntentSandboxPolicy,
    PluginProxyBroker, BrokerSession, VerifiedIntentChild, bound_init_fingerprint,
    encode_frame, encode_json, decode_json)
from app.plugin_sandbox_policy import (LaunchPlan, IpcHandle, SandboxPolicyError,
    bind_ipc_handles, semantic_plan_projection, inventory_fingerprint)
from app.plugin_sandbox_provision import ProvisionLedger
from app.plugin_scheduler import SchedulerLease
from app.plugin_transport import HostChannelAttestation, TrustedChannel, PluginTransport
from app import plugin_windows_launcher as launcher


def _hash(value):
    return hashlib.sha256(encode_json(value, safe=True)).hexdigest()


def _policy_projection(plan):
    try:
        value=semantic_plan_projection(plan)
    except SandboxPolicyError as error:
        raise EchoError('echo_policy_invalid') from error
    return json.loads(json.dumps(value,ensure_ascii=False,allow_nan=False))


class EchoError(ValueError):
    def __init__(self,code):
        super().__init__(code);self.code=code


@dataclass(frozen=True)
class EchoPlan:
    policy_plan: LaunchPlan
    profile_name: str
    manifest_json: bytes = field(repr=False)
    intent: HostOperationIntent = field(repr=False)
    executable: str
    environment: tuple[tuple[str,str],...]
    fingerprint: str


@dataclass(frozen=True)
class EchoObservers:
    """All callbacks are trusted, bounded host work, never plugin-wire providers.

    host_evidence(launch, ledger_proof, prepared, lease) -> TrustedHostEvidence
    verify_child(child_receipt, borrowed_process_handle, launch) -> VerifiedIntentChild
    live_intent(intent) -> exactly current HostOperationIntent or None
    channel_evidence(channel, borrowed_process_handle, launch) -> current attestation

    Missing network/profile/ancestor observers in these implementations must fail;
    no callback here has a default True or synthesized PID.
    """
    host_evidence: object
    verify_child: object
    live_intent: object
    channel_evidence: object
    resource_owner: object = None

    def validate(self):
        if not all(callable(getattr(self,key)) for key in
                   ('host_evidence','verify_child','live_intent','channel_evidence')):
            raise EchoError('strict_echo_host_not_configured')
        if self.resource_owner is not None and not callable(getattr(self.resource_owner,'cleanup_gate',None)):
            raise EchoError('strict_echo_resource_owner_not_configured')


def _bind_channel_plan(plan, channel_io, channel):
    """Bind only this NativeBoundedIO owner's still-held, freshly queried endpoints.

    Channel flags/receipt fields do not prove a native pipe. verify_pipe performs
    GetHandleInformation/GetFileType in production and raises on any query failure.
    The launch lock stays held until the launcher's child-created hook closes the
    host copies; the launcher independently queries inheritance/type before create.
    """
    if type(channel_io) is not NativeBoundedIO:
        raise EchoError('echo_native_channel_required')
    with channel_io._lock:
        state=channel_io._state(channel)
        with state.lock:
            children=state.child_ends
            if (state.call_id!=plan.binding.call_id or state.child_closed
                    or state.launch_lock_held is not True or type(children) is not tuple
                    or len(children)!=3 or any(type(item) is not tuple or len(item)!=2 for item in children)
                    or tuple(role for role,_ in children)!=plan.ipc_roles
                    or any(type(value) is not int or value<=3 or value not in state.owned for _,value in children)
                    or len({value for _,value in children})!=3):
                raise EchoError('echo_native_channel_binding')
            hosts=(state.request_write,state.result_read,state.diagnostics_read)
            if (any(type(value) is not int or value<=3 or value not in state.owned for value in hosts)
                    or len(set(hosts))!=3 or set(hosts)&{value for _,value in children}):
                raise EchoError('echo_native_channel_binding')
            # NativePipeApi's checked-command contract is None on success, raise
            # on failed GetHandleInformation/GetFileType; a boolean is not proof.
            for values,inheritable in ((hosts,False),(tuple(value for _,value in children),True)):
                for value in values:
                    if channel_io.api.verify_pipe(value,inheritable=inheritable) is not None:
                        raise EchoError('echo_native_pipe_query_invalid')
            return bind_ipc_handles(plan,tuple(IpcHandle(value,role,True,True,True) for role,value in children))


class StrictEchoHost:
    _echo_only = True

    def __init__(self, *, approvals=None, clock=time.monotonic):
        if not callable(clock):raise TypeError('explicit host clock required')
        self.clock=clock
        self.approvals=approvals if approvals is not None else IntentApprovalStore(clock=clock)
        if type(self.approvals) is not IntentApprovalStore:raise TypeError('host intent store required')
        self._issued={};self._used=set();self._pending={};self._lock=threading.RLock()

    @classmethod
    def make_plan(cls, policy_plan, *, profile_name, manifest, tool='echo', args=None,
                  config=None, state=None, trust_fingerprint, dependency_fingerprint,
                  executable, environment, operations=(), file_descriptors=None,
                  data_descriptors=None, http_descriptors=None, model_descriptors=None):
        projection=_policy_projection(policy_plan)
        if cls._echo_only and tool!='echo':raise EchoError('echo_only_tool_required')
        if cls._echo_only and (operations or any((file_descriptors, data_descriptors, http_descriptors, model_descriptors))):
            raise EchoError('echo_external_operations_forbidden')
        if policy_plan.policy.mode!='normal' or not re.fullmatch(r'Lingxi\.[A-Za-z0-9_.-]{8,100}',profile_name):
            raise EchoError('echo_mode_or_profile_invalid')
        raw=encode_json(manifest,safe=True)
        if (type(manifest) is not dict or hashlib.sha256(raw).hexdigest()!=policy_plan.binding.digest
                or manifest.get('id')!=policy_plan.binding.plugin_id
                or manifest.get('entrypoint',{}).get('type')!='python'
                or not any(type(item) is dict and item.get('name')==tool for item in manifest.get('tools',[]))):
            raise EchoError('echo_manifest_binding')
        files={entry.relative_path:entry.sha256 for entry in policy_plan.code_files}
        # manifest.json is optional inventory metadata, not an executable import.
        if any(files.get(name)!=digest for name,digest in manifest.get('files',{}).items()):
            raise EchoError('echo_code_inventory_binding')
        policy=IntentSandboxPolicy(policy_plan.appcontainer_sid,policy_plan.runtime_manifest_sha256,
            inventory_fingerprint(policy_plan.code_files),_hash(asdict(policy_plan.paths)))
        binding=policy_plan.binding
        intent=HostOperationIntent.create(binding.plugin_id,binding.revision,binding.digest,binding.call_id,
            trust_fingerprint,policy,tool=tool,args=args or {},config=config or {},state=state or {},
            dependency_fingerprint=dependency_fingerprint,operations=operations,
            file_descriptors=file_descriptors, data_descriptors=data_descriptors,
            http_descriptors=http_descriptors, model_descriptors=model_descriptors)
        # Validate exact command/environment against the already validated template.
        launcher.plan_launch(policy_plan,executable,(executable,'--plugin-worker'),environment,
                             semantic_only=policy_plan.ipc_stage=='semantic')
        body={'policy':projection,'profile_name':profile_name,'manifest_sha256':hashlib.sha256(raw).hexdigest(),
              'intent':intent.projection(),'executable':executable,'environment':dict(environment)}
        return EchoPlan(policy_plan,profile_name,raw,intent,executable,tuple(sorted(environment.items())),_hash(body))

    def preview(self,plan):
        if type(plan) is not EchoPlan:raise EchoError('echo_plan_required')
        fresh=self.make_plan(plan.policy_plan,profile_name=plan.profile_name,manifest=decode_json(plan.manifest_json),
            **decode_json(plan.intent.semantic_json),trust_fingerprint=plan.intent.trust_fingerprint,
            executable=plan.executable,environment=dict(plan.environment), operations=plan.intent.operations,
            file_descriptors=decode_json(plan.intent.descriptor_json)['file'],
            data_descriptors=decode_json(plan.intent.descriptor_json)['data'],
            http_descriptors=decode_json(plan.intent.descriptor_json)['http.request'],
            model_descriptors=decode_json(plan.intent.descriptor_json)['model.generate'])
        if fresh!=plan:raise EchoError('echo_plan_changed')
        with self._lock:
            self._purge_previews_locked()
            # Check before minting an inner token. Both ledgers stay bounded together.
            if len(self._issued)>=128:raise EchoError('echo_approval_capacity')
            inner=self.approvals.preview(plan.intent)
            self._issued[inner['token']]=(plan,inner,self.clock()+inner['expires_in'])
        return {'token':inner['token'],'fingerprint':plan.fingerprint,'intent_fingerprint':plan.intent.fingerprint,
                'expires_in':inner['expires_in'],'snapshot':{'policy':_policy_projection(plan.policy_plan),
                'profile_name':plan.profile_name,'semantic':plan.intent.projection(),
                'executable':plan.executable,'environment':dict(plan.environment)},
                'process_started':False,'native_actions_performed':False,'approval_required':True}

    def _purge_previews_locked(self):
        now=self.clock()
        expired=[token for token,record in self._issued.items() if record[2]<=now]
        for token in expired:
            self._issued.pop(token)
            self.approvals.cancel_preview(token)

    def cancel_preview(self,token,fingerprint):
        """Consume only an exact unattempted Echo preview; no native/resource work."""
        with self._lock:
            self._purge_previews_locked()
            record=self._issued.pop(token,None) if type(token) is str else None
            if record is None:raise EchoError('echo_preview_unknown')
            # Even a changed fingerprint consumes this preview, never leaves a
            # dangling inner approval that could later be approved independently.
            self.approvals.cancel_preview(token)
            if record[0].fingerprint!=fingerprint:raise EchoError('echo_plan_changed')
            return {'cancelled':True,'native_actions_performed':False,'process_started':False}

    def pending_resources(self):
        with self._lock:
            return {key:tuple(name for name,value in owners.items() if value is not None)
                    for key,owners in self._pending.items()}

    def _cleanup(self,owners):
        failures=[]
        # Revoke before Job termination/pipe closure; never accept a late response.
        broker=owners.get('broker')
        if broker is not None:
            try:broker.close();owners['broker']=None
            except BaseException:failures.append('broker')
        item=owners.get('broker_adapters')
        if item is not None:
            try:item.close();owners['broker_adapters']=None
            except BaseException:failures.append('broker_adapters')
        item=owners.get('running')
        if item is not None:
            try:
                item.close()
                if getattr(item,'closed',None) is not True:
                    raise EchoError('echo_launcher_cleanup_unverified')
                owners['running']=None
            except BaseException:failures.append('running')
        item=owners.get('process_guard')
        if item is not None:
            try:
                api,handle,pid=item
                if (api.process_id(handle)!=pid or api.process_exited(handle) is not True
                        or type(api.exit_code(handle)) is not int or api.exit_code(handle)==259):
                    raise EchoError('echo_process_exit_cleanup_unverified')
                api.close(handle);owners['process_guard']=None
            except BaseException:failures.append('process_guard')
        unknown=owners.get('observation_owners',[])
        remaining=[]
        for observed in unknown:
            try:
                observed.close()
                if getattr(observed,'closed',None) is not True:
                    raise EchoError('echo_observation_cleanup_unconfirmed')
            except BaseException:
                remaining.append(observed)
        owners['observation_owners']=remaining
        if remaining:failures.append('observation_owners')
        item=owners.get('observer_owner')
        if item is not None:
            try:
                if item.cleanup_gate() is not True:
                    raise EchoError('echo_observer_cleanup_unconfirmed')
                owners['observer_owner']=None
            except BaseException:failures.append('observer_owner')
        # An outstanding impersonation thread can still reference held identities.
        # Keep its prepared path lease and admission owner until it actually exits.
        for name in ('channel','prepared'):
            if name=='prepared' and owners.get('observer_owner') is not None:
                continue
            item=owners.get(name)
            if item is None:continue
            try:
                if name=='channel':owners['io'].close(item)
                elif name=='prepared':item.lease.close()
                else:item.close()
                owners[name]=None
            except BaseException:failures.append(name)
        io=owners.get('io')
        if io is not None and io._orphaned:
            try:io.retry_orphan_cleanup()
            except BaseException:failures.append('orphaned_pipe_handles')
        if owners.get('cancel_hook_pending'):
            failures.append('cancel_hook_pending')
        return failures

    def retry_cleanup(self,call_id):
        with self._lock:owners=self._pending.get(call_id)
        if owners is None:raise EchoError('echo_cleanup_owner_unknown')
        if owners.get('active'):
            raise EchoError('echo_execution_still_active')
        failures=self._cleanup(owners)
        if failures:raise EchoError('echo_cleanup_unconfirmed')
        with self._lock:self._pending.pop(call_id,None)
        return ResourceCleanupReceipt(owners['identity'],True,True,True,True,'host_os_query')

    def execute(self,token,fingerprint,*,user_confirmed,prepared,provision_adapter,ledger,
                channel_io,native_launcher,observers,admission_lease,cancel_event,register_cancel,
                provision_lock=None, broker_adapters=None):
        # Complete host wiring must be present BEFORE any pipe/profile/ACL operation.
        if (type(prepared) is not HostPreparedTree or type(provision_adapter) is not NativeProvisionAdapter
                or type(ledger) is not ProvisionLedger or type(channel_io) is not NativeBoundedIO
                or type(native_launcher) is not launcher.WindowsLauncher or type(observers) is not EchoObservers
                or type(admission_lease) is not SchedulerLease or not callable(register_cancel)
                or not callable(getattr(cancel_event,'is_set',None))):
            raise EchoError('strict_echo_host_not_configured')
        observers.validate()
        with self._lock:
            self._purge_previews_locked()
            issued=self._issued.pop(token,None) if type(token) is str else None
            if issued is None:raise EchoError('echo_confirmation_required')
            if user_confirmed is not True or issued[0].fingerprint!=fingerprint:
                self.approvals.cancel_preview(token)
                raise EchoError('echo_confirmation_required')
        plan,inner,_expires=issued;call_id=plan.policy_plan.binding.call_id
        if broker_adapters is not None:
            from app.plugin_runtime_brokers import InvocationBrokerAdapters
            if self._echo_only or type(broker_adapters) is not InvocationBrokerAdapters:
                self.approvals.cancel_preview(token)
                raise EchoError('echo_broker_injection_forbidden')
            broker_adapters.validate_intent(plan.intent)
        elif plan.intent.operations:
            self.approvals.cancel_preview(token)
            raise EchoError('invocation_broker_adapters_required')
        if (prepared.plan!=plan.policy_plan or provision_adapter.prepared is not prepared
                or ledger.adapter is not provision_adapter or call_id!=admission_lease.call_id
                or plan.policy_plan.binding.plugin_id!=admission_lease.plugin_id):
            raise EchoError('echo_prepared_or_admission_binding')
        scheduler=admission_lease._scheduler
        with scheduler._condition:
            if scheduler._running.get(call_id) is not admission_lease or admission_lease._released:
                raise EchoError('echo_admission_not_owned')
        if cancel_event.is_set():raise EchoError('echo_cancelled_before_native')
        capability=self.approvals.approve(inner['token'],plan.intent.fingerprint,user_confirmed=True)
        identity=ExecutionIdentity(plan.intent.plugin_id,plan.intent.revision,plan.intent.digest,call_id,plan.intent.trust_fingerprint)
        owners={'running':None,'channel':None,'prepared':prepared,'broker':None,'io':channel_io,'identity':identity,
                'process_guard':None,'cancel_hook_pending':False,'observer_owner':observers.resource_owner,'observation_owners':[],'active':True,
                'broker_adapters':broker_adapters}
        outcome=None;failure=None;channel=None;session=None;borrowed_process=None;launch_plan=None
        # Register every resource owner before entering any resource-creating stage.
        # A second BaseException during cleanup must not make ownership disappear.
        with self._lock:
            if call_id in self._pending:raise EchoError('echo_cleanup_already_pending')
            self._pending[call_id]=owners
        try:
            creation=plan_channel_creation(call_id)
            channel=channel_io.create_channel(creation,user_confirmed=True,fingerprint=creation.fingerprint)
            owners['channel']=channel
            physical=_bind_channel_plan(plan.policy_plan,channel_io,channel)
            if _policy_projection(physical)!=_policy_projection(plan.policy_plan):raise EchoError('echo_mechanical_binding_changed_semantics')
            # This is a derived physical binding of an approved semantic intent, not
            # a new human confirmation of unpredictable OS handle numbers.
            physical_prepared=replace(prepared,plan=physical)
            provision_adapter.prepared=physical_prepared;provision_adapter.plan=physical
            owners['prepared']=physical_prepared
            with provision_lock if provision_lock is not None else nullcontext():
                p=ledger.preview(physical,profile_name=plan.profile_name,expected_revision=ledger.snapshot()['revision'])
                provision_adapter.authorize_writes(intent=p['snapshot'],fingerprint=p['fingerprint'],user_confirmed=True)
                provisioned=ledger.commit(p['token'],p['fingerprint'],user_confirmed=True)
            launch_plan=launcher.plan_launch(physical,plan.executable,(plan.executable,'--plugin-worker'),dict(plan.environment))
            def host_verify(current):
                if cancel_event.is_set():raise EchoError('echo_cancelled')
                proof=ledger.readonly_verify(provisioned['id'],physical,profile_name=plan.profile_name)
                return observers.host_evidence(current,proof,physical_prepared,admission_lease)
            current_binding=[None]
            broker=PluginProxyBroker(live_binding_provider=lambda binding:binding if not cancel_event.is_set()
                    and current_binding[0]==binding and observers.live_intent(plan.intent)==plan.intent else None,
                adapters={} if broker_adapters is None else broker_adapters.adapters(), clock=self.clock,
                object_descriptor_provider=None if broker_adapters is None else broker_adapters.object_descriptor_provider,
                runtime_deadline_provider=None if broker_adapters is None else broker_adapters.runtime_deadline)
            owners['broker']=broker
            def created(child):
                nonlocal borrowed_process
                borrowed_process=child.process
                api=channel_io.api
                if api.process_id(child.process)!=child.pid:raise EchoError('echo_child_pid_changed')
                duplicate=api.duplicate(child.process,inheritable=False)
                owners['process_guard']=(api,duplicate,child.pid)
                if api.process_id(duplicate)!=child.pid:raise EchoError('echo_child_pid_changed')
                api.verify_noninheritable(duplicate)
                # Even failure while closing the host's child-pipe copies leaves
                # an independent held process identity to prove actual termination.
                channel_io.mark_child_spawned(channel)
            bound={}
            def suspended(child,observation,_evidence,job_control):
                nonlocal session
                def stop():
                    owners['cancel_hook_pending']=True
                    try:
                        cancel_event.set()
                        if session is not None:broker.revoke_session(session)
                        job_control.cancel()
                    finally:owners['cancel_hook_pending']=False
                register_cancel(stop)
                if cancel_event.is_set():stop();raise EchoError('echo_cancelled_before_resume')
                receipt=launcher.AttestedChild(observation.pid,observation.appcontainer_sid,physical.fingerprint,
                    physical.runtime_manifest_sha256,observation.capabilities,observation.network_capabilities,
                    tuple((h.purpose,v) for h,v in zip(physical.inherited_handles,observation.inherited_handles)))
                evidence=observers.verify_child(receipt,child.process,launch_plan)
                if type(evidence) is not VerifiedIntentChild or evidence.child!=receipt:raise EchoError('echo_child_observation_missing')
                evidence.validate()
                actual=BrokerSession(plan.intent.plugin_id,plan.intent.revision,plan.intent.digest,call_id,
                                     evidence.child.pid,evidence.child.appcontainer_sid,plan.intent.trust_fingerprint)
                current_binding[0]=actual
                if broker_adapters is not None:
                    # Only an independently verified held child may claim once. No
                    # user wait, model submission or runtime budget starts here.
                    def adapter_live(binding):
                        if (cancel_event.is_set() or current_binding[0] != binding
                                or observers.live_intent(plan.intent) != plan.intent):
                            return None
                        if 'channel' in bound:
                            if observers.channel_evidence(bound['channel'], child.process, launch_plan) != bound['channel'].attestation:
                                return None
                        elif observers.verify_child(receipt, child.process, launch_plan) != evidence:
                            return None
                        return binding
                    broker_adapters.claim_child(evidence, actual, cancel_event, live_binding=adapter_live)
                bound.update(broker.bind_approved_intent(self.approvals,capability,receipt,
                    verify_child=lambda value:observers.verify_child(value,child.process,launch_plan),
                    live_intent_provider=observers.live_intent))
                session=bound['session']
                channel_io.bind_process(channel,child.process,child.pid,cancel_job=job_control.cancel)
                semantic=decode_json(plan.intent.semantic_json);manifest=decode_json(plan.manifest_json)
                init={'v':1,'type':'init','plugin_id':actual.plugin_id,'revision':actual.revision,'digest':actual.digest,
                    'call_id':call_id,'request_id':'echo_'+call_id,'package_root':physical.paths.code,
                    'entrypoint':manifest['entrypoint'],'files':manifest['files'],'manifest':manifest,
                    'tool':semantic['tool'],'args':semantic['args'],'config':semantic['config'],'state':semantic['state'],
                    'session':session,'grants':bound['grants']}
                encode_frame(init)  # Bound final init before package execution/IO.
                attestation=HostChannelAttestation(actual,actual.pid,actual.sandbox_sid,'job_'+call_id,
                    channel.child_ends,physical.runtime_manifest_sha256,bound_init_fingerprint(init),
                    source='host_os_query',is_appcontainer=evidence.is_appcontainer,capabilities=receipt.capabilities,
                    job_limits_verified=evidence.job_verified,handle_whitelist_verified=evidence.handles_verified,
                    network_isolation_verified=evidence.network_verified)
                bound.update({'init':init,'channel':TrustedChannel(actual,session,channel.result_read,channel.request_write,channel.diagnostics_read,attestation)})
            running=native_launcher.execute(launch_plan,user_confirmed=True,fingerprint=launch_plan.fingerprint,
                host_verifier=host_verify,on_child_created=created,on_verified_suspended=suspended)
            owners['running']=running
            deadline=running.runtime_deadline
            if type(deadline) not in (float,int) or not math.isfinite(deadline) or not 0<deadline-self.clock()<=10:
                raise EchoError('echo_resume_deadline_unverified')
            if broker_adapters is not None:
                # Pending model registration happens after Resume using THIS exact
                # deadline; it never blocks a suspended child for human approval.
                broker_adapters.started(deadline)
            trusted=bound['channel']
            def verify_channel(current):return observers.channel_evidence(current,borrowed_process,launch_plan)
            transport=PluginTransport(trusted,host_verifier=verify_channel,clock=self.clock)
            outcome=transport.run(bound['init'],broker,cancel_event=cancel_event,close_job=running.close,
                                  deadline=deadline,reader_adapter=channel_io)
            if not outcome['ok']:failure=outcome['error']['code']
        except BaseException as error:
            # Launcher may attach unclosed native ownership even to cancellation or
            # KeyboardInterrupt. Preserve it before cleanup; never lose it on unwind.
            code=getattr(error,'code',None)
            failure=code if type(code) is str and code else 'strict_echo_refused'
            # Launcher preserves incomplete resources on the exception for retry.
            if getattr(error,'sandbox_cleanup',None) is not None:owners['running']=error.sandbox_cleanup
            seen=set();chain=[error]
            while chain:
                current=chain.pop()
                if id(current) in seen:continue
                seen.add(id(current))
                observed=getattr(current,'observation_cleanup',None)
                if observed is not None and all(observed is not prior for prior in owners['observation_owners']):
                    owners['observation_owners'].append(observed)
                chain.extend(item for item in (current.__cause__,current.__context__) if item is not None)
        finally:
            try:self.approvals.revoke(capability)
            except BaseException:pass  # Already consumed is normal after successful binding.
            cleanup_faults=self._cleanup(owners)
            owners['active']=False
            if not cleanup_faults:
                with self._lock:self._pending.pop(call_id,None)
        if cleanup_faults:
            return {'ok':False,'code':'echo_cleanup_unconfirmed','cleanup_pending':True,'retry':False,'result':None,'cleanup_receipt':None}
        receipt=ResourceCleanupReceipt(identity,True,True,True,True,'host_os_query')
        return {'ok':failure is None,'code':failure or 'completed','cleanup_pending':False,'retry':False,
                'result':outcome.get('result') if failure is None and outcome else None,'cleanup_receipt':receipt}


class StrictInvocationHost(StrictEchoHost):
    """Normal SDK invocation using the same strict native gates, not an Echo-only tool.

    Only a trusted host may construct its full immutable plan from a consumed exact
    package approval. Tool declaration, args/config/state and OS policy still bind;
    there is no MCP mode or permission to mint proxy grants from package JSON.
    """
    _echo_only = False
