"""Runtime-only plugin pending model registry; no HTTP identity registration.

Use the actual verified BrokerSession and the invocation's already-running deadline
and cancellation Event. This module never creates a PID, launches a child, extends
a budget, mints OS evidence, reads credentials or sends a model request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import secrets
import threading
import time

from app.model_submission import CompiledSubmission
from app.plugin_model_api import HostModelExecution, HostPendingModelRequest
from app.plugin_proxy import BrokerSession

MAX_RECORDS = 128
MAX_BYTES = 256 * 1024**2
BUDGETS = {'ordinary':10, 'mcp':30}


class PendingModelError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise PendingModelError(code)


@dataclass(frozen=True)
class RuntimeModelInvocation:
    """Trusted runtime observation, not a package/browser request DTO.

    live_invocation(binding) must freshly return this exact observation only while
    its native process/channel, manager grant, authenticated owner and scheduler
    invocation remain valid. A Python source label is not an OS attestation.
    """
    binding: BrokerSession = field(repr=False)
    owner_session_id: str = field(repr=False)
    deadline: float
    cancel: threading.Event = field(repr=False)
    mode: str = 'ordinary'

    def __post_init__(self):
        if (type(self.binding) is not BrokerSession or type(self.owner_session_id) is not str
                or not 16 <= len(self.owner_session_id) <= 128
                or any(not (char.isascii() and (char.isalnum() or char in '_-')) for char in self.owner_session_id)
                or type(self.deadline) not in (int,float) or not math.isfinite(self.deadline)
                or not isinstance(self.cancel, threading.Event) or self.mode not in BUDGETS):
            raise ValueError('actual host runtime invocation required')


@dataclass
class _Pending:
    pending: HostPendingModelRequest = field(repr=False)
    invocation: RuntimeModelInvocation = field(repr=False)
    expires: float
    size: int
    materials_revalidate: object = field(repr=False)
    execution_claimed: bool = False
    revoked: bool = False


class RuntimePendingModels:
    """Production host-owned storage for HostModelApprovals provider callbacks.

    No default/fake live provider is available. register() is a Python host method,
    never expose it to browser or plugin IPC. The real runtime must call revoke on
    cancellation, process/channel cleanup, trust/config changes or owner logout.
    live_invocation is checked at every lookup too, so missed notifications fail
    closed. Registration never allocates a new cancellation Event or time budget.
    """
    def __init__(self, *, live_invocation, clock=time.monotonic, max_records=MAX_RECORDS,
                 max_bytes=MAX_BYTES, ttl=60):
        if not callable(live_invocation) or not callable(clock):
            raise TypeError('actual live runtime invocation provider required')
        if (type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS
                or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES
                or type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0 < ttl <= 60):
            raise ValueError('bounded runtime pending policy required')
        self.live_invocation, self.clock = live_invocation, clock
        self.maximum, self.byte_limit, self.ttl = max_records, max_bytes, ttl
        self._records = {}
        self._used_bindings = set()
        self._bytes = 0
        self._closed = False
        self._lock = threading.RLock()

    def _observe(self, expected):
        if self._closed or type(expected) is not RuntimeModelInvocation or expected.cancel.is_set():
            _fail('model_runtime_invocation_cancelled_or_closed')
        try:
            actual = self.live_invocation(expected.binding)
        except Exception:
            _fail('model_runtime_invocation_unavailable')
        if (type(actual) is not RuntimeModelInvocation or actual != expected
                or actual.cancel is not expected.cancel or actual.cancel.is_set()
                or not self.clock() < actual.deadline <= self.clock()+BUDGETS[actual.mode]):
            _fail('model_runtime_invocation_changed_or_expired')
        return actual

    def register(self, invocation, compiled, *, materials_revalidate):
        """Register an exact already-compiled host payload after real runtime binding.

        materials_revalidate(compiled)->True must check the held actual source/input
        descriptors, selection and host configuration. Serialized verified flags do
        not satisfy this callback. It is required even for text-only submissions.
        """
        if type(invocation) is not RuntimeModelInvocation or type(compiled) is not CompiledSubmission:
            _fail('model_runtime_typed_invocation_and_submission_required')
        if not callable(materials_revalidate):
            raise TypeError('actual host material/configuration revalidator required')
        with self._lock:
            self._observe(invocation)
            now = self.clock()
            if not now < invocation.deadline <= now+BUDGETS[invocation.mode]:
                _fail('model_runtime_actual_budget_required')
            try:
                if materials_revalidate(compiled) is not True:
                    _fail('model_runtime_materials_changed')
            except PendingModelError:
                raise
            except Exception:
                _fail('model_runtime_materials_unavailable')
            self._observe(invocation)
            self._purge()
            if invocation.binding in self._used_bindings:
                _fail('model_runtime_invocation_already_registered')
            if len(self._used_bindings) >= 4096:
                _fail('model_runtime_lifetime_capacity')
            size = sum(len(item) for item in (compiled.payload_bytes,
                compiled.submission_manifest_bytes, compiled.preview_content_bytes))
            if len(self._records) >= self.maximum or self._bytes+size > self.byte_limit:
                _fail('model_runtime_pending_capacity')
            request_id = secrets.token_urlsafe(32)
            while request_id in self._records:
                request_id = secrets.token_urlsafe(32)
            pending = HostPendingModelRequest(request_id, invocation.binding, compiled,
                                               invocation.owner_session_id)
            self._records[request_id] = _Pending(pending, invocation,
                min(invocation.deadline, now+self.ttl), size, materials_revalidate)
            self._used_bindings.add(invocation.binding)
            self._bytes += size
            return {'request_id':request_id, 'owner_session_id':invocation.owner_session_id,
                'submission_sha256':compiled.payload_sha256,
                'submission_fingerprint':compiled.fingerprint, 'approval_required':True,
                'sent':False, 'expires_in':min(invocation.deadline-now, self.ttl)}

    def _purge(self):
        now = self.clock()
        for request_id, record in tuple(self._records.items()):
            if record.expires <= now or record.revoked:
                self._records.pop(request_id)
                self._bytes -= record.size

    def _record(self, request_id):
        if self._closed or type(request_id) is not str:
            _fail('model_runtime_pending_unavailable')
        record = self._records.get(request_id)
        if record is None or record.revoked or self.clock() >= record.expires:
            _fail('model_runtime_pending_unavailable')
        try:
            self._observe(record.invocation)
        except PendingModelError:
            record.revoked = True
            raise
        try:
            if record.materials_revalidate(record.pending.compiled) is not True:
                _fail('model_runtime_materials_changed')
        except PendingModelError:
            record.revoked = True
            raise
        except Exception:
            record.revoked = True
            _fail('model_runtime_materials_unavailable')
        try:
            self._observe(record.invocation)
        except PendingModelError:
            record.revoked = True
            raise
        return record

    def pending(self, request_id):
        """HostModelApprovals.pending_provider; never trusts an HTTP payload."""
        with self._lock:
            return self._record(request_id).pending

    def execution_context(self, pending):
        """Claim the real invocation context once, without creating/extending time."""
        if type(pending) is not HostPendingModelRequest:
            _fail('model_runtime_exact_pending_required')
        with self._lock:
            record = self._record(pending.request_id)
            if record.pending != pending or record.execution_claimed:
                _fail('model_runtime_execution_already_claimed_or_changed')
            record.execution_claimed = True
            invocation = self._observe(record.invocation)
            return HostModelExecution(pending.request_id, invocation.binding,
                invocation.owner_session_id, invocation.deadline, invocation.cancel,
                invocation.mode, source='host_runtime')

    def live_binding(self, binding):
        """ModelProxyService.authority_provider hook; actual invocation only."""
        with self._lock:
            matches = [record for record in self._records.values()
                       if not record.revoked and record.invocation.binding == binding]
            if len(matches) != 1:
                _fail('model_runtime_actual_binding_unavailable')
            return self._record(matches[0].pending.request_id).invocation.binding

    def route_provider(self, binding, profile_id):
        """Pinned payload route, freshly source/config revalidated by the host."""
        with self._lock:
            matches = [record for record in self._records.values()
                       if not record.revoked and record.invocation.binding == binding
                       and record.pending.compiled.route.profile_id == profile_id]
            if len(matches) != 1:
                _fail('model_runtime_route_unavailable')
            return self._record(matches[0].pending.request_id).pending.compiled.route

    def revoke(self, request_id):
        """Called by real runtime cleanup/change notification; never undo external IO."""
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                return False
            record.revoked = True
            record.invocation.cancel.set()
            return True

    def revoke_binding(self, binding, *, cancel_invocation=True):
        with self._lock:
            count = 0
            for record in self._records.values():
                if record.invocation.binding == binding:
                    record.revoked = True
                    if cancel_invocation:record.invocation.cancel.set()
                    count += 1
            return count

    def close(self):
        with self._lock:
            self._closed = True
            for record in self._records.values():
                record.revoked = True
                record.invocation.cancel.set()
            self._records.clear(); self._used_bindings.clear(); self._bytes = 0
