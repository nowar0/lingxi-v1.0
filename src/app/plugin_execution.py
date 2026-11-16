"""Thread-safe host admission/execution coordination, not an OS sandbox.

The host must inject its SINGLE shared scheduler. Prepare runs only after admission
and must atomically recheck live package/trust state and consume an exact approval
under a short manager lock. Actual execution and cancellation hooks NEVER run while
this coordinator holds its state lock. This module starts no processes or I/O.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import re
import secrets
import threading
from typing import Any, Callable

from app.plugin_scheduler import AdmissionError, PluginCallScheduler, SchedulerLease

_PLUGIN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_CALL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
TERMINAL = frozenset(('completed', 'failed', 'unknown'))


class ExecutionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExecutionIdentity:
    plugin_id: str
    revision: int
    digest: str
    call_id: str
    trust_fingerprint: str

    def __post_init__(self):
        if (not isinstance(self.plugin_id, str) or len(self.plugin_id) > 200
                or not _PLUGIN.fullmatch(self.plugin_id)
                or type(self.revision) is not int or not 1 <= self.revision <= 2**53-1
                or not isinstance(self.call_id, str) or not _CALL.fullmatch(self.call_id)
                or not isinstance(self.digest, str) or not _SHA.fullmatch(self.digest)
                or not isinstance(self.trust_fingerprint, str) or not _SHA.fullmatch(self.trust_fingerprint)):
            raise ValueError('exact bounded execution identity required')


@dataclass(frozen=True)
class ResourceCleanupReceipt:
    """Host adapter contract, NOT OS proof merely because a dataclass exists.

    Production finish must obtain all observations from the actual resource owner.
    Unknown, absent or mismatched observations never free an admission reservation.
    """
    identity: ExecutionIdentity
    job_terminated_or_exited: bool = False
    pipes_closed: bool = False
    writer_threads_stopped: bool = False
    cancelhooks_completed: bool = False
    source: str = 'unverified'


@dataclass(frozen=True)
class ExecutionContext:
    identity: ExecutionIdentity
    cancel_event: threading.Event
    # Trusted opaque result from prepare, never included in snapshots or audit metadata.
    prepared: Any
    # Host-only existing admission owner. Never serialized to a package or copied
    # into approval metadata, and never substituted by a second scheduler lease.
    admission_lease: SchedulerLease | None = field(default=None,repr=False,compare=False)


@dataclass(frozen=True)
class ExecutionResult:
    identity: ExecutionIdentity
    phase: str
    code: str
    result: Any = None
    cancellation_hook_failed: bool = False
    finish_failed: bool = False
    external_effects_undone: bool = False
    automatic_retry: bool = False
    # finish receives only a provisional finishing record. execute/final_audit
    # receive final=True, whose phase/code/result can never be changed by auditing.
    final: bool = True
    candidate_phase: str | None = None
    candidate_code: str | None = None
    cancellation_hook_pending: bool = False
    audit_failed: bool = False
    resources_pending: bool = False
    quarantined: bool = False
    final_gate_failed: bool = False


@dataclass
class _Call:
    identity: ExecutionIdentity
    cancel_event: threading.Event
    phase: str = 'queued'
    code: str = ''
    running: bool = False
    started: bool = False
    finalized: bool = False
    hook: Callable[[], None] | None = None
    hook_registered: bool = False
    hook_invoked: bool = False
    hook_failed: bool = False
    hook_completed: bool = False
    audit_failed: bool = False
    final_gate_failed: bool = False
    lease: SchedulerLease | None = None
    resources_active: bool = False
    quarantined: bool = False
    cleanup_finished: bool = False
    resource_cleanup_required: bool = False


@dataclass(frozen=True)
class DeferredCancellation:
    """Opaque coordinator-owned dispatch capability, never parsed from package JSON.

    Claiming has no foreign-code effect. Holders MUST dispatch after releasing their
    manager/DB locks, even when subsequent response construction or auditing fails.
    """
    _owner: object = field(repr=False, compare=False)
    _token: str = field(repr=False)
    _affected_count: int = field(repr=False)


class ExecutionCoordinator:
    """One host coordinator over an injected scheduler; maximum ten active calls.

    Call IDs are never reused during this instance's bounded lifetime. At capacity
    the host must explicitly rotate only after quiescence, not forget old call IDs
    and replay approvals. Queue wait time is distinct from the worker's wall budget.
    """
    def __init__(self, scheduler: PluginCallScheduler, *, max_records=4096,
                 owns_scheduler=False):
        if not isinstance(scheduler, PluginCallScheduler) or scheduler.max_concurrent > 10:
            raise TypeError('an explicitly shared host scheduler, ceiling ten, is required')
        if type(max_records) is not int or not 10 <= max_records <= 100000:
            raise ValueError('max_records must be 10..100000')
        if type(owns_scheduler) is not bool:
            raise ValueError('scheduler ownership must be explicit')
        self.scheduler = scheduler
        self.max_records = max_records
        self.owns_scheduler = owns_scheduler
        self._lock = threading.RLock()
        self._calls: dict[str, _Call] = {}
        self._mutating: set[str] = set()
        self._closed = False
        self._deferred_cancellations = {}

    def snapshot(self):
        """Only call identities and phases/counts, never grants, args or results."""
        with self._lock:
            calls = [{'plugin_id': c.identity.plugin_id, 'revision': c.identity.revision,
                      'digest': c.identity.digest, 'call_id': c.identity.call_id,
                      'phase': c.phase, 'code': c.code, 'running': c.running,
                      'finalized': c.finalized, 'cancel_requested': c.cancel_event.is_set(),
                      'cancellation_hook_failed': c.hook_failed,
                       'cancellation_hook_pending': c.hook_invoked and not c.hook_completed,
                       'audit_failed': c.audit_failed,
                        'final_gate_failed': c.final_gate_failed,
                       'resources_active': c.resources_active, 'quarantined': c.quarantined}
                     for c in self._calls.values()]
            counts: dict[str, int] = {}
            for c in self._calls.values():
                if c.resources_active:
                    counts[c.identity.plugin_id] = counts.get(c.identity.plugin_id, 0) + 1
            return {'closed': self._closed, 'max_concurrent': self.scheduler.max_concurrent,
                    'running': sum(counts.values()),
                    'queued': sum(not c.finalized and c.phase == 'queued' for c in self._calls.values()),
                    'inflight': sum(not c.finalized or c.resources_active for c in self._calls.values()),
                    'quarantined': sum(c.quarantined for c in self._calls.values()),
                    'loaded_counts': counts, 'calls': calls}

    def loaded_count(self, plugin_id):
        with self._lock:
            return sum(c.resources_active and c.identity.plugin_id == plugin_id for c in self._calls.values())

    def assert_plugin_idle(self, plugin_id):
        """Update/uninstall must also hold the host manager's mutation barrier.

        This is an observation, not a lease: production integration must serialize
        registration against mutation, rather than assert then mutate with a race.
        It never cancels or kills work as an implicit update side effect.
        """
        with self._lock:
            if any((not c.finalized or c.resources_active) and c.identity.plugin_id == plugin_id for c in self._calls.values()):
                raise ExecutionError('plugin_busy')
        return True

    @contextmanager
    def mutation_guard(self, plugin_id):
        """Reserve an idle plugin atomically against new call registration.

        Host update/uninstall takes its manager mutation lock before this guard;
        neither lock may be held by run(). Guard itself does not cancel anything.
        """
        if not isinstance(plugin_id, str) or len(plugin_id) > 200 or not _PLUGIN.fullmatch(plugin_id):
            raise ValueError('plugin identity required')
        with self._lock:
            if self._closed:
                raise ExecutionError('coordinator_closed')
            if plugin_id in self._mutating or any(
                    (not c.finalized or c.resources_active) and c.identity.plugin_id == plugin_id for c in self._calls.values()):
                raise ExecutionError('plugin_busy')
            self._mutating.add(plugin_id)
        try:
            yield
        finally:
            with self._lock:
                self._mutating.remove(plugin_id)

    def _release_resources_locked(self, call):
        if call.lease is not None:
            call.lease.release()
        call.resources_active = False
        call.quarantined = False

    @staticmethod
    def _cleanup_verified(call, receipt):
        return (type(receipt) is ResourceCleanupReceipt
                and type(receipt.identity) is ExecutionIdentity and receipt.identity == call.identity
                and receipt.source == 'host_os_query'
                and receipt.job_terminated_or_exited is True
                and receipt.pipes_closed is True and receipt.writer_threads_stopped is True
                and receipt.cancelhooks_completed is True)

    def release_quarantine(self, call_id, receipt):
        """Trusted owner repair, never an RPC/automatic retry or terminal rewrite.

        Actual hook completion is checked independently of a receipt's claim. Works
        after close() so a retained resource owner can safely finish cleanup later.
        """
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or not call.finalized or not call.quarantined or not call.resources_active:
                raise ExecutionError('quarantine_not_active')
            if call.hook_invoked and not call.hook_completed:
                raise ExecutionError('cancellation_hook_pending')
            if not self._cleanup_verified(call, receipt):
                raise ExecutionError('resource_cleanup_unverified')
            self._release_resources_locked(call)
            return True

    def _claim_hook_locked(self, call):
        """Claim once in the SAME critical section that accepts cancellation.

        The returned callable is independent of call.hook: finalization may drop
        the stored closure, but cannot retract an already accepted cancellation.
        """
        if not call.cancel_event.is_set() or call.hook is None or call.hook_invoked:
            return None
        call.hook_invoked = True
        return call.hook

    def _invoke_hook(self, call, hook):
        """Run an already claimed hook outside coordinator/scheduler locks."""
        try:
            hook()
        except BaseException:
            # One faulty Job close callback cannot suppress cancellation of others.
            with self._lock:
                call.hook_failed = True
        finally:
            with self._lock:
                call.hook_completed = True
                # Legacy callbacks model no native resources. Even there a claimed
                # hook owns the reservation until it actually returns. Strict mode
                # never auto-releases quarantine from a callback completion alone.
                if (call.finalized and call.cleanup_finished and call.resources_active
                        and not call.resource_cleanup_required):
                    self._release_resources_locked(call)

    def _register_hook(self, call, hook):
        if not callable(hook):
            raise TypeError('trusted cancellation hook required')
        with self._lock:
            if call.hook_registered or call.finalized:
                raise ExecutionError('cancellation_hook_already_registered_or_finished')
            call.hook_registered = True
            call.hook = hook
            claimed = self._claim_hook_locked(call)
        # A cancellation that beat registration closes the actual Job immediately.
        if claimed is not None:
            self._invoke_hook(call, claimed)

    def _claim_cancellations_locked(self, calls):
        affected = [call for call in calls if not call.finalized]
        claimed = []
        for call in affected:
            call.cancel_event.set()
            call.phase = 'cancelling'
            hook = self._claim_hook_locked(call)
            if hook is not None:
                claimed.append((call, hook))
        token = secrets.token_hex(24)
        capability = DeferredCancellation(self, token, len(affected))
        # Empty claims need no retained registry entry. Every nonempty entry owns
        # at least one hook, hence the existing max_records also bounds its count.
        if claimed:
            self._deferred_cancellations[token] = (capability, tuple(claimed))
        return capability

    def claim_cancel_calls(self, call_ids):
        """Mark ONLY these immutable call IDs; never invoke hooks on this stack.

        Intended lock order is manager -> coordinator. A later revision of the same
        plugin is not selected when this capability is dispatched. Registrations
        after a claim observe the already-set event and invoke their own hook once.
        """
        if (not isinstance(call_ids, (tuple, list, set, frozenset)) or len(call_ids) > self.max_records
                or any(not isinstance(item, str) or not _CALL.fullmatch(item) for item in call_ids)):
            raise ValueError('bounded exact call IDs required')
        with self._lock:
            selected = [self._calls[item] for item in dict.fromkeys(call_ids) if item in self._calls]
            return self._claim_cancellations_locked(selected)

    def dispatch_cancellation(self, capability):
        """Consume an owned claim once, outside ALL manager/coordinator/DB locks.

        False means empty/already dispatched. A forged or foreign capability is
        rejected. An accepted claim cannot be retracted by terminal finalization.
        """
        if type(capability) is not DeferredCancellation or capability._owner is not self:
            raise ExecutionError('foreign_cancellation_claim')
        with self._lock:
            record = self._deferred_cancellations.get(capability._token)
            if record is None:
                return False
            if record[0] is not capability:
                raise ExecutionError('foreign_cancellation_claim')
            self._deferred_cancellations.pop(capability._token)
            claimed = record[1]
        for call, hook in claimed:
            self._invoke_hook(call, hook)
        return True

    def _cancel_matching(self, predicate):
        with self._lock:
            capability = self._claim_cancellations_locked(
                [call for call in self._calls.values() if predicate(call)])
        self.dispatch_cancellation(capability)
        return capability._affected_count

    def cancel_call(self, call_id):
        return self._cancel_matching(lambda c: c.identity.call_id == call_id)

    def cancel_plugin(self, plugin_id):
        return self._cancel_matching(lambda c: c.identity.plugin_id == plugin_id)

    def revoke_authority(self, plugin_id, *, current_trust_fingerprint=None):
        if current_trust_fingerprint is not None and (
                not isinstance(current_trust_fingerprint, str)
                or not _SHA.fullmatch(current_trust_fingerprint)):
            raise ValueError('current authority fingerprint must be SHA256')
        return self._cancel_matching(lambda c: c.identity.plugin_id == plugin_id and (
            current_trust_fingerprint is None
            or c.identity.trust_fingerprint != current_trust_fingerprint))

    def close(self):
        with self._lock:
            self._closed = True
        self._cancel_matching(lambda _: True)
        if self.owns_scheduler:
            self.scheduler.close()

    def execute(self, identity: ExecutionIdentity, *, prepare_after_admission: Callable,
                run: Callable, finish: Callable | None = None,
                final_audit: Callable | None = None, queue_timeout=60,
                resource_cleanup_required: bool = False,
                finalize_gate: Callable | None = None):
        """Synchronous host entry point; callers may invoke it from independent threads.

        prepare(identity) runs after FIFO admission and returns a trusted opaque
        value. run(context, register_cancel_hook) executes WITHOUT manager/global
        coordinator locks. finish(identity, provisional) is pre-final cleanup,
        not a terminal audit: phase='finishing', final=False and candidate_phase/
        candidate_code describe the observed run result before cleanup/cancellation.
        After cleanup, one locked transition fixes the authoritative terminal result
        and rejects later cancellation. final_audit(identity, final_result) runs
        outside the lock AFTER that transition. Its failure only sets audit_failed;
        it cannot change a completed/failed/unknown execution or retry the audit.
        The admission lease covers prepare, run AND finish. Production hosts MUST
        pass resource_cleanup_required=True: finish returns an exact verified
        ResourceCleanupReceipt or the lease stays quarantined until trusted explicit
        release_quarantine. This flag defaults False only for legacy pure callbacks,
        not as a production fallback. finish/hook implementations must themselves
        have bounded cleanup; this module cannot forcibly stop arbitrary Python code.
        Accepted hooks may still be in progress at return; their resource reservation
        stays occupied. We never wait/join them or automatically retry unknown cleanup.

        Optional trusted finalize_gate(identity, candidate, finalize_once) runs WITHOUT
        this lock, after finish but while the lease is still held. It is host-only,
        never a package RPC. The candidate contains metadata, not the mutable output.
        Under its manager lock the host must recheck the COMPLETE live identity,
        enabled/trust state and then call finalize_once(authorized=exact_bool).
        Lock order: external gate -> manager lock -> coordinator lock. Never acquire
        the manager lock while holding this coordinator's lock. Example::

            def gate(identity, candidate, finalize_once):
                with manager_lock:
                    finalize_once(authorized=live_identity_is_current(identity))

        The finalizer is valid only on this execute thread DURING this callback;
        repeated/cross-thread/late/invalid verdict attempts return False without effect.
        True preserves the existing run/cleanup outcome, never promotes a failure;
        False suppresses output as unknown. No invocation or an exception before it
        finalizes fails closed. An exception AFTER finalization sets only metadata,
        never rewrites the committed terminal result. Gates and hooks must be bounded
        by trusted callers: this module cannot forcibly stop a hung Python callback.
        The caller must not synchronously invoke foreign cancellation hooks while its
        manager lock is held. final_audit still runs once, outside these locks.
        """
        if type(identity) is not ExecutionIdentity:
            raise TypeError('immutable host execution identity required')
        if type(resource_cleanup_required) is not bool:
            raise TypeError('resource cleanup mode must be explicit bool')
        if (not callable(prepare_after_admission) or not callable(run)
                or (finish is not None and not callable(finish))
                or (final_audit is not None and not callable(final_audit))
                or (finalize_gate is not None and not callable(finalize_gate))):
            raise TypeError('trusted prepare/run/optional finish/final_audit/finalize_gate callbacks required')
        with self._lock:
            if self._closed:
                raise ExecutionError('coordinator_closed')
            if identity.plugin_id in self._mutating:
                raise ExecutionError('plugin_mutating')
            if identity.call_id in self._calls:
                raise ExecutionError('duplicate_call')
            if len(self._calls) >= self.max_records:
                raise ExecutionError('execution_registry_full')
            call = _Call(identity, threading.Event(), resource_cleanup_required=resource_cleanup_required)
            self._calls[identity.call_id] = call
        phase, code, output = 'failed', 'not_started', None
        try:
            lease = self.scheduler.acquire_lease(identity.call_id, identity.plugin_id,
                                                 cancel=call.cancel_event, queue_timeout=queue_timeout)
            with self._lock:
                lease.cancel_event=call.cancel_event
                call.lease, call.resources_active = lease, True
                if call.cancel_event.is_set():
                    raise ExecutionError('cancelled_before_prepare')
                call.phase = 'admitted'
            prepared = prepare_after_admission(identity)
            with self._lock:
                if call.cancel_event.is_set():
                    raise ExecutionError('cancelled_before_run')
                call.phase = 'running'
                call.running = True
                call.started = True
            try:
                output = run(ExecutionContext(identity, call.cancel_event, prepared, lease),
                             lambda hook: self._register_hook(call, hook))
                phase, code = 'completed', 'completed'
            except BaseException:
                phase, code, output = 'unknown', 'run_failed_result_unknown', None
            finally:
                with self._lock:
                    call.running = False
        except AdmissionError as error:
            phase, code = 'failed', error.code
        except ExecutionError as error:
            phase, code = 'failed', error.code
        except BaseException:
            phase, code = ('unknown', 'run_failed_result_unknown') if call.started else ('failed', 'prepare_failed')
            output = None
        finally:
            with self._lock:
                if call.cancel_event.is_set() and call.started:
                    phase, code, output = 'unknown', 'cancelled_result_unknown', None
                call.running = False
                call.phase, call.code = 'finishing', 'cleanup_pending'
                provisional = ExecutionResult(
                    identity, 'finishing', 'cleanup_pending', output,
                    cancellation_hook_failed=call.hook_failed, final=False,
                    candidate_phase=phase, candidate_code=code,
                    cancellation_hook_pending=call.hook_invoked and not call.hook_completed,
                    resources_pending=call.resources_active)
            finish_failed = False
            receipt = None
            if finish is not None:
                try:
                    receipt = finish(identity, provisional)
                except BaseException:
                    finish_failed = True
            result = None

            def terminal_locked(*, authorized=True, gate_failed=False):
                # Manager lock may surround this coordinator transition, but this
                # function itself never calls any manager/foreign callback.
                nonlocal result
                terminal_phase, terminal_code, terminal_output = phase, code, output
                if finish_failed:
                    terminal_phase, terminal_code, terminal_output = 'unknown', 'finish_failed_result_unknown', None
                if not authorized:
                    terminal_phase, terminal_code, terminal_output = 'unknown', 'final_binding_changed', None
                if gate_failed:
                    terminal_phase, terminal_code, terminal_output = 'unknown', 'final_gate_failed', None
                if call.cancel_event.is_set() and call.started:
                    terminal_phase, terminal_code, terminal_output = 'unknown', 'cancelled_result_unknown', None
                # Legacy/no-gate precedence stays unchanged. A supplied gate's
                # denial/failure otherwise remains explicit, except cancellation
                # and resource quarantine which must always win publication.
                if finish_failed and finalize_gate is None:
                    terminal_phase, terminal_code, terminal_output = 'unknown', 'finish_failed_result_unknown', None
                call.cleanup_finished = True
                hook_pending = call.hook_invoked and not call.hook_completed
                cleanup_unknown = (call.resources_active and resource_cleanup_required
                                   and (finish_failed or not self._cleanup_verified(call, receipt)
                                        or hook_pending))
                if cleanup_unknown or call.resources_active and hook_pending:
                    call.quarantined = True
                    call.lease.quarantine()
                    if resource_cleanup_required:
                        terminal_phase, terminal_code, terminal_output = 'unknown', 'resource_cleanup_unverified', None
                elif call.resources_active:
                    self._release_resources_locked(call)
                call.phase, call.code, call.finalized = terminal_phase, terminal_code, True
                call.final_gate_failed = gate_failed
                call.hook = None  # Claimed hooks remain owned by their invoker.
                result = ExecutionResult(
                    identity, terminal_phase, terminal_code, terminal_output,
                    cancellation_hook_failed=call.hook_failed,
                    finish_failed=finish_failed, final=True,
                    cancellation_hook_pending=call.hook_invoked and not call.hook_completed,
                    resources_pending=call.resources_active, quarantined=call.quarantined,
                    final_gate_failed=gate_failed)

            if finalize_gate is None:
                with self._lock:
                    terminal_locked()
            else:
                owner = threading.current_thread()
                gate_active, finalizer_attempted = True, False

                def finalize_once(*, authorized):
                    nonlocal finalizer_attempted
                    with self._lock:
                        if (threading.current_thread() is not owner or not gate_active
                                or finalizer_attempted or call.finalized):
                            return False
                        finalizer_attempted = True
                        if type(authorized) is not bool:
                            return False
                        terminal_locked(authorized=authorized)
                        return True

                with self._lock:
                    # Metadata only, never a mutable run-output reference. Cleanup
                    # and lease ownership remain reserved until finalize_once.
                    candidate_phase, candidate_code = phase, code
                    if call.cancel_event.is_set() and call.started:
                        candidate_phase, candidate_code = 'unknown', 'cancelled_result_unknown'
                    if finish_failed:
                        candidate_phase, candidate_code = 'unknown', 'finish_failed_result_unknown'
                    if (call.resources_active and resource_cleanup_required
                            and (finish_failed or not self._cleanup_verified(call, receipt)
                                 or call.hook_invoked and not call.hook_completed)):
                        candidate_phase, candidate_code = 'unknown', 'resource_cleanup_unverified'
                    candidate = ExecutionResult(
                        identity, 'finishing', 'final_gate_pending', final=False,
                        candidate_phase=candidate_phase, candidate_code=candidate_code,
                        finish_failed=finish_failed, cancellation_hook_failed=call.hook_failed,
                        cancellation_hook_pending=call.hook_invoked and not call.hook_completed,
                        resources_pending=call.resources_active)
                gate_threw = False
                try:
                    finalize_gate(identity, candidate, finalize_once)
                except BaseException:
                    gate_threw = True
                finally:
                    with self._lock:
                        gate_active = False
                        if not call.finalized:
                            terminal_locked(gate_failed=True)
                        elif gate_threw:
                            # A callback error cannot rewrite an already committed
                            # result, just like an authoritative audit failure.
                            call.final_gate_failed = True
                            result = replace(result, final_gate_failed=True)
            if final_audit is not None:
                try:
                    final_audit(identity, result)
                except BaseException:
                    # Audit failure is not an execution failure. In particular an
                    # audit that already observed success must not make our return
                    # invent an unknown execution after the terminal boundary.
                    with self._lock:
                        call.audit_failed = True
                    result = replace(result, audit_failed=True)
        return result
