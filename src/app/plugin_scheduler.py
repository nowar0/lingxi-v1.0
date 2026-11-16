"""Bounded FIFO plugin-call admission; not an OS sandbox or execution adapter.

Each admitted call still needs its own Job, OS identity and one-use approvals.
This scheduler never starts, retries, terminates or transfers authorization itself.
"""
from collections import deque
from contextlib import contextmanager
import math
import threading
import time


class AdmissionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SchedulerLease:
    """Host-owned reservation. A quarantined lease still consumes admission capacity.

    This is accounting, not proof that an OS resource is gone. Only the owner that
    received this object may explicitly release it after its cleanup contract.
    """
    def __init__(self, scheduler, call_id, plugin_id):
        self._scheduler = scheduler
        self.call_id, self.plugin_id = call_id, plugin_id
        self._released = False
        self._quarantined = False

    def quarantine(self):
        with self._scheduler._condition:
            if self._released or self._scheduler._running.get(self.call_id) is not self:
                raise AdmissionError('lease_not_active', '调用资源预约不再有效')
            self._quarantined = True
            self._scheduler._condition.notify_all()

    def release(self):
        """Idempotent exact-owner release, including after scheduler.close()."""
        with self._scheduler._condition:
            if self._released:
                return False
            if self._scheduler._running.get(self.call_id) is not self:
                raise AdmissionError('lease_not_owner', '调用资源预约所有权不匹配')
            del self._scheduler._running[self.call_id]
            self._released = True
            self._scheduler._condition.notify_all()
            return True


class PluginCallScheduler:
    """Default ten concurrent calls; bounded waiters and explicit cancellation."""
    def __init__(self, *, max_concurrent=10, max_queued=100, clock=time.monotonic):
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 10:
            raise ValueError('max_concurrent must be 1..10; product-wide ceiling is ten')
        if type(max_queued) is not int or not 1 <= max_queued <= 4096:
            raise ValueError('max_queued must be 1..4096')
        self.max_concurrent, self.max_queued, self.clock = max_concurrent, max_queued, clock
        self._condition = threading.Condition(threading.RLock())
        self._queue, self._running = deque(), {}
        self._closed = False

    def snapshot(self):
        with self._condition:
            return {'max_concurrent': self.max_concurrent, 'max_queued': self.max_queued,
                    'running': len(self._running), 'queued': len(self._queue),
                    'closed': self._closed,
                    'quarantined': sum(lease._quarantined for lease in self._running.values())}

    def close(self):
        """Reject/wake waiters; the runtime separately cancels each running Job."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _acquire(self, call_id, plugin_id, cancel, queue_timeout):
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
                or not isinstance(plugin_id, str) or not 1 <= len(plugin_id) <= 200):
            raise ValueError('bounded explicit call/plugin identity required')
        if type(queue_timeout) not in (int, float) or not math.isfinite(queue_timeout) or not 0 < queue_timeout <= 600:
            raise ValueError('queue_timeout must be >0 and <=600')
        deadline = self.clock() + queue_timeout
        with self._condition:
            if self._closed:
                raise AdmissionError('scheduler_closed', '插件调度已停止')
            if call_id in self._running or any(item[0] == call_id for item in self._queue):
                raise AdmissionError('duplicate_call', '调用已运行或排队，不重复提交')
            if len(self._queue) >= self.max_queued:
                raise AdmissionError('queue_full', '插件排队容量已满，未执行')
            ticket = (call_id, plugin_id)
            self._queue.append(ticket)
            self._condition.notify_all()
            try:
                while True:
                    if self._closed:
                        raise AdmissionError('scheduler_closed', '插件调度已停止，排队未执行')
                    if cancel is not None and cancel.is_set():
                        raise AdmissionError('cancelled', '排队已取消，未执行')
                    if self.clock() >= deadline:
                        raise AdmissionError('queue_timeout', '排队等待超时，未执行，不自动重试')
                    if self._queue[0] is ticket and len(self._running) < self.max_concurrent:
                        self._queue.popleft()
                        lease = SchedulerLease(self, call_id, plugin_id)
                        self._running[call_id] = lease
                        self._condition.notify_all()
                        return lease
                    self._condition.wait(timeout=min(.05, max(.001, deadline-self.clock())))
            finally:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._condition.notify_all()

    def acquire_lease(self, call_id, plugin_id, *, cancel=None, queue_timeout=60):
        """Explicit owner lifetime for resources that can outlive a callback/stack."""
        return self._acquire(call_id, plugin_id, cancel, queue_timeout)

    @contextmanager
    def slot(self, call_id, plugin_id, *, cancel=None, queue_timeout=60):
        """Synthetic lexical lifetime; resource owners must use acquire_lease()."""
        lease = self.acquire_lease(call_id, plugin_id, cancel=cancel, queue_timeout=queue_timeout)
        try:
            yield lease
        finally:
            lease.release()
