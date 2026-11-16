"""Owned native observation bundle for one explicitly approved synthetic Echo.

Not a PluginManager runtime or a default provider. Construction/import has no OS IO;
``configure`` is only for a host controller after its exact OS plan is confirmed.
The existing launcher still owns the child/Job. This narrow API subclass adds a real
child-token path gate immediately before Resume, never a pre-creation fake proof.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import threading

from app.plugin_windows_launcher import (NativeWindowsApi, TrustedHostEvidence,
    WindowsSandboxError, Child, JobOwner, RunningSandbox)
from app.plugin_windows_observer import WindowsObserver
from app.plugin_token_access import (NativeTokenAccessApi, OwnedChildReceipt,
    TokenPathAccessVerifier)
from app.plugin_proxy import VerifiedIntentChild, encode_json
from app.plugin_strict_echo import EchoObservers
from app.plugin_sandbox_policy import semantic_plan_projection


def _hash(value):
    return hashlib.sha256(encode_json(value)).hexdigest()


class EchoNativeEvidenceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class OwnedEchoNativeApi(NativeWindowsApi):
    """Create only via the explicitly approved controller, not an RPC factory.

    Each borrowed raw handle is tracked for its exact ownership lifetime. There is
    no public raw-Job reobserve callback and no trusting a previous wire attestation.
    A query error is a refusal; cleanup never resets this gate to success.
    """
    def __init__(self, *, access_request_provider, prepared, network_observer,
                 identity_api=None, path_verifier=None):
        super().__init__()
        if (not callable(access_request_provider) or type(network_observer) is not WindowsObserver
                or getattr(prepared, 'lease', None) is None):
            raise EchoNativeEvidenceError('explicit_echo_native_capabilities_required')
        self._request_provider = access_request_provider
        self._prepared = prepared
        self._network = network_observer
        self._identity = identity_api if identity_api is not None else NativeTokenAccessApi()
        self._path_verifier = path_verifier if path_verifier is not None else TokenPathAccessVerifier()
        self._owned_lock = threading.RLock()
        self._echo_child = None
        self._echo_job = None
        self._echo_job_owner = None
        self._echo_plan = None
        self._creation = None
        self._initial_native = None
        self._resumed = False
        self._access_attempted = False
        self._access_ready = False
        self._access_inflight = False
        self.access_result = None
        self.probe_cleanup = None
        self._probe_process_handle = None
        self._probe_pipe_api = None
        self.audit = []

    def create_suspended(self, plan):
        with self._owned_lock:
            if self._echo_child is not None or self._echo_plan is not None:
                raise EchoNativeEvidenceError('echo_native_single_attempt_only')
            if semantic_plan_projection(plan.policy_plan) != semantic_plan_projection(self._prepared.plan):
                raise EchoNativeEvidenceError('echo_native_plan_changed')
            self._echo_plan = plan  # An attempted native create consumes this API.
            child = super().create_suspended(plan)
            self._echo_child = child
            try:
                self._creation = self._identity.process_identity(child.process)
                if self._creation[0] != child.pid:
                    raise EchoNativeEvidenceError('echo_native_creation_identity_changed')
                self.audit.append({'event': 'created_suspended', 'pid': child.pid,
                                   'creation_time': self._creation[1]})
                return child
            except BaseException as error:
                # execute() has not received the child yet. Preserve this early
                # owner on failure rather than leaking a suspended process.
                cleanup = RunningSandbox(self, child, 0, None, require_process_exit=True)
                try:
                    self.terminate(child.process)
                    cleanup.close()
                except BaseException:
                    pass
                if not cleanup.closed:
                    error.sandbox_cleanup = cleanup
                raise

    def create_job(self):
        with self._owned_lock:
            if self._echo_job is not None:
                raise EchoNativeEvidenceError('echo_job_single_attempt_only')
            self._echo_job = super().create_job()
            return self._echo_job

    def observe(self, child, job, plan):
        with self._owned_lock:
            if (self._echo_child is not child or self._echo_job != job or self._echo_plan != plan
                    or self._resumed or self._identity.process_identity(child.process) != self._creation):
                raise EchoNativeEvidenceError('echo_native_owner_changed')
            native = super().observe(child, job, plan)
            self._initial_native = dict(native)
            self.audit.append({'event': 'native_suspended_observed', 'pid': child.pid,
                'appcontainer_sid': native['appcontainer_sid'], 'is_appcontainer': native['is_appcontainer'],
                'capabilities': native['capabilities'], 'job': asdict(native['job']),
                'job_membership_verified': native['job_membership_verified'],
                'handle_whitelist_verified': native['handle_whitelist_verified'],
                'inherited_handles': native['inherited_handles'], 'suspended': native['suspended']})
            return native

    def _owned_process(self, process, plan):
        child = self._echo_child
        if (child is None or child.process != process or self._echo_plan != plan
                or self._echo_job is None or self._identity.process_identity(process) != self._creation):
            raise EchoNativeEvidenceError('echo_native_owner_changed')
        return child

    def _ready_access(self):
        result=self.access_result
        return (self._access_ready is True and result is not None
                and result.evidence.read_access_verified is True and result.evidence.cleanup_verified is True
                and not result.owner.thread.is_alive() and result.owner.done.is_set()
                and result.owner.reverted is True and not result.owner.handles and not result.owner.cleanup_fault)

    def arm_watchdog(self, job, seconds):
        # Borrow exactly the launcher's existing owner, never another Job handle.
        self._echo_job_owner=job
        return super().arm_watchdog(job,seconds)

    def prepare_child_access(self):
        with self._owned_lock:
            if self._access_inflight:
                raise EchoNativeEvidenceError('echo_child_access_inflight')
            self._access_inflight=True
        try:
            return self._prepare_child_access()
        finally:
            with self._owned_lock:
                self._access_inflight=False

    def _prepare_child_access(self):
        # Long provider/impersonation work holds neither JobOwner.lock nor the
        # native-state lock. TerminateJob and kill-on-close remain independently usable.
        with self._owned_lock:
            child = self._echo_child
            if self._access_attempted:
                if not self._ready_access():
                    raise EchoNativeEvidenceError('echo_child_path_access_unverified')
                return
            if (child is None or self._resumed or self._echo_job is None or self._initial_native is None
                    or type(self._echo_job_owner) is not JobOwner):
                raise EchoNativeEvidenceError('echo_resume_owner_unverified')
            self._access_attempted = True
            physical = self._echo_plan.policy_plan
        request = self._request_provider(physical)
        # The probe can outlive the caller's bounded wait. It must never keep a
        # borrowed launcher handle whose numeric value can be closed/recycled.
        from app.plugin_native_channel import NativePipeApi
        if self._probe_pipe_api is None:
            self._probe_pipe_api=NativePipeApi()
        duplicate=self._probe_pipe_api.duplicate(child.process,inheritable=False)
        self._probe_process_handle=duplicate  # Register before any follow-up query.
        self._probe_pipe_api.verify_noninheritable(duplicate)
        if self._identity.process_identity(duplicate) != self._creation:
            raise EchoNativeEvidenceError('echo_probe_process_identity_changed')
        receipt = OwnedChildReceipt(duplicate, child.pid, self._creation[1],
                                    physical.appcontainer_sid, physical.fingerprint)
        fingerprint = request.probe_fingerprint(physical, receipt)
        try:
            result = self._path_verifier.verify(receipt, request.targets, user_confirmed=True,
                                                fingerprint=fingerprint, timeout=2.0)
        except BaseException as error:
            self.probe_cleanup=getattr(error,'probe_cleanup',None)
            raise
        self.access_result = result  # Retain unknown thread/token ownership before checks.
        evidence = result.evidence
        if (evidence.source != 'host_os_query' or evidence.read_access_verified is not True
                or evidence.cleanup_verified is not True or evidence.parent_acl_modified is not False
                or evidence.pid != child.pid or evidence.creation_time != self._creation[1]
                or evidence.sandbox_sid != physical.appcontainer_sid
                or evidence.targets_fingerprint != fingerprint
                or evidence.reason != 'verified_readonly_access_at_observation'
                or result.owner.thread.is_alive() or not result.owner.done.is_set()
                or result.owner.reverted is not True or result.owner.handles or result.owner.cleanup_fault):
            raise EchoNativeEvidenceError('echo_child_path_access_unverified')
        self._prepared.lease.revalidate()
        if self._network.network_isolation(physical.appcontainer_sid).verified is not True:
            raise EchoNativeEvidenceError('echo_loopback_state_unverified')
        # Use the same lock ordering as execute(): JobOwner -> short native state.
        with self._echo_job_owner.lock, self._owned_lock:
            if (not self._echo_job_owner.handle or self._echo_job_owner.timed_out
                    or self._echo_job_owner.handle != self._echo_job):
                raise EchoNativeEvidenceError('echo_job_changed_during_probe')
            self._owned_process(child.process, self._echo_plan)
            native = super().observe(child, self._echo_job, self._echo_plan)
            if native != self._initial_native:
                raise EchoNativeEvidenceError('echo_native_changed_at_resume')
            self.audit.append({'event': 'child_path_access_verified', 'pid': child.pid,
                'creation_time': self._creation[1], 'targets': [asdict(t) for t in request.targets],
                'evidence': asdict(evidence)})
            self._access_ready = True

    def resume(self, thread):
        # No thread wait/impersonation inside the launcher's Job lock. Only the
        # prepared actual child-token result and final short native state checks.
        with self._owned_lock:
            child = self._echo_child
            if (child is None or child.thread != thread or self._resumed or not self._ready_access()):
                raise EchoNativeEvidenceError('echo_child_path_access_unverified')
            self._owned_process(child.process, self._echo_plan)
            native = super().observe(child, self._echo_job, self._echo_plan)
            if native != self._initial_native:
                raise EchoNativeEvidenceError('echo_native_changed_at_resume')
            super().resume(thread)
            self._resumed = True
            self.audit.append({'event': 'resumed', 'pid': child.pid})

    def verify_child(self, receipt, process, plan, intent_policy):
        owner=self._echo_job_owner
        if type(owner) is not JobOwner:
            raise EchoNativeEvidenceError('echo_job_owner_unverified')
        with owner.lock, self._owned_lock:
            child = self._owned_process(process, plan)
            native = self.observe(child, self._echo_job, plan)
            if (receipt.pid != child.pid or receipt.policy_fingerprint != plan.policy_plan.fingerprint
                    or receipt.runtime_sha256 != plan.policy_plan.runtime_manifest_sha256
                    or native['is_appcontainer'] is not True or native['appcontainer_sid'] != receipt.appcontainer_sid
                    or native['capabilities'] != () or native['network_capabilities'] != ()
                    or native['job'] != plan.policy_plan.job or native['job_membership_verified'] is not True
                    or native['handle_whitelist_verified'] is not True or native['suspended'] is not True
                    or native['inherited_handles'] != tuple(value for _, value in receipt.inherited_roles)):
                raise EchoNativeEvidenceError('echo_child_native_unverified')
        if not self._network.network_isolation(receipt.appcontainer_sid).verified:
            raise EchoNativeEvidenceError('echo_child_native_unverified')
        self.prepare_child_access()
        value = VerifiedIntentChild(receipt, intent_policy, process, self._creation[1],
            _hash({'process_handle': process, 'pid': child.pid, 'creation_time': self._creation[1]}),
            source='host_os_query', is_appcontainer=True, job_verified=True,
            handles_verified=True, network_verified=True)
        value.validate()
        return value

    def channel_evidence(self, channel, process, plan, channel_io):
        if not self._network.network_isolation(channel.binding.sandbox_sid).verified:
            raise EchoNativeEvidenceError('echo_channel_native_changed')
        owner=self._echo_job_owner
        if type(owner) is not JobOwner:
            raise EchoNativeEvidenceError('echo_job_owner_unverified')
        with owner.lock, self._owned_lock:
            if not owner.handle or owner.timed_out or owner.handle != self._echo_job:
                raise EchoNativeEvidenceError('echo_channel_job_changed')
            child = self._owned_process(process, plan)
            state = channel_io._bound(channel)  # Requeries held endpoints/process identity.
            if (not self._resumed or self.access_result is None
                    or self.access_result.evidence.read_access_verified is not True
                    or self._initial_native is None or self._initial_native['handle_whitelist_verified'] is not True
                    or state.pid != child.pid):
                raise EchoNativeEvidenceError('echo_channel_native_unverified')
            # The exact creation whitelist was checked while suspended. Once Python
            # runs it opens runtime files, so its live handle table is NOT three
            # pipes. Recheck the still-owned Job/token instead of falsifying that.
            import ctypes as C
            from ctypes import wintypes as W
            from app.plugin_windows_launcher import EXTENDED_LIMITS, CPU_LIMITS, JobPlan
            member = W.BOOL()
            self._check(self.InJob(process, self._echo_job, C.byref(member)), 'IsProcessInJob')
            info, cpu, needed = EXTENDED_LIMITS(), CPU_LIMITS(), W.DWORD()
            self._check(self.QueryJob(self._echo_job, 9, C.byref(info), C.sizeof(info), C.byref(needed)), 'QueryJob limits')
            self._check(self.QueryJob(self._echo_job, 15, C.byref(cpu), C.sizeof(cpu), C.byref(needed)), 'QueryJob CPU')
            actual_job = JobPlan(info.BasicLimitInformation.LimitFlags, info.ProcessMemoryLimit,
                info.JobMemoryLimit, info.BasicLimitInformation.ActiveProcessLimit,
                cpu.ControlFlags, cpu.CpuRate, plan.policy_plan.job.wall_seconds)
            identity = self._network.appcontainer(process, expected_pid=child.pid,
                                                  expected_sid=channel.binding.sandbox_sid)
            if member.value != 1 or actual_job != plan.policy_plan.job or not identity.verified:
                raise EchoNativeEvidenceError('echo_channel_native_changed')
            return replace(channel.attestation, source=identity.source, is_appcontainer=identity.is_appcontainer,
                capabilities=identity.capabilities, job_limits_verified=True,
                handle_whitelist_verified=self._initial_native['handle_whitelist_verified'],
                network_isolation_verified=True)

    def close(self, handle):
        with self._owned_lock:
            result = super().close(handle)
            if handle == self._echo_job:
                self._echo_job = None
            return result


class NativeEchoObserverBundle:
    """Trusted in-process composition only; no browser-provided evidence fields."""
    def __init__(self, *, plan, prepared, ledger, native_api, channel_io, scheduler_lease,
                 runtime_revalidator, live_intent_provider, network_observer):
        if (type(native_api) is not OwnedEchoNativeApi or type(network_observer) is not WindowsObserver
                or native_api._network is not network_observer
                or native_api._prepared.lease is not prepared.lease
                or not callable(runtime_revalidator) or not callable(live_intent_provider)):
            raise EchoNativeEvidenceError('native_echo_bundle_required')
        self.plan, self.prepared, self.ledger, self.native_api = plan, prepared, ledger, native_api
        self.channel_io, self.lease = channel_io, scheduler_lease
        self.runtime_revalidator, self.live_intent_provider = runtime_revalidator, live_intent_provider
        self.network = network_observer

    def host_evidence(self, launch, proof, prepared, lease):
        self.prepared.lease.revalidate()
        if (semantic_plan_projection(launch.policy_plan) != semantic_plan_projection(self.plan.policy_plan)
                or prepared.lease is not self.prepared.lease or lease is not self.lease
                or proof.get('acl_verified') is not True or proof.get('profile_ledger_verified') is not True
                or proof.get('launch_authorized') is not False or self.runtime_revalidator() is not True):
            raise EchoNativeEvidenceError('echo_host_binding_changed')
        scheduler = lease._scheduler
        with scheduler._condition:
            if scheduler._running.get(lease.call_id) is not lease or lease._released:
                raise EchoNativeEvidenceError('echo_shared_lease_changed')
        request=self.native_api._request_provider(launch.policy_plan)
        if proof.get('child_access') != request.snapshot():
            raise EchoNativeEvidenceError('echo_pending_child_access_changed')
        if not self.network.network_isolation(launch.policy_plan.appcontainer_sid).verified:
            raise EchoNativeEvidenceError('echo_loopback_state_unverified')
        # ACL/inventories/profile/owned scratch/shared lease are actual observations.
        # Child-token traversal is still explicitly pending and enforced by resume().
        return TrustedHostEvidence(launch.fingerprint, True, True, True, True, True, True, True)

    def cleanup_gate(self):
        # Never close a probe duplicate or release held paths before its result /
        # interrupted owner has been published, including provider/revalidate work.
        with self.native_api._owned_lock:
            if getattr(self.native_api,'_access_inflight',False):
                return False
        result=self.native_api.access_result
        owners=[result.owner] if result is not None else []
        interrupted=getattr(self.native_api,'probe_cleanup',None)
        if interrupted is not None and interrupted not in owners:
            owners.append(interrupted)
        for owner in owners:
            if owner.thread.is_alive() or not owner.done.is_set():
                return False
            if owner.handles and not owner.retry_handle_cleanup():
                return False
            if not owner.reverted or owner.handles:
                return False
        handle=getattr(self.native_api,'_probe_process_handle',None)
        if handle is not None:
            self.native_api._probe_pipe_api.close(handle)
            self.native_api._probe_process_handle=None
        api=self.network._api
        provider=getattr(api,'_loopback_provider',None) if api is not None else None
        pending=getattr(provider,'pending_cleanup',None) if provider is not None else None
        if pending is not None:
            pending.close()
            if pending.closed is not True:
                return False
        return True

    def observers(self):
        return EchoObservers(self.host_evidence,
            lambda child, process, launch: self.native_api.verify_child(child, process, launch, self.plan.intent.policy),
            self.live_intent_provider,
            lambda channel, process, launch: self.native_api.channel_evidence(channel, process, launch, self.channel_io),
            resource_owner=self)
