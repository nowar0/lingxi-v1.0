"""Explicit, fail-closed Windows AppContainer adapter. Import performs NO native calls.

Not wired to plugin_runtime. ACL writes and runtime/profile installation are deliberately
not implemented here: a trusted host must independently verify the exact approval plan.
AppContainer does not promise to hide publicly readable Windows resources.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import ntpath
import os
import subprocess
import threading
import math
import time
from typing import Callable

from app.plugin_sandbox_policy import (LaunchPlan, HostLaunchObservation, JobPlan,
    SandboxPolicy, SandboxPolicyError, CREATION_FLAGS, JOB_LIMIT_FLAGS,
    canonical_local_path, inventory_fingerprint, plan_fingerprint, validate_launch_plan, verify_resume)


class WindowsSandboxError(SandboxPolicyError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ProvisionPlan:
    profile_name: str
    display_name: str
    expected_sid: str
    acl_grants: tuple
    fingerprint: str


def plan_provision(profile_name, expected_sid, acl_grants=()):
    """Pure preview. Does not derive SID, create a profile, or apply ACLs."""
    import re
    if not isinstance(profile_name, str) or not re.fullmatch(r'Lingxi\.[A-Za-z0-9_.-]{8,100}', profile_name):
        raise WindowsSandboxError('host-owned unique profile name required')
    if not isinstance(expected_sid, str) or not re.fullmatch(r'S-1-15-2-(?:[0-9]+-)*[0-9]+', expected_sid):
        raise WindowsSandboxError('explicit expected AppContainer SID required')
    grants = tuple(acl_grants)
    if any(getattr(g, 'sid', None) != expected_sid for g in grants):
        raise WindowsSandboxError('ACL preview SID differs from profile')
    body = {'profile_name':profile_name, 'display_name':profile_name,
        'expected_sid':expected_sid, 'acl_grants':[asdict(g) for g in grants]}
    return ProvisionPlan(profile_name, profile_name, expected_sid, grants, _digest(body))


@dataclass(frozen=True)
class ApprovedLaunchPlan:
    policy_plan: LaunchPlan
    executable: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    fingerprint: str


def plan_launch(plan, executable, argv, environment, *, semantic_only=False):
    """Pure command validation; physical whitelist is the compatible default.

    semantic_only=True admits ONLY a zero-handle semantic intent for preview. The
    returned value is not executable: execute always revalidates physical binding.
    """
    if type(semantic_only) is not bool:
        raise WindowsSandboxError('explicit semantic-only validation mode required')
    try:
        validate_launch_plan(plan, require_physical=not semantic_only)
    except SandboxPolicyError as error:
        raise WindowsSandboxError('invalid strict launch policy') from error
    if semantic_only and plan.ipc_stage != 'semantic':
        raise WindowsSandboxError('semantic-only preview requires a zero-handle semantic plan')
    canonical_local_path(executable)
    if not executable.casefold().startswith(plan.paths.runtime.casefold()+'\\'):
        raise WindowsSandboxError('host executable or runtime escape forbidden')
    relative = ntpath.relpath(executable, plan.paths.runtime).replace('\\', '/')
    if relative not in {e.relative_path for e in plan.runtime_files} or not executable.lower().endswith('.exe'):
        raise WindowsSandboxError('executable not approved in dedicated runtime inventory')
    inventory_fingerprint(plan.runtime_files); inventory_fingerprint(plan.code_files)
    # Recheck policy's complete frozen value, never trust a modified dataclass.
    if plan.fingerprint != plan_fingerprint(plan) or plan.runtime_manifest_sha256 != inventory_fingerprint(plan.runtime_files):
        raise WindowsSandboxError('policy fingerprint mismatch')
    if type(argv) is not tuple or not argv or argv[0] != executable or len(argv)>32:
        raise WindowsSandboxError('exact immutable argv including executable required')
    if any(not isinstance(v,str) or '\x00' in v or len(v)>8192 for v in argv) or sum(map(len,argv))>24000:
        raise WindowsSandboxError('invalid command arguments')
    # The runtime must be a separately packaged isolated worker, never the unrestricted host.
    if len(argv)<2 or argv[1] not in ('--plugin-worker','--plugin-stage-worker','--package-mcp-worker','--package-mcp-server'):
        raise WindowsSandboxError('only dedicated worker command modes are permitted')
    if plan.policy.mode == 'stdio_mcp':
        expected = (executable, '--package-mcp-server')
        if not semantic_only:
            handles = {h.purpose: h.value for h in plan.inherited_handles}
            expected += (str(handles['proxy_request_read']), str(handles['proxy_result_write']))
        if argv != expected:
            raise WindowsSandboxError('MCP requires exact single-server entry and two approved proxy handles')
    elif argv not in ((executable,'--plugin-worker'),(executable,'--plugin-stage-worker')):
        raise WindowsSandboxError('normal policy requires an exact ordinary or migration worker entry')
    allowed={'SYSTEMROOT','WINDIR','TEMP','TMP','PYTHONIOENCODING','PYTHONUTF8','PYTHONNOUSERSITE'}
    if not isinstance(environment,dict) or set(environment)-allowed:
        raise WindowsSandboxError('environment contains non-allowlisted variables')
    env={str(k):v for k,v in environment.items()}
    if any(not isinstance(v,str) or '\x00' in v or len(v)>4096 for v in env.values()):
        raise WindowsSandboxError('invalid environment')
    if env.get('TEMP')!=plan.paths.scratch or env.get('TMP')!=plan.paths.scratch:
        raise WindowsSandboxError('TEMP/TMP must bind unique approved scratch')
    for name in ('SYSTEMROOT','WINDIR'):
        canonical_local_path(env.get(name,''))
    if env['SYSTEMROOT']!=env['WINDIR'] or env.get('PYTHONNOUSERSITE')!='1' or env.get('PYTHONUTF8')!='1':
        raise WindowsSandboxError('explicit isolated interpreter environment required')
    pairs=tuple(sorted(env.items()))
    digest=_digest({'policy':asdict(plan),'executable':executable,'argv':argv,'environment':pairs})
    return ApprovedLaunchPlan(plan,executable,argv,pairs,digest)


@dataclass(frozen=True)
class TrustedHostEvidence:
    """Produced by host-owned observer, never accepted from plugin JSON/RPC."""
    fingerprint: str
    acl_verified: bool = False
    inventories_reverified: bool = False
    runtime_release_verified: bool = False
    profile_ledger_verified: bool = False
    scratch_prepared_verified: bool = False
    global_lease_verified: bool = False
    network_isolation_verified: bool = False  # Includes host-checked loopback exemptions, not just capabilities.


@dataclass
class Child:
    process: int
    thread: int
    pid: int


@dataclass(frozen=True)
class SuspendedChild:
    """Borrowed callback view; handles remain owned by the launcher, never close them."""
    process: int
    thread: int
    pid: int


class JobOwner:
    """One owner/lock for the kill-on-close handle shared by timeout and cleanup.

    The native handle is never duplicated. A successful close clears it while the
    lock is held, preventing a concurrent timer from using a recycled handle.
    Failed closes retain ownership for an explicit retry and record safe faults.
    """
    def __init__(self, api, handle):
        self.api,self._handle=api,handle
        self.lock=threading.RLock()
        self.timed_out=False
        self.termination_verified=False
        self.fallback_close_succeeded=False
        self.timeout_failure=False
        self.cleanup_fault=False
        self.faults=[]
    @property
    def handle(self):
        with self.lock:return self._handle
    def snapshot(self):
        with self.lock:
            return {'job_handle_owned':bool(self._handle),'timed_out':self.timed_out,
                'termination_verified':self.termination_verified,
                'fallback_close_succeeded':self.fallback_close_succeeded,
                'timeout_failure':self.timeout_failure,'cleanup_fault':self.cleanup_fault,
                'faults':tuple(self.faults)}
    def _close_locked(self):
        if not self._handle:return
        try:
            if self.api.close(self._handle) is False:
                raise WindowsSandboxError('CloseHandle returned failure')
        except BaseException:
            self.cleanup_fault=True
            self.faults.append('job_close_failed')
            raise
        self._handle=0
        self.cleanup_fault=False
    def close(self):
        with self.lock:self._close_locked()
    def expire(self):
        """Attempt checked termination, then kill-on-close fallback on any failure."""
        with self.lock:
            if not self._handle:return
            self.timed_out=True
            try:
                if self.api.terminate_job(self._handle) is False:
                    raise WindowsSandboxError('TerminateJobObject returned failure')
                self.termination_verified=True
            except BaseException:
                self.timeout_failure=True
                self.faults.append('timeout_termination_failed')
                try:
                    self._close_locked()
                    self.fallback_close_succeeded=True
                except BaseException:
                    # Timer exceptions alone would not stop the process. Retain
                    # ownership, record failure and expose an explicit retry path.
                    self.faults.append('timeout_fallback_close_failed')


class JobCancellation:
    """Borrowed host callback control, deliberately without a raw Job handle.

    Cancellation is irreversible for this launch. execute() rechecks the real
    owner/watchdog after callbacks, so cancelling here can never permit resume.
    """
    __slots__ = ('__owner',)
    def __init__(self, owner):
        self.__owner=owner
    def cancel(self):
        self.__owner.expire()
    def snapshot(self):
        return self.__owner.snapshot()


@dataclass(frozen=True)
class AttestedChild:
    """Immutable host receipt, NOT standalone OS proof or package-parsed JSON.

    PID/SID/capabilities/handle values come from the checked native observation;
    role names and runtime inventory SHA bind that observation to the approved
    host plan. The caller must retain the actual process/channel ownership.
    """
    pid: int
    appcontainer_sid: str
    policy_fingerprint: str
    runtime_sha256: str
    capabilities: tuple[str, ...]
    network_capabilities: tuple[str, ...]
    inherited_roles: tuple[tuple[str, int], ...]


class JobWatchdog:
    """A timer that owns no independent native handle and cannot outlive ownership."""
    def __init__(self, owner, seconds, *, timer_factory=None, start_immediately=True,
                 clock=time.monotonic):
        if type(owner) is not JobOwner or not callable(clock):
            raise WindowsSandboxError('shared JobOwner and monotonic clock required for watchdog')
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds <= 0:
            raise WindowsSandboxError('bounded positive watchdog budget required')
        self.owner,self.seconds,self.clock=owner,seconds,clock
        self._armed=True
        self._started=False
        self._started_at=None
        self._deadline=None
        self.timer=(timer_factory or threading.Timer)(seconds,self.expire)
        self.timer.daemon=True
        if start_immediately:self.start()
    @property
    def prepared(self):return True
    @property
    def started(self):
        with self.owner.lock:return self._started
    @property
    def started_at(self):
        with self.owner.lock:return self._started_at
    @property
    def deadline(self):
        with self.owner.lock:return self._deadline
    def start(self):
        """Start once at the host's Resume boundary, never reset a running budget."""
        with self.owner.lock:
            if self._started or not self.armed:
                raise WindowsSandboxError('watchdog cannot start twice or after cancellation')
            self._started_at=self.clock()
            self._deadline=self._started_at+self.seconds
            self._started=True
            try:self.timer.start()
            except BaseException:
                self._armed=False
                raise
    def disarm(self):
        """Non-waiting owner-lock operation; join is performed only by cancel outside it."""
        with self.owner.lock:self._armed=False
    @property
    def armed(self):
        with self.owner.lock:return self._armed and bool(self.owner._handle) and not self.owner.timed_out
    @property
    def status(self):
        with self.owner.lock:
            return {**self.owner.snapshot(),'prepared':True,'started':self._started,
                    'started_at':self._started_at,'deadline':self._deadline}
    def expire(self):
        with self.owner.lock:
            if not self._armed or not self._started:return
            self._armed=False
            self.owner.expire()
    def cancel(self):
        # Never join while holding the owner lock: the timer may be waiting for
        # that same lock. A timer cancelling itself must not join its own thread.
        self.disarm()
        failures=[]
        try:self.timer.cancel()
        except BaseException as error:failures.append(error)
        if self._started and self.timer is not threading.current_thread():
            try:self.timer.join()
            except BaseException as error:failures.append(error)
        if failures:
            raise WindowsSandboxError('watchdog cancellation incomplete') from failures[0]


class RunningSandbox:
    def __init__(self, api, child, job, watchdog, *, host_observation=None, attested_child=None,
                 setup_watchdog=None, runtime_started_at=None, runtime_deadline=None,
                 require_process_exit=False):
        self.api,self.child,self.watchdog=api,child,watchdog
        self.setup_watchdog=setup_watchdog
        self._runtime_prepared=getattr(watchdog,'prepared',False) is True
        self._runtime_started_at=runtime_started_at
        self._runtime_deadline=runtime_deadline
        self._require_process_exit=require_process_exit
        self._process_exit_verified=False
        self._host_observation=host_observation
        self._attested_child=attested_child
        self.job_owner=job if type(job) is JobOwner else JobOwner(api,job)
        self.closed=False
        self._cleanup_faults=[]
        self._close_lock=threading.RLock()
    @property
    def host_observation(self):
        """Last checked suspended observation, not a claim of current process state."""
        return self._host_observation
    @property
    def attested_child(self):return self._attested_child
    @property
    def runtime_started(self):return self._runtime_started_at is not None
    @property
    def runtime_prepared(self):return self._runtime_prepared
    @property
    def runtime_started_at(self):return self._runtime_started_at
    @property
    def runtime_deadline(self):return self._runtime_deadline
    @property
    def job(self):return self.job_owner.handle
    @property
    def status(self):
        with self._close_lock:
            return {**self.job_owner.snapshot(),'closed':self.closed,
                'owned_process':bool(self.child.process),'owned_thread':bool(self.child.thread),
                'process_exit_verified':self._process_exit_verified,
                'runtime_prepared':self.runtime_prepared,
                'runtime_started':self.runtime_started,'runtime_started_at':self.runtime_started_at,
                'runtime_deadline':self.runtime_deadline,'setup_watchdog_owned':self.setup_watchdog is not None,
                'cleanup_faults':tuple(self._cleanup_faults)}
    def close(self):
        with self._close_lock:
            if self.closed:return
            failures=[]
            for field in ('setup_watchdog','watchdog'):
                value=getattr(self,field)
                if value is not None:
                    try:value.cancel();setattr(self,field,None)
                    except BaseException as error:
                        failures.append(error);self._cleanup_faults.append(field+'_cancel_failed')
            # Always try all owned handles even if an earlier cleanup failed.
            # Keep failed handles for a later explicit retry, not a false closed state.
            try:self.job_owner.close()
            except BaseException as error:failures.append(error)
            for field in ('thread','process'):
                handle=getattr(self.child,field)
                if handle:
                    try:
                        if field=='process' and self._require_process_exit:
                            exited=getattr(self.api,'process_exited',None)
                            exit_code=getattr(self.api,'exit_code',None)
                            if not callable(exited) or not callable(exit_code) or exited(handle) is not True:
                                raise WindowsSandboxError('process termination/exit not yet verified; process owner retained')
                            code=exit_code(handle)
                            if type(code) is not int or code==259:
                                raise WindowsSandboxError('process exit code unverified; process owner retained')
                            self._process_exit_verified=True
                        if self.api.close(handle) is False:
                            raise WindowsSandboxError('CloseHandle returned failure')
                        setattr(self.child,field,0)
                    except BaseException as error:
                        failures.append(error);self._cleanup_faults.append(field+'_close_failed')
            self.closed=self.watchdog is None and self.setup_watchdog is None and not self.job and not self.child.process and not self.child.thread
            if failures:
                raise WindowsSandboxError('sandbox cleanup incomplete; explicit host review/retry required') from failures[0]
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class WindowsLauncher:
    def __init__(self, api=None, *, clock=time.monotonic):
        if not callable(clock):raise WindowsSandboxError('monotonic clock required')
        self.clock=clock
        self._api=api
        self._used=set()
        self._lock=threading.RLock()
    @property
    def api(self):
        if self._api is None:self._api=NativeWindowsApi()
        return self._api
    def provision(self, plan, *, user_confirmed, fingerprint):
        if type(plan) is not ProvisionPlan or plan_provision(plan.profile_name,plan.expected_sid,plan.acl_grants)!=plan:
            raise WindowsSandboxError('invalid provision plan')
        if user_confirmed is not True or fingerprint != plan.fingerprint:
            raise WindowsSandboxError('profile creation requires exact explicit confirmation')
        with self._lock:
            if fingerprint in self._used:raise WindowsSandboxError('approval already attempted')
            self._used.add(fingerprint)
        # Collision is refused; no automatic adoption, ACL change, profile removal or retry.
        sid=self.api.create_profile(plan.profile_name,plan.display_name,plan.expected_sid)
        if sid!=plan.expected_sid:raise WindowsSandboxError('created profile SID differs; manual review required')
        return {'profile_name':plan.profile_name,'sid':sid,'acl_applied':False,
            'ledger_record_required':True,'launch_authorized':False}
    def execute(self, plan, *, user_confirmed, fingerprint, host_verifier: Callable,
                on_child_created: Callable | None = None,
                on_verified_suspended: Callable | None = None, setup_timeout=5):
        """Launch with optional trusted-host wiring hooks, never package callbacks.

        on_child_created gets an immutable borrowed child view immediately after
        CreateProcess, before any Job/observation work, so the channel owner can
        close host copies of child pipe ends and release its launch lock exactly
        once. It must close those copies even if its own callback then fails.
        on_verified_suspended gets (child view, observation, evidence, cancellation
        control), outside the Job lock, after initial native/two host checks. It
        may bind the broker/session but must not resume or close borrowed handles.
        Host and native evidence are rechecked afterwards; cancellation cannot
        resume. The caller owns broker/session resources created by hooks and must
        revoke them in its own failure/finally path. Hook exceptions follow the
        same retryable native cleanup as any launch failure; no hook is retried.

        Setup has a separate 1..10s Job watchdog after assignment (default 5s).
        The 10s/30s runtime budget starts immediately before Resume, and its exact
        deadline is returned for init/transport/proxy to share without resetting.
        Hooks are bounded host wiring, never human approval waits. Terminating a
        Job cannot forcibly unwind a stuck Python hook; its caller must retain the
        admission/resource owner until actual cleanup is confirmed. Pre-assignment
        native calls and on_child_created cannot be bounded by a not-yet-assigned Job.
        """
        if type(setup_timeout) not in (int,float) or not math.isfinite(setup_timeout) or not 1 <= setup_timeout <= 10:
            raise WindowsSandboxError('setup_timeout must be 1..10 seconds')
        if any(value is not None and not callable(value)
               for value in (on_child_created,on_verified_suspended)):
            raise WindowsSandboxError('trusted host callbacks must be callable or absent')
        if type(plan) is not ApprovedLaunchPlan:
            raise WindowsSandboxError('approved launch plan required')
        # Never propagate semantic-only preview validation into execution.
        fresh=plan_launch(plan.policy_plan,plan.executable,plan.argv,dict(plan.environment))
        if fresh!=plan or user_confirmed is not True or fingerprint!=plan.fingerprint or not callable(host_verifier):
            raise WindowsSandboxError('exact confirmation and trusted host verifier required')
        with self._lock:
            if fingerprint in self._used:raise WindowsSandboxError('approval already attempted')
            self._used.add(fingerprint)
        evidence=host_verifier(plan)
        if type(evidence) is not TrustedHostEvidence or evidence.fingerprint!=plan.fingerprint or not all(
            getattr(evidence,k) is True for k in ('acl_verified','inventories_reverified','runtime_release_verified',
                'profile_ledger_verified','scratch_prepared_verified','global_lease_verified','network_isolation_verified')):
            raise WindowsSandboxError('host prerequisites unverified; no launch or fallback')
        child=None;job=None;watchdog=None;setup_watchdog=None
        runtime_started_at=None;runtime_deadline=None
        try:
            api=self.api
            child=api.create_suspended(plan)
            child_view=SuspendedChild(child.process,child.thread,child.pid)
            if on_child_created is not None:
                on_child_created(child_view)
            job=JobOwner(api,api.create_job())
            api.configure_job(job.handle,plan.policy_plan.job)
            api.assign_job(job.handle,child.process)
            setup_started_at=self.clock()
            setup_deadline=setup_started_at+setup_timeout
            setup_watchdog=api.arm_watchdog(job,setup_timeout)
            if setup_watchdog is None or getattr(setup_watchdog,'armed',False) is not True:
                raise WindowsSandboxError('setup watchdog not armed')
            prepare_watchdog=getattr(api,'prepare_watchdog',None)
            if not callable(prepare_watchdog):
                raise WindowsSandboxError('deferred runtime watchdog unavailable; no immediate-timer fallback')
            watchdog=prepare_watchdog(job,plan.policy_plan.job.wall_seconds,clock=self.clock)
            if (watchdog is None or getattr(watchdog,'prepared',False) is not True
                    or getattr(watchdog,'started',None) is not False
                    or getattr(watchdog,'armed',False) is not True):
                raise WindowsSandboxError('runtime watchdog not prepared')
            with job.lock:
                if (getattr(setup_watchdog,'armed',False) is not True or self.clock() >= setup_deadline
                        or not job.handle or job.timed_out):
                    raise WindowsSandboxError('setup budget expired before native observation')
                native=api.observe(child,job.handle,plan)
            if not isinstance(native,dict) or set(native)!= {'is_appcontainer','appcontainer_sid','capabilities',
                'network_capabilities','suspended','job','job_membership_verified','inherited_handles','handle_whitelist_verified'}:
                raise WindowsSandboxError('missing actual native observation')
            observation=HostLaunchObservation(source='host_os_query',actual_os_verified=True,os_supported=True,
                pid=child.pid,expected_pid=child.pid,creation_flags=plan.policy_plan.creation_flags,
                watchdog_armed=True,acl_verified=evidence.acl_verified,inventories_reverified=evidence.inventories_reverified,
                plan_fingerprint=plan.policy_plan.fingerprint,**native)
            decision=verify_resume(plan.policy_plan,observation)
            if not decision.allowed:raise WindowsSandboxError('resume blocked: '+','.join(decision.reasons))
            # The host must re-observe exact files/ACL/ledger immediately before resume.
            if host_verifier(plan)!=evidence:raise WindowsSandboxError('host observations changed before resume')
            if on_verified_suspended is not None:
                # Never hold JobOwner.lock across host work: the watchdog must
                # remain able to cancel/terminate a callback that consumes budget.
                on_verified_suspended(child_view,observation,evidence,JobCancellation(job))
                if host_verifier(plan)!=evidence:
                    raise WindowsSandboxError('host observations changed after suspended callback')
                with job.lock:
                    if (getattr(setup_watchdog,'armed',False) is not True or self.clock() >= setup_deadline
                            or getattr(watchdog,'armed',False) is not True or not job.handle or job.timed_out):
                        raise WindowsSandboxError('setup budget expired during suspended callback')
                    # Observation borrows the raw Job handle only while its owner
                    # lock prevents a timeout fallback from closing/recycling it.
                    native_after=api.observe(child,job.handle,plan)
                if not isinstance(native_after,dict) or set(native_after)!=set(native):
                    raise WindowsSandboxError('missing native observation after suspended callback')
                observation=HostLaunchObservation(source='host_os_query',actual_os_verified=True,os_supported=True,
                    pid=child.pid,expected_pid=child.pid,creation_flags=plan.policy_plan.creation_flags,
                    watchdog_armed=getattr(watchdog,'armed',False) is True,
                    acl_verified=evidence.acl_verified,inventories_reverified=evidence.inventories_reverified,
                    plan_fingerprint=plan.policy_plan.fingerprint,**native_after)
                decision=verify_resume(plan.policy_plan,observation)
                if not decision.allowed:
                    raise WindowsSandboxError('resume blocked after suspended callback: '+','.join(decision.reasons))
                # The callback/native-query interval can itself race host changes.
                if host_verifier(plan)!=evidence:
                    raise WindowsSandboxError('host observations changed after callback reobservation')
            receipt=AttestedChild(observation.pid,observation.appcontainer_sid,
                observation.plan_fingerprint,plan.policy_plan.runtime_manifest_sha256,
                observation.capabilities,observation.network_capabilities,
                tuple((handle.purpose,value) for handle,value in
                    zip(plan.policy_plan.inherited_handles,observation.inherited_handles)))
            # Serialize the final armed/owned check with timer termination/close.
            # A callback cannot close/recycle this Job between the check and resume.
            with job.lock:
                if (getattr(setup_watchdog,'armed',False) is not True or self.clock() >= setup_deadline
                        or getattr(watchdog,'armed',False) is not True or not job.handle or job.timed_out):
                    raise WindowsSandboxError('setup budget expired before resume')
                # Disarm only while locked; do not join a timer waiting on this lock.
                # The shared deadline closes the cancellation-to-Resume race even
                # if the setup timer callback has not acquired the lock yet.
                disarm=getattr(setup_watchdog,'disarm',None)
                if not callable(disarm):
                    raise WindowsSandboxError('setup watchdog nonwaiting disarm unavailable')
                disarm()
                if self.clock() >= setup_deadline or job.timed_out or not job.handle:
                    raise WindowsSandboxError('setup budget expired at resume boundary')
                watchdog.start()
                runtime_started_at=watchdog.started_at
                runtime_deadline=watchdog.deadline
                if (getattr(watchdog,'started',False) is not True
                        or type(runtime_started_at) not in (int,float) or not math.isfinite(runtime_started_at)
                        or type(runtime_deadline) not in (int,float) or not math.isfinite(runtime_deadline)
                        or runtime_deadline != runtime_started_at+plan.policy_plan.job.wall_seconds
                        or self.clock() >= setup_deadline or self.clock() >= runtime_deadline
                        or not watchdog.armed or job.timed_out or not job.handle):
                    raise WindowsSandboxError('runtime watchdog failed at resume boundary')
                api.resume(child.thread)
            setup_watchdog.cancel();setup_watchdog=None
            api.close(child.thread);child.thread=0
            return RunningSandbox(api,child,job,watchdog,host_observation=observation,attested_child=receipt,
                runtime_started_at=runtime_started_at,runtime_deadline=runtime_deadline)
        except BaseException as original:
            cleanup_errors=[]
            if child is not None:
                try:self.api.terminate(child.process)
                except BaseException as error:cleanup_errors.append(type(error).__name__)
                # Reuse the independent, retryable owner cleanup. Successful native
                # closes clear ownership; failed handles stay reachable by the host
                # on the original error instead of being leaked in a local variable.
                resource=RunningSandbox(self.api,child,job if job is not None else 0,watchdog,
                    setup_watchdog=setup_watchdog,runtime_started_at=runtime_started_at,runtime_deadline=runtime_deadline,
                    require_process_exit=True)
                try:resource.close()
                except BaseException as error:cleanup_errors.append(type(error).__name__)
                if not resource.closed:
                    original.sandbox_cleanup=resource
            if cleanup_errors:
                original.add_note('Native cleanup errors; host review required: '+','.join(cleanup_errors))
            raise


# Fixed-width Win32 layout definitions. No WinDLL load or OS call at module import.
SIZE_T=C.c_size_t; HANDLE=W.HANDLE; DWORD=W.DWORD; ULONG_PTR=C.c_size_t
class SECURITY_ATTRIBUTES(C.Structure):
    _fields_=[('nLength',DWORD),('lpSecurityDescriptor',C.c_void_p),('bInheritHandle',W.BOOL)]
class SECURITY_CAPABILITIES(C.Structure):
    _fields_=[('AppContainerSid',C.c_void_p),('Capabilities',C.c_void_p),('CapabilityCount',DWORD),('Reserved',DWORD)]
class STARTUPINFO(C.Structure):
    _fields_=[('cb',DWORD),('lpReserved',W.LPWSTR),('lpDesktop',W.LPWSTR),('lpTitle',W.LPWSTR),
        ('dwX',DWORD),('dwY',DWORD),('dwXSize',DWORD),('dwYSize',DWORD),('dwXCountChars',DWORD),
        ('dwYCountChars',DWORD),('dwFillAttribute',DWORD),('dwFlags',DWORD),('wShowWindow',W.WORD),
        ('cbReserved2',W.WORD),('lpReserved2',C.c_void_p),('hStdInput',HANDLE),('hStdOutput',HANDLE),('hStdError',HANDLE)]
class STARTUPINFOEX(C.Structure):_fields_=[('StartupInfo',STARTUPINFO),('lpAttributeList',C.c_void_p)]
class PROCESS_INFORMATION(C.Structure):_fields_=[('hProcess',HANDLE),('hThread',HANDLE),('dwProcessId',DWORD),('dwThreadId',DWORD)]
class BASIC_LIMITS(C.Structure):
    _fields_=[('PerProcessUserTimeLimit',C.c_longlong),('PerJobUserTimeLimit',C.c_longlong),('LimitFlags',DWORD),
        ('MinimumWorkingSetSize',SIZE_T),('MaximumWorkingSetSize',SIZE_T),('ActiveProcessLimit',DWORD),
        ('Affinity',ULONG_PTR),('PriorityClass',DWORD),('SchedulingClass',DWORD)]
class IO_COUNTERS(C.Structure):_fields_=[(name,C.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
class EXTENDED_LIMITS(C.Structure):
    _fields_=[('BasicLimitInformation',BASIC_LIMITS),('IoInfo',IO_COUNTERS),('ProcessMemoryLimit',SIZE_T),
        ('JobMemoryLimit',SIZE_T),('PeakProcessMemoryUsed',SIZE_T),('PeakJobMemoryUsed',SIZE_T)]
class CPU_LIMITS(C.Structure):_fields_=[('ControlFlags',DWORD),('CpuRate',DWORD)]
class SID_AND_ATTRIBUTES(C.Structure):_fields_=[('Sid',C.c_void_p),('Attributes',DWORD)]
class PROCESS_HANDLE_ENTRY(C.Structure):
    _fields_=[('HandleValue',HANDLE),('HandleCount',SIZE_T),('PointerCount',SIZE_T),('GrantedAccess',DWORD),
        ('ObjectTypeIndex',DWORD),('HandleAttributes',DWORD),('Reserved',DWORD)]


class NativeWindowsApi:
    """Trusted lazy Win32 adapter. Construct ONLY after explicit host execution approval."""
    def __init__(self):
        if os.name!='nt':raise WindowsSandboxError('Windows AppContainer unsupported; no fallback')
        self.k=C.WinDLL('kernel32',use_last_error=True)
        self.a=C.WinDLL('advapi32',use_last_error=True)
        self.u=C.WinDLL('userenv',use_last_error=True)
        self.n=C.WinDLL('ntdll',use_last_error=True)
        def bind(lib,name,args,result):
            fn=getattr(lib,name,None)
            if fn is None:raise WindowsSandboxError('required native API unavailable: '+name)
            fn.argtypes=args;fn.restype=result;return fn
        P=C.c_void_p;PH=C.POINTER(HANDLE);PD=C.POINTER(DWORD)
        self.CloseHandle=bind(self.k,'CloseHandle',[HANDLE],W.BOOL)
        self.LocalFree=bind(self.k,'LocalFree',[P],P)
        self.FreeSid=bind(self.a,'FreeSid',[P],P)
        self.ConvertStringSidToSid=bind(self.a,'ConvertStringSidToSidW',[W.LPCWSTR,C.POINTER(P)],W.BOOL)
        self.ConvertSidToStringSid=bind(self.a,'ConvertSidToStringSidW',[P,C.POINTER(W.LPWSTR)],W.BOOL)
        self.CreateProfile=bind(self.u,'CreateAppContainerProfile',[W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,P,DWORD,C.POINTER(P)],C.c_long)
        self.DeriveProfile=bind(self.u,'DeriveAppContainerSidFromAppContainerName',[W.LPCWSTR,C.POINTER(P)],C.c_long)
        self.InitializeAttributes=bind(self.k,'InitializeProcThreadAttributeList',[P,DWORD,DWORD,C.POINTER(SIZE_T)],W.BOOL)
        self.UpdateAttribute=bind(self.k,'UpdateProcThreadAttribute',[P,DWORD,ULONG_PTR,P,SIZE_T,P,P],W.BOOL)
        self.DeleteAttributes=bind(self.k,'DeleteProcThreadAttributeList',[P],None)
        self.GetHandleInformation=bind(self.k,'GetHandleInformation',[HANDLE,PD],W.BOOL)
        self.GetFileType=bind(self.k,'GetFileType',[HANDLE],DWORD)
        self.CreateProcess=bind(self.k,'CreateProcessW',[W.LPCWSTR,W.LPWSTR,P,P,W.BOOL,DWORD,P,W.LPCWSTR,C.POINTER(STARTUPINFOEX),C.POINTER(PROCESS_INFORMATION)],W.BOOL)
        self.CreateJob=bind(self.k,'CreateJobObjectW',[P,W.LPCWSTR],HANDLE)
        self.SetJob=bind(self.k,'SetInformationJobObject',[HANDLE,C.c_int,P,DWORD],W.BOOL)
        self.QueryJob=bind(self.k,'QueryInformationJobObject',[HANDLE,C.c_int,P,DWORD,PD],W.BOOL)
        self.AssignJob=bind(self.k,'AssignProcessToJobObject',[HANDLE,HANDLE],W.BOOL)
        self.InJob=bind(self.k,'IsProcessInJob',[HANDLE,HANDLE,C.POINTER(W.BOOL)],W.BOOL)
        self.Resume=bind(self.k,'ResumeThread',[HANDLE],DWORD)
        self.Terminate=bind(self.k,'TerminateProcess',[HANDLE,W.UINT],W.BOOL)
        self.WaitProcess=bind(self.k,'WaitForSingleObject',[HANDLE,DWORD],DWORD)
        self.GetExitCode=bind(self.k,'GetExitCodeProcess',[HANDLE,PD],W.BOOL)
        self.TerminateJob=bind(self.k,'TerminateJobObject',[HANDLE,W.UINT],W.BOOL)
        self.OpenToken=bind(self.a,'OpenProcessToken',[HANDLE,DWORD,PH],W.BOOL)
        self.TokenInfo=bind(self.a,'GetTokenInformation',[HANDLE,C.c_int,P,DWORD,PD],W.BOOL)
        self.ThreadInfo=bind(self.n,'NtQueryInformationThread',[HANDLE,C.c_int,P,DWORD,PD],C.c_long)
        self.ProcessInfo=bind(self.n,'NtQueryInformationProcess',[HANDLE,C.c_int,P,DWORD,PD],C.c_long)
    def _check(self,ok,name):
        if not ok:raise WindowsSandboxError(name+' failed: '+str(C.get_last_error()))
    def close(self,handle):
        if handle and not self.CloseHandle(handle):
            raise WindowsSandboxError('CloseHandle failed; resource cleanup not verified')
    def process_exited(self,handle):
        """Read-only signal observation; termination API success is not exit proof."""
        result=self.WaitProcess(handle,0)
        if result not in (0,258):raise WindowsSandboxError('process wait observation failed')
        return result==0
    def exit_code(self,handle):
        code=DWORD();self._check(self.GetExitCode(handle,C.byref(code)),'GetExitCodeProcess')
        return code.value
    def _sid_text(self,sid):
        text=W.LPWSTR();self._check(self.ConvertSidToStringSid(sid,C.byref(text)),'ConvertSidToStringSid')
        try:return text.value
        finally:self.LocalFree(C.cast(text,C.c_void_p))
    def derive_sid(self,name):
        """Read-only derivation, no existing profile is implicitly trusted."""
        sid=C.c_void_p();status=self.DeriveProfile(name,C.byref(sid))
        if status<0:raise WindowsSandboxError('DeriveAppContainerSid failed: '+hex(status & 0xffffffff))
        try:return self._sid_text(sid)
        finally:self.FreeSid(sid)
    def create_profile(self,name,display,expected_sid):
        if self.derive_sid(name)!=expected_sid:raise WindowsSandboxError('derived profile SID differs from approved SID')
        sid=C.c_void_p();status=self.CreateProfile(name,display,'Lingxi strictly isolated local plugin',None,0,C.byref(sid))
        if status<0:raise WindowsSandboxError('profile creation/collision refused: '+hex(status & 0xffffffff))
        try:
            value=self._sid_text(sid)
            if value!=expected_sid:raise WindowsSandboxError('created SID mismatch; manual host review required, no auto-delete')
            return value
        finally:self.FreeSid(sid)
    def create_suspended(self,plan):
        # Defense in depth even if a trusted host bypasses WindowsLauncher.execute.
        if type(plan) is not ApprovedLaunchPlan:
            raise WindowsSandboxError('approved physical launch plan required')
        fresh=plan_launch(plan.policy_plan,plan.executable,plan.argv,dict(plan.environment))
        if fresh!=plan:raise WindowsSandboxError('modified physical launch plan')
        p=plan.policy_plan;handles=tuple(h.value for h in p.inherited_handles)
        purposes={h.purpose:h.value for h in p.inherited_handles}
        expected_roles = ({'stdin_read','stdout_write','diagnostics_write','proxy_request_read','proxy_result_write'}
                          if p.policy.mode == 'stdio_mcp' else {'request_read','result_write','diagnostics_write'})
        if len(handles)!=len(expected_roles) or len(set(handles))!=len(expected_roles) or set(purposes)!=expected_roles:
            raise WindowsSandboxError('exact mode-specific independent pipe whitelist required')
        for handle in handles:
            flags=DWORD();self._check(self.GetHandleInformation(handle,C.byref(flags)),'GetHandleInformation')
            if not flags.value & 1 or self.GetFileType(handle)!=3:raise WindowsSandboxError('only verified inheritable pipes may cross boundary')
        sid=C.c_void_p();self._check(self.ConvertStringSidToSid(p.appcontainer_sid,C.byref(sid)),'ConvertStringSidToSid')
        attrs=None;initialized=False;pi=PROCESS_INFORMATION()
        try:
            size=SIZE_T();self.InitializeAttributes(None,2,0,C.byref(size))
            if not 0<size.value<1024**2:raise WindowsSandboxError('startup attribute size unsupported')
            attrs=C.create_string_buffer(size.value)
            self._check(self.InitializeAttributes(attrs,2,0,C.byref(size)),'InitializeProcThreadAttributeList');initialized=True
            caps=SECURITY_CAPABILITIES(sid,None,0,0)
            inherited=(HANDLE*len(handles))(*handles)
            self._check(self.UpdateAttribute(attrs,0,0x00020009,C.byref(caps),C.sizeof(caps),None,None),'SECURITY_CAPABILITIES')
            self._check(self.UpdateAttribute(attrs,0,0x00020002,inherited,C.sizeof(inherited),None,None),'HANDLE_LIST')
            startup=STARTUPINFOEX();startup.StartupInfo.cb=C.sizeof(startup);startup.lpAttributeList=C.cast(attrs,C.c_void_p)
            startup.StartupInfo.dwFlags=0x100
            startup.StartupInfo.hStdInput=purposes['stdin_read' if p.policy.mode == 'stdio_mcp' else 'request_read']
            startup.StartupInfo.hStdOutput=purposes['stdout_write' if p.policy.mode == 'stdio_mcp' else 'result_write']
            startup.StartupInfo.hStdError=purposes['diagnostics_write']
            command=C.create_unicode_buffer(subprocess.list2cmdline(plan.argv))
            env=C.create_unicode_buffer('\x00'.join(k+'='+v for k,v in plan.environment)+'\x00\x00')
            self._check(self.CreateProcess(plan.executable,command,None,None,True,p.creation_flags|0x400,
                env,p.paths.scratch,C.byref(startup),C.byref(pi)),'CreateProcessW suspended')
            return Child(int(pi.hProcess),int(pi.hThread),int(pi.dwProcessId))
        finally:
            if initialized:self.DeleteAttributes(attrs)
            self.LocalFree(sid)
    def create_job(self):
        value=self.CreateJob(None,None);self._check(value,'CreateJobObjectW');return int(value)
    def configure_job(self,job,limits):
        info=EXTENDED_LIMITS();info.BasicLimitInformation.LimitFlags=limits.limit_flags
        info.BasicLimitInformation.ActiveProcessLimit=limits.active_processes
        info.ProcessMemoryLimit=limits.process_memory_bytes;info.JobMemoryLimit=limits.job_memory_bytes
        self._check(self.SetJob(job,9,C.byref(info),C.sizeof(info)),'SetJob extended limits')
        cpu=CPU_LIMITS(limits.cpu_control_flags,limits.cpu_rate)
        self._check(self.SetJob(job,15,C.byref(cpu),C.sizeof(cpu)),'SetJob CPU hard cap')
    def assign_job(self,job,process):self._check(self.AssignJob(job,process),'AssignProcessToJobObject')
    def _token_info(self,token,kind):
        size=DWORD();self.TokenInfo(token,kind,None,0,C.byref(size))
        if not 0<size.value<=1024**2:raise WindowsSandboxError('token observation unsupported')
        buffer=C.create_string_buffer(size.value)
        self._check(self.TokenInfo(token,kind,buffer,size.value,C.byref(size)),'GetTokenInformation')
        return buffer
    def _child_handles(self,process):
        size=4096
        while size<=1024**2:
            buf=C.create_string_buffer(size);needed=DWORD()
            status=self.ProcessInfo(process,51,buf,size,C.byref(needed))
            if status>=0:
                count=C.cast(buf,C.POINTER(SIZE_T))[0]
                if count>(size-2*C.sizeof(SIZE_T))//C.sizeof(PROCESS_HANDLE_ENTRY):raise WindowsSandboxError('invalid process handle observation')
                array=(PROCESS_HANDLE_ENTRY*count).from_buffer(buf,2*C.sizeof(SIZE_T))
                return tuple(sorted(int(e.HandleValue) for e in array))
            if status & 0xffffffff not in (0xc0000004,0xc0000023):
                raise WindowsSandboxError('actual handle table query unavailable; no resume')
            size=max(size*2,needed.value)
        raise WindowsSandboxError('process handle observation capacity exceeded')
    def observe(self,child,job,plan):
        token=HANDLE();self._check(self.OpenToken(child.process,0x8,C.byref(token)),'OpenProcessToken')
        try:
            app=C.cast(self._token_info(token,29),C.POINTER(DWORD))[0]!=0
            sid_buffer=self._token_info(token,31)
            sid=C.cast(sid_buffer,C.POINTER(C.c_void_p))[0]
            actual_sid=self._sid_text(sid) if sid else None
            caps_buffer=self._token_info(token,30)
            count=C.cast(caps_buffer,C.POINTER(DWORD))[0]
            if count:raise WindowsSandboxError('nonempty capability token; no resume')
        finally:self.close(token)
        member=W.BOOL();self._check(self.InJob(child.process,job,C.byref(member)),'IsProcessInJob')
        info=EXTENDED_LIMITS();cpu=CPU_LIMITS();needed=DWORD()
        self._check(self.QueryJob(job,9,C.byref(info),C.sizeof(info),C.byref(needed)),'QueryJob limits')
        self._check(self.QueryJob(job,15,C.byref(cpu),C.sizeof(cpu),C.byref(needed)),'QueryJob CPU')
        suspend=DWORD();status=self.ThreadInfo(child.thread,35,C.byref(suspend),C.sizeof(suspend),C.byref(needed))
        if status<0:raise WindowsSandboxError('actual thread suspension query unavailable; no resume')
        actual_handles=self._child_handles(child.process)
        wanted=tuple(sorted(h.value for h in plan.policy_plan.inherited_handles))
        observed_job=JobPlan(info.BasicLimitInformation.LimitFlags,info.ProcessMemoryLimit,info.JobMemoryLimit,
            info.BasicLimitInformation.ActiveProcessLimit,cpu.ControlFlags,cpu.CpuRate,plan.policy_plan.job.wall_seconds)
        return {'is_appcontainer':app,'appcontainer_sid':actual_sid,'capabilities':(), 'network_capabilities':(),
            'suspended':suspend.value==1,'job':observed_job,'job_membership_verified':bool(member.value),
            'inherited_handles':tuple(h.value for h in plan.policy_plan.inherited_handles),
            'handle_whitelist_verified':actual_handles==wanted}
    def terminate_job(self,job):
        self._check(self.TerminateJob(job,0xdead),'TerminateJobObject')
    def arm_watchdog(self,job,seconds):
        if type(job) is not JobOwner or job.api is not self:
            raise WindowsSandboxError('watchdog must share the exact owned Job handle')
        return JobWatchdog(job,seconds)
    def prepare_watchdog(self,job,seconds,*,clock=time.monotonic):
        if type(job) is not JobOwner or job.api is not self:
            raise WindowsSandboxError('watchdog must share the exact owned Job handle')
        return JobWatchdog(job,seconds,start_immediately=False,clock=clock)
    def resume(self,thread):
        previous=self.Resume(thread)
        if previous!=1:raise WindowsSandboxError('unexpected ResumeThread count; terminate child')
    def terminate(self,process):self._check(self.Terminate(process,0xdead),'TerminateProcess')
