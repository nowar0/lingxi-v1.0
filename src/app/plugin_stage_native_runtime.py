"""Actual migration-only native session over the dedicated frozen worker.

All native creation is downstream of the service's consumed, identity-owned A
capability and its existing shared scheduler lease. No ordinary-process fallback,
new scheduler, execution-time root generation or browser evidence is accepted.
"""
from __future__ import annotations
from dataclasses import asdict,dataclass,replace
import hashlib
import json
import os
import secrets
import threading
from types import SimpleNamespace

from app.plugin_runtime_preparation import (PreparationOwner,read_portable_runtime,read_code_inventory,
    observe_source,prepare_tree,canonical_bytes,digest,WORKER_EXE)
from app.plugin_sandbox_policy import (CallBinding,SandboxPaths,SandboxPolicy,LaunchPlan,JobPlan,
    CREATION_FLAGS,JOB_LIMIT_FLAGS,CPU_RATE_ENABLE,CPU_RATE_HARD_CAP,_grants,
    inventory_fingerprint,plan_fingerprint,semantic_plan_projection,validate_launch_plan,InventoryEntry)
from app.plugin_native_provision import root_creation_plan,NativeProvisionAdapter
from app.plugin_sandbox_provision import ProvisionLedger
from app.plugin_native_channel import NativeBoundedIO,plan_channel_creation
from app.plugin_strict_echo import _bind_channel_plan
from app.plugin_echo_native_host import OwnedEchoNativeApi,NativeEchoObserverBundle
from app.plugin_windows_launcher import NativeWindowsApi,WindowsLauncher,plan_launch
from app.plugin_windows_observer import WindowsObserver
from app.plugin_proxy import BrokerSession,IntentSandboxPolicy
from app.plugin_transport import HostChannelAttestation,TrustedChannel
from app.plugin_wire import encode_frame,decode_json,encode_json
from app.plugin_migrations import ExecutorAttestation,PERMISSIONS

_CONFIG={}
_LOCK=threading.RLock()


class StageNativeError(ValueError):
    def __init__(self,code):super().__init__(code);self.code=code


@dataclass(frozen=True)
class StageHostAuthority:
    snapshot_json:bytes
    authorizing_fingerprint:str
    approval_capability:object
    admission_lease:object
    cancel_event:object
    validate_current:object
    register_cancel:object


def configure_stage_runtime(runtime,*,shared_scheduler,authority_provider):
    if not callable(authority_provider):raise TypeError('trusted service authority required')
    with _LOCK:_CONFIG[id(runtime)]=(runtime,shared_scheduler,authority_provider)


def _configuration(runtime):
    with _LOCK:value=_CONFIG.get(id(runtime))
    if value is None or value[0] is not runtime:raise StageNativeError('stage_native_authority_unconfigured')
    return value


def stage_available(runtime):
    try:
        _configuration(runtime)
        return runtime.execution_evidence('ordinary') is not None
    except Exception:return False


def _plan(runtime,body,*,run_id,nonce,session):
    runtime._require_configured()
    inventory=read_portable_runtime(runtime.runtime_source)
    document=json.loads((__import__('pathlib').Path(runtime.runtime_source)/'runtime-inventory.json').read_text(encoding='utf-8'))
    if '--plugin-stage-worker' not in document.get('worker_modes',[]) or 'app.plugin_stage_worker' not in document.get('analyzed_modules',[]):
        raise StageNativeError('stage_worker_not_in_frozen_runtime')
    profile='Lingxi.Stage.'+run_id
    sid=NativeWindowsApi().derive_sid(profile)
    root=runtime.approved_parent+'\\sandbox-lab-'+run_id
    binding=CallBinding(body['plugin_id'],body['expected_revision'],body['target_code_digest'],body['call_id'])
    paths=SandboxPaths(root,root+'\\runtime',root+'\\code\\'+binding.plugin_id+'\\'+binding.digest,root+'\\scratch\\'+binding.call_id)
    files=tuple(InventoryEntry(item['path'],item['sha256'],item['size']) for item in body['candidate_code_inventory'])
    policy=SandboxPolicy.normal()
    job=JobPlan(JOB_LIMIT_FLAGS,policy.memory_bytes,policy.memory_bytes,1,CPU_RATE_ENABLE|CPU_RATE_HARD_CAP,2000,10)
    plan=LaunchPlan(policy,binding,paths,sid,(),(),files,inventory.entries,inventory.inventory_sha256,
        _grants(paths,sid),(),CREATION_FLAGS,job,'',ipc_stage='semantic')
    plan=replace(plan,fingerprint=plan_fingerprint(plan));validate_launch_plan(plan,require_physical=False)
    system_root=runtime.system_root or os.environ.get('SYSTEMROOT') or os.environ.get('SystemRoot')
    env={'SYSTEMROOT':system_root,'WINDIR':system_root,'TEMP':paths.scratch,'TMP':paths.scratch,
         'PYTHONUTF8':'1','PYTHONNOUSERSITE':'1','PYTHONIOENCODING':'utf-8'}
    executable=paths.runtime+'\\'+WORKER_EXE
    plan_launch(plan,executable,(executable,'--plugin-stage-worker'),env,semantic_only=True)
    selected={key:value for key,value in body.items() if key!='native_spec'}
    public={'schema':'lingxi.stage-native.v1','worker_mode':'--plugin-stage-worker','fallback_allowed':False,
        'call_id':body['call_id'],'run_id':run_id,'nonce':nonce,'session':session,'profile_name':profile,
        'runtime_source':runtime.runtime_source,'runtime_manifest_sha256':inventory.manifest_sha256,
        'candidate_code_root':body['candidate_code_root'],'host_snapshot_sha256':digest(selected),
        'policy':json.loads(canonical_bytes(semantic_plan_projection(plan))),'policy_fingerprint':plan.fingerprint,
        'root_creation':root_creation_plan(approved_parent=runtime.approved_parent,root=root,creation_nonce=nonce),
        'executable':executable,'environment':env,'parent_acl_modified':False}
    public['fingerprint']=digest(public)
    return public,plan,inventory


def preview_stage_spec(runtime,*,host_snapshot):
    _configuration(runtime)
    public,_,_=_plan(runtime,host_snapshot,run_id=secrets.token_hex(16),nonce=secrets.token_hex(32),session=secrets.token_urlsafe(32))
    return public


def validate_stage_spec(runtime,spec,*,request):
    if type(spec) is not dict or spec.get('schema')!='lingxi.stage-native.v1':return False
    try:
        value=dict(spec);fingerprint=value.pop('fingerprint')
        return (digest(value)==fingerprint and spec['call_id']==request.call_id
            and request.code_root==spec['candidate_code_root']
            and spec['policy']['binding']['digest']==request.plan.target_code_digest
            and spec['policy']['binding']['plugin_id']==request.plan.plugin_id
            and spec['worker_mode']==request.worker_mode and spec['fallback_allowed'] is False)
    except (KeyError,TypeError,ValueError):return False


class StageSession:
    def __init__(self,runtime,request,proxy,cancelled):
        self.runtime=runtime;self.request=request;self.proxy=proxy;self.cancelled=cancelled
        self.preparation=PreparationOwner();self.running=None;self.io=None;self.channel=None
        self.bundle=None;self.native=None;self.prepared=None;self.authority=None;self.guard=None
        self.launch=None;self.trusted=None;self._closed=False;self._fault=False;self._diagnostics=0
        self._total=0;self._cleanup_lock=threading.RLock();self._cancel_pending=False
        self.spec=json.loads(request.native_spec_json)

    def current(self):
        auth=self.authority
        if (auth is None or auth.cancel_event is not self.cancelled or self.cancelled.is_set()
                or auth.validate_current(self.request,self.proxy,auth.approval_capability,json.loads(auth.snapshot_json)) is not True):
            raise StageNativeError('stage_live_authority_changed')
        return True

    def prepare(self):
        runtime,scheduler,provider=_configuration(self.runtime)
        authority=provider(self.request,self.proxy,self.cancelled)
        if type(authority) is not StageHostAuthority or not callable(authority.validate_current) or not callable(authority.register_cancel):
            raise StageNativeError('stage_consumed_authority_required')
        self.authority=authority
        body=json.loads(authority.snapshot_json)
        if canonical_bytes(body)!=authority.snapshot_json or digest(body)!=authority.authorizing_fingerprint or body.get('native_spec')!=self.spec:
            raise StageNativeError('stage_authority_snapshot_changed')
        lease=authority.admission_lease
        with scheduler._condition:
            if (lease._scheduler is not scheduler or scheduler._running.get(self.request.call_id) is not lease
                    or lease._released or lease.call_id!=self.request.call_id or lease.plugin_id!=self.request.plan.plugin_id
                    or getattr(lease,'cancel_event',None) is not self.cancelled):
                raise StageNativeError('stage_shared_admission_changed')
        self.current()
        fresh,plan,inventory=_plan(runtime,body,run_id=self.spec['run_id'],nonce=self.spec['nonce'],session=self.spec['session'])
        if fresh!=self.spec or not validate_stage_spec(runtime,self.spec,request=self.request):
            raise StageNativeError('stage_native_spec_changed')
        code=read_code_inventory(self.request.code_root,json.loads(self.request.manifest_json),self.request.plan.target_code_digest)
        if code!=plan.code_files:raise StageNativeError('stage_registered_code_inventory_changed')
        self.preparation.retain(observe_source(inventory.root,(*inventory.entries,inventory.manifest_entry)))
        self.preparation.retain(observe_source(self.request.code_root,code))
        self.prepared,observer=prepare_tree(plan,approved_parent=runtime.approved_parent,
            creation_nonce=self.spec['nonce'],root_intent=self.spec['root_creation'],runtime_source=inventory.root,
            package_root=self.request.code_root,owner=self.preparation,authorized=self.current(),cancel_event=self.cancelled)
        adapter=NativeProvisionAdapter(self.prepared,creation_nonce=self.spec['nonce'],observation_approved=True,
            profile_folder_observation_approved=True,defer_child_access=True)
        ledger=ProvisionLedger(runtime.ledger_path,approved_parent=runtime.approved_parent,native_adapter=adapter,defer_child_access=True)
        self.io=NativeBoundedIO(clock=runtime.clock)
        create=plan_channel_creation(self.request.call_id)
        self.channel=self.io.create_channel(create,user_confirmed=self.current(),fingerprint=create.fingerprint)
        physical=_bind_channel_plan(plan,self.io,self.channel)
        physical_tree=replace(self.prepared,plan=physical);adapter.plan=physical;adapter.prepared=physical_tree
        with runtime._provision_lock:
            p=ledger.preview(physical,profile_name=self.spec['profile_name'],expected_revision=ledger.snapshot()['revision'])
            adapter.authorize_writes(intent=p['snapshot'],fingerprint=p['fingerprint'],user_confirmed=self.current())
            receipt=ledger.commit(p['token'],p['fingerprint'],user_confirmed=self.current())
        self.native=OwnedEchoNativeApi(access_request_provider=adapter.child_access_request,prepared=physical_tree,network_observer=observer)
        policy=IntentSandboxPolicy(physical.appcontainer_sid,physical.runtime_manifest_sha256,inventory_fingerprint(physical.code_files),digest(asdict(physical.paths)))
        intent=SimpleNamespace(policy_plan=physical,intent=SimpleNamespace(policy=policy))
        def revalidate_runtime():
            self.current()
            for held in self.preparation.leases:
                if not held.closed:held.revalidate()
            return read_portable_runtime(inventory.root)==inventory
        self.bundle=NativeEchoObserverBundle(plan=intent,prepared=physical_tree,ledger=ledger,native_api=self.native,
            channel_io=self.io,scheduler_lease=lease,runtime_revalidator=revalidate_runtime,
            live_intent_provider=lambda _:None,network_observer=observer)
        self.launch=plan_launch(physical,self.spec['executable'],(self.spec['executable'],'--plugin-stage-worker'),self.spec['environment'])
        def host_verify(launch):
            self.current()
            proof=ledger.readonly_verify(receipt['id'],physical,profile_name=self.spec['profile_name'])
            return self.bundle.host_evidence(launch,proof,physical_tree,lease)
        def created(child):
            duplicate=self.io.api.duplicate(child.process,inheritable=False)
            self.guard=(duplicate,child.pid)
            self.io.api.verify_noninheritable(duplicate)
            if self.io.api.process_id(duplicate)!=child.pid:raise StageNativeError('stage_process_identity_changed')
            self.io.mark_child_spawned(self.channel)
        def suspended(child,observation,evidence,control):
            def cancel():
                self._cancel_pending=True
                try:self.cancelled.set();control.cancel()
                finally:self._cancel_pending=False
            authority.register_cancel(cancel)
            self.current();self.native.prepare_child_access()
            self.io.bind_process(self.channel,child.process,child.pid,cancel_job=control.cancel)
            binding=BrokerSession(physical.binding.plugin_id,physical.binding.revision,physical.binding.digest,
                physical.binding.call_id,child.pid,physical.appcontainer_sid,digest(body['host_binding']))
            init=self.request.init(session=self.spec['session'])
            init={**init,'package_root':physical.paths.code}
            # Bind the proxy only after the launcher returns its Resume deadline;
            # pre-import init hashing does not start or extend the runtime budget.
            attestation=HostChannelAttestation(binding,child.pid,physical.appcontainer_sid,'job_'+self.request.call_id,
                self.channel.child_ends,physical.runtime_manifest_sha256,hashlib.sha256(encode_json(init)).hexdigest(),
                source='host_os_query',is_appcontainer=observation.is_appcontainer,capabilities=observation.capabilities,
                job_limits_verified=observation.job_membership_verified,handle_whitelist_verified=observation.handle_whitelist_verified,
                network_isolation_verified=observer.network_isolation(physical.appcontainer_sid).verified)
            self.trusted=TrustedChannel(binding,self.spec['session'],self.channel.result_read,self.channel.request_write,self.channel.diagnostics_read,attestation)
        self.running=WindowsLauncher(self.native,clock=runtime.clock).execute(self.launch,user_confirmed=self.current(),
            fingerprint=self.launch.fingerprint,host_verifier=host_verify,on_child_created=created,on_verified_suspended=suspended)
        return self

    def _check(self):
        self.current();deadline=self.running.runtime_deadline
        self.io._check(deadline,self.cancelled)
        actual=self.native.channel_evidence(self.trusted,self.running.child.process,self.launch,self.io)
        if actual!=self.trusted.attestation:raise StageNativeError('stage_channel_changed')
        return deadline

    def query_stage_identity(self):
        self._check()
        plan=self.request.plan
        return ExecutorAttestation(plan.fingerprint,self.trusted.binding.sandbox_sid,self.trusted.binding.pid,
            256*1024**2,2000,1,10,PERMISSIONS,(),True,True,'host_os_query')

    def _read(self,size):
        result=bytearray()
        while len(result)<size:
            deadline=self._check()
            diagnostic=self.io.read_diagnostics(self.trusted,1024,deadline=deadline,cancel=self.cancelled)
            self._diagnostics+=len(diagnostic)
            if self._diagnostics>4096:raise StageNativeError('stage_diagnostics_limit')
            data=self.io.read(self.trusted,size-len(result),deadline=deadline,cancel=self.cancelled)
            if not data:raise StageNativeError('stage_unexpected_eof')
            self._total+=len(data)
            if self._total>1024*1024*1024:raise StageNativeError('stage_wire_limit')
            result.extend(data)
        return bytes(result)

    def _write(self,value):
        raw=encode_frame(value);offset=0
        while offset<len(raw):
            deadline=self._check()
            count=self.io.write(self.trusted,raw[offset:],deadline=deadline,cancel=self.cancelled)
            if type(count) is not int or count<=0:raise StageNativeError('stage_wire_write_failed')
            offset+=count

    def run_stage(self,*,init_factory,handler):
        init=init_factory(session=self.spec['session'],deadline=self.running.runtime_deadline,cancelled=self.cancelled)
        init={**init,'package_root':self.launch.policy_plan.paths.code}
        if hashlib.sha256(encode_json(init)).hexdigest()!=self.trusted.attestation.approved_init_sha256:
            raise StageNativeError('stage_init_changed')
        self._write(init)
        import struct
        for _ in range(100001):
            size=struct.unpack('!I',self._read(4))[0]
            if not 0<size<=65536:raise StageNativeError('stage_frame_size')
            frame=decode_json(self._read(size));self._check()
            if type(frame) is not dict:raise StageNativeError('stage_frame_invalid')
            if frame.get('type')=='stage_result':
                completion=self.io.complete_exit(self.trusted,deadline=self.running.runtime_deadline,cancel=self.cancelled)
                if (completion.returncode!=0 or not completion.exit_verified or not completion.stdout_eof_verified
                        or not completion.no_trailing_bytes or not completion.diagnostics_drained):
                    raise StageNativeError('stage_exit_unconfirmed')
                self._check();return frame
            if frame.get('type')!='stage_rpc':raise StageNativeError('stage_rpc_type')
            self._check();reply=handler(frame);self._check();self._write(reply)
        raise StageNativeError('stage_request_limit')

    def close(self):
        with self._cleanup_lock:
            failures=[]
            if self.running is not None:
                try:self.running.close()
                except BaseException:failures.append('running')
            if self.guard is not None:
                handle,pid=self.guard
                try:
                    if self.io.api.process_id(handle)!=pid or not self.io.api.process_exited(handle) or self.io.api.exit_code(handle)==259:
                        raise StageNativeError('stage_exit_cleanup_unconfirmed')
                    self.io.api.close(handle);self.guard=None
                except BaseException:failures.append('process_guard')
            safe=self.bundle is None
            if self.bundle is not None:
                try:safe=self.bundle.cleanup_gate() is True
                except BaseException:safe=False
                if not safe:failures.append('observer')
            if self.channel is not None:
                try:self.io.close(self.channel)
                except BaseException:failures.append('channel')
            if self.io is not None:
                if self.io._orphaned:
                    try:self.io.retry_orphan_cleanup()
                    except BaseException:failures.append('orphaned_pipe_handles')
                if self.io._orphaned or any(not state.closed or state.owned or state.writer is not None
                        or state.launch_lock_held for state in self.io._states.values()):
                    failures.append('channel_ownership_unconfirmed')
            if safe and not failures and not self.preparation.close():failures.append('preparation')
            elif failures:failures.append('preparation_retained')
            self._closed=not failures and not self._cancel_pending
            if failures:raise StageNativeError('stage_native_cleanup_pending')

    def cleanup_confirmed(self):return self._closed is True and not self._cancel_pending


def open_stage(runtime,request,*,proxy,cancelled):
    owner=StageSession(runtime,request,proxy,cancelled)
    try:return owner.prepare()
    except BaseException as error:
        owner.preparation.absorb(error)
        if getattr(error,'sandbox_cleanup',None) is not None:owner.running=error.sandbox_cleanup
        error.stage_owner=owner
        raise
