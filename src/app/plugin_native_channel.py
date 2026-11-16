"""Owned anonymous one-shot channels with bounded native IO, not OS attestation.

Import/construct/plan perform no native calls. Real creation requires an exact host
approval, and real execution has NOT been exercised by this module's Fake API tests.
No named listener, network, ACL, token or AppContainer provisioning exists here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import math
import os
import threading
import time

from app.plugin_transport import (BoundedChannelIO, MAX_DIAGNOSTIC_BYTES,
                                  MAX_TOTAL_BYTES, ProcessCompletion, TransportError)

LAUNCH_HANDLE_LOCK = threading.Lock()
_CHILD_ROLES = ('request_read', 'result_write', 'diagnostics_write')
_MCP_CHILD_ROLES = ('stdin_read', 'stdout_write', 'diagnostics_write',
                    'proxy_request_read', 'proxy_result_write')


@dataclass(frozen=True)
class ChannelCreationPlan:
    call_id: str
    fingerprint: str
    mode: str = 'normal'


def plan_channel_creation(call_id, *, mode='normal'):
    import re
    if not isinstance(call_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}', call_id):
        raise TransportError('channel_call_id')
    if mode not in ('normal', 'stdio_mcp'):
        raise TransportError('channel_mode')
    roles = _CHILD_ROLES if mode == 'normal' else _MCP_CHILD_ROLES
    raw = json.dumps({'v':1, 'call_id':call_id, 'pipes':roles}, sort_keys=True).encode()
    return ChannelCreationPlan(call_id, hashlib.sha256(raw).hexdigest(), mode)


@dataclass(eq=False)
class ChannelHandles:
    """Host-created endpoints; ownership is never reconstructed from package JSON."""
    call_id: str
    request_write: int
    result_read: int
    diagnostics_read: int
    child_ends: tuple[tuple[str, int], ...]
    owned: set[int] = field(repr=False)
    child_closed: bool = False
    launch_lock_held: bool = True
    process_handle: int | None = None
    pid: int | None = None
    cancel_job: object = field(default=None, repr=False)
    writer: object = field(default=None, repr=False)
    diagnostic_total: int = 0
    cleanup_fault: bool = False
    closed: bool = False
    job_cancelled: bool = False
    lock: object = field(default_factory=threading.RLock, repr=False)
    mode: str = 'normal'
    proxy_request_write: int | None = None
    proxy_result_read: int | None = None
    stdin_closed: bool = False

    def host_mapping(self):
        return {'reader':self.result_read, 'writer':self.request_write,
                'diagnostics':self.diagnostics_read}


@dataclass
class _Writer:
    ready: object = field(default_factory=threading.Event)
    done: object = field(default_factory=threading.Event)
    thread: object = None
    native_thread_handle: int | None = None
    count: int | None = None
    error: bool = False


class NativeBoundedIO(BoundedChannelIO):
    def __init__(self, *, api=None, clock=time.monotonic, sleeper=time.sleep, cleanup_timeout=1.0):
        if not callable(clock) or not callable(sleeper) or type(cleanup_timeout) not in (int,float) or not math.isfinite(cleanup_timeout) or not 0 < cleanup_timeout <= 5:
            raise ValueError('explicit bounded IO timing required')
        self._api, self.clock, self.sleep = api, clock, sleeper
        self.cleanup_timeout = cleanup_timeout
        self._states = {}
        self._orphaned = set()
        self._used = set()
        self._lock = threading.RLock()

    @property
    def api(self):
        if self._api is None:
            self._api = NativePipeApi()
        return self._api

    def create_channel(self, plan, *, user_confirmed, fingerprint):
        if type(plan) is not ChannelCreationPlan or plan_channel_creation(plan.call_id, mode=plan.mode) != plan or user_confirmed is not True or fingerprint != plan.fingerprint:
            raise TransportError('channel_creation_approval')
        with self._lock:
            if self._orphaned:
                raise TransportError('channel_cleanup_fault')
            if fingerprint in self._used:
                raise TransportError('channel_creation_consumed')
            if len(self._used) >= 4096:
                raise TransportError('channel_adapter_capacity')
            self._used.add(fingerprint)
        if not LAUNCH_HANDLE_LOCK.acquire(timeout=self.cleanup_timeout):
            raise TransportError('channel_launch_busy')
        owned = set()
        try:
            ends = []
            roles = _MCP_CHILD_ROLES if plan.mode == 'stdio_mcp' else _CHILD_ROLES
            for _ in roles:
                read, write = self.api.create_pipe()
                owned.update((read, write))
                if read == write or read <= 0 or write <= 0:
                    raise TransportError('channel_pipe_identity')
                ends.append((read, write))
            if len(owned) != 2 * len(roles):
                raise TransportError('channel_pipe_identity')
            host = (ends[0][1], ends[1][0], ends[2][0])
            original_child = (ends[0][0], ends[1][1], ends[2][1])
            extra_host = ()
            if plan.mode == 'stdio_mcp':
                original_child += (ends[3][0], ends[4][1])
                extra_host = (ends[3][1], ends[4][0])
            children = []
            for role, handle in zip(roles, original_child):
                duplicate = self.api.duplicate(handle, inheritable=True)
                if duplicate <= 0 or duplicate in owned:
                    raise TransportError('channel_pipe_identity')
                owned.add(duplicate)
                self.api.close(handle); owned.remove(handle)
                children.append((role, duplicate))
            for handle in (*host, *extra_host):
                self.api.verify_pipe(handle, inheritable=False)
            for _, handle in children:
                self.api.verify_pipe(handle, inheritable=True)
            state = ChannelHandles(plan.call_id, *host, tuple(children), owned, mode=plan.mode)
            if extra_host:
                state.proxy_request_write, state.proxy_result_read = extra_host
            with self._lock:
                self._states[id(state)] = state
            return state
        except BaseException:
            failed = set()
            for handle in tuple(owned):
                try:self.api.close(handle)
                except Exception:failed.add(handle)
            LAUNCH_HANDLE_LOCK.release()
            if failed:
                with self._lock:self._orphaned.update(failed)
                raise TransportError('channel_creation_cleanup_unconfirmed') from None
            raise

    def retry_orphan_cleanup(self):
        """Explicit retry of exactly held handles only; no reconstructed/computed closes."""
        with self._lock:
            for handle in tuple(self._orphaned):
                try:self.api.close(handle)
                except Exception:continue
                self._orphaned.remove(handle)
            if self._orphaned:raise TransportError('channel_cleanup_fault')

    def _state(self, state):
        if type(state) is not ChannelHandles or self._states.get(id(state)) is not state or state.closed:
            raise TransportError('channel_ownership')
        if state.cleanup_fault:
            raise TransportError('channel_cleanup_fault')
        return state

    def mark_child_spawned(self, state):
        """Call immediately after CreateProcess returns, even before token verification."""
        state = self._state(state)
        with state.lock:
            if state.child_closed:
                raise TransportError('channel_child_ends_consumed')
            try:
                for _, handle in state.child_ends:
                    if handle in state.owned:
                        self.api.close(handle); state.owned.remove(handle)
                state.child_closed = True
            except Exception:
                state.cleanup_fault = True
                raise TransportError('channel_cleanup_fault') from None
            finally:
                if state.launch_lock_held:
                    LAUNCH_HANDLE_LOCK.release(); state.launch_lock_held = False

    def bind_process(self, state, process_handle, pid, *, cancel_job):
        """The launcher owns its process handle; this adapter owns a private duplicate."""
        state = self._state(state)
        if not state.child_closed or state.process_handle is not None or type(pid) is not int or pid <= 0 or not callable(cancel_job):
            raise TransportError('channel_process_binding')
        if self.api.process_id(process_handle) != pid:
            raise TransportError('channel_process_binding')
        duplicate = self.api.duplicate(process_handle, inheritable=False)
        try:
            if duplicate in state.owned or self.api.process_id(duplicate) != pid:
                raise TransportError('channel_process_binding')
            self.api.verify_noninheritable(duplicate)
            with state.lock:
                state.process_handle, state.pid, state.cancel_job = duplicate, pid, cancel_job
                state.owned.add(duplicate)
        except BaseException:
            try:self.api.close(duplicate)
            except Exception:
                state.owned.add(duplicate);state.cleanup_fault=True
                raise TransportError('channel_cleanup_fault') from None
            raise

    def _bound(self, channel):
        with self._lock:
            values = tuple(self._states.values())
        match = [s for s in values if not s.closed and channel.reader == s.result_read and channel.writer == s.request_write and channel.diagnostics == s.diagnostics_read]
        if len(match) != 1:
            raise TransportError('channel_ownership')
        state = self._state(match[0])
        if (not state.child_closed or state.process_handle is None or state.pid != channel.binding.pid
                or state.call_id != channel.binding.call_id or channel.attestation.binding != channel.binding
                or channel.attestation.inherited_handles != state.child_ends
                or self.api.process_id(state.process_handle) != state.pid):
            raise TransportError('channel_process_binding')
        if state.mode != channel.attestation.mode:
            raise TransportError('channel_mode')
        with state.lock:
            self._state(state)
            handles = [state.result_read, state.diagnostics_read]
            if not state.stdin_closed:
                handles.append(state.request_write)
            if state.mode == 'stdio_mcp':
                handles.extend((state.proxy_request_write, state.proxy_result_read))
            for handle in handles:
                self.api.verify_pipe(handle, inheritable=False)
        return state

    def _check(self, deadline, cancel):
        if type(deadline) not in (int,float) or not math.isfinite(deadline):
            raise TransportError('channel_deadline')
        if cancel.is_set():
            raise InterruptedError('channel_cancelled')
        if self.clock() >= deadline:
            raise TimeoutError('channel_deadline')

    def _poll(self, state, handle, size, *, deadline, cancel):
        with state.lock:
            self._state(state)
            self._check(deadline, cancel)
            # Serialize Peek + Read against other host reads and endpoint closure.
            # Only already-available bytes are read, so there is no blocking reader.
            available, eof = self.api.peek(handle)
            if eof:return b'', True
            if type(available) is not int or available < 0:
                raise TransportError('channel_native_read')
            if available:
                self._check(deadline, cancel)
                data = self.api.read_available(handle, min(size, available, 4096))
                if type(data) is not bytes or not 0 < len(data) <= min(size, available, 4096):
                    raise TransportError('channel_native_read')
                self._check(deadline, cancel)
                return data, False
            # Exited does not itself mean pipe EOF; do not fabricate EOF or exit=0.
            self.api.process_exited(state.process_handle)
            return b'', False

    def read(self, channel, size, *, deadline, cancel):
        if type(size) is not int or not 1 <= size <= MAX_TOTAL_BYTES:
            raise TransportError('channel_read_size')
        state = self._bound(channel)
        while True:
            data, eof = self._poll(state, state.result_read, size, deadline=deadline, cancel=cancel)
            if data or eof:return data
            self.sleep(min(.01, max(0, deadline-self.clock())))

    def read_diagnostics(self, channel, size, *, deadline, cancel):
        if type(size) is not int or not 1 <= size <= MAX_DIAGNOSTIC_BYTES + 1:
            raise TransportError('channel_diagnostic_size')
        state = self._bound(channel)
        data, _ = self._poll(state, state.diagnostics_read, size, deadline=deadline, cancel=cancel)
        state.diagnostic_total += len(data)
        if state.diagnostic_total > MAX_DIAGNOSTIC_BYTES:
            raise TransportError('diagnostic_budget')
        return data

    def _cancel_writer(self, state, writer):
        # Kill the Job first to remove the blocked child, then cancel only our writer.
        if not state.job_cancelled:
            try:state.cancel_job()
            except Exception:state.cleanup_fault = True
            state.job_cancelled = True
        cleanup_deadline = time.monotonic() + self.cleanup_timeout
        if writer.ready.wait(max(0, cleanup_deadline-time.monotonic())) and writer.native_thread_handle:
            try:self.api.cancel_synchronous(writer.native_thread_handle)
            except Exception:state.cleanup_fault = True
        writer.thread.join(max(0, cleanup_deadline-time.monotonic()))
        if writer.thread.is_alive():
            state.cleanup_fault = True
            raise TransportError('channel_writer_cleanup_unconfirmed')

    def write(self, channel, data, *, deadline, cancel):
        state = self._bound(channel)
        if state.stdin_closed:
            raise TransportError('channel_stdin_closed')
        return self._write_handle(state, state.request_write, data, deadline=deadline, cancel=cancel)

    def _write_handle(self, state, handle, data, *, deadline, cancel):
        self._check(deadline, cancel)
        if type(data) is not bytes or not data or len(data) > MAX_TOTAL_BYTES:
            raise TransportError('channel_write_size')
        writer = _Writer()
        with state.lock:
            if state.writer is not None:
                raise TransportError('channel_writer_busy')
            state.writer = writer
        def work():
            try:
                writer.native_thread_handle = self.api.open_current_thread()
                writer.ready.set()
                writer.count = self.api.write(handle, data[:4096])
            except Exception:
                writer.error = True
            finally:
                writer.ready.set(); writer.done.set()
        writer.thread = threading.Thread(target=work, name='lingxi-native-pipe-write', daemon=False)
        failure = None
        try:
            writer.thread.start()
            while not writer.done.wait(.01):
                self._check(deadline, cancel)
            writer.thread.join(self.cleanup_timeout)
            if writer.thread.is_alive():
                state.cleanup_fault = True
                raise TransportError('channel_writer_cleanup_unconfirmed')
            self._check(deadline, cancel)
            if writer.error or type(writer.count) is not int or not 0 < writer.count <= min(len(data),4096):
                raise TransportError('channel_native_write')
            return writer.count
        except BaseException as error:
            failure = error
            if writer.thread.ident is not None:
                self._cancel_writer(state, writer)
            raise
        finally:
            if writer.thread.ident is not None:
                writer.thread.join(0)
            if writer.thread.is_alive():
                state.cleanup_fault = True
                # Retain both the thread handle and pipe ownership until bounded cleanup.
            else:
                if writer.native_thread_handle:
                    try:self.api.close(writer.native_thread_handle)
                    except Exception:
                        state.cleanup_fault = True
                        # Retain the writer record and owned native thread handle for
                        # an explicit cleanup retry; never silently forget a leak.
                        if failure is None:raise TransportError('channel_cleanup_fault') from None
                    else:
                        writer.native_thread_handle = None
                if writer.native_thread_handle is None:
                    with state.lock:state.writer = None

    def poll_mcp(self, channel, stream, size, *, deadline, cancel):
        state = self._bound(channel)
        if state.mode != 'stdio_mcp' or type(size) is not int or not 1 <= size <= 4096:
            raise TransportError('mcp_channel_shape')
        handles = {'stdout':state.result_read, 'proxy':state.proxy_result_read,
                   'diagnostics':state.diagnostics_read}
        if stream not in handles:
            raise TransportError('mcp_channel_stream')
        data, eof = self._poll(state, handles[stream], size, deadline=deadline, cancel=cancel)
        if stream == 'diagnostics':
            state.diagnostic_total += len(data)
            if state.diagnostic_total > MAX_DIAGNOSTIC_BYTES:
                raise TransportError('diagnostic_budget')
        return data, eof

    def write_mcp(self, channel, stream, data, *, deadline, cancel):
        state = self._bound(channel)
        if state.mode != 'stdio_mcp' or stream not in ('stdin','proxy'):
            raise TransportError('mcp_channel_shape')
        if stream == 'stdin' and state.stdin_closed:
            raise TransportError('channel_stdin_closed')
        handle = state.request_write if stream == 'stdin' else state.proxy_request_write
        return self._write_handle(state, handle, data, deadline=deadline, cancel=cancel)

    def close_mcp_stdin(self, channel):
        state = self._bound(channel)
        if state.mode != 'stdio_mcp':
            raise TransportError('mcp_channel_shape')
        with state.lock:
            if state.stdin_closed:
                raise TransportError('channel_stdin_consumed')
            if state.writer is not None:
                raise TransportError('channel_writer_busy')
            self.api.close(state.request_write)
            state.owned.remove(state.request_write)
            state.stdin_closed = True

    def mcp_exit_code(self, channel):
        state = self._bound(channel)
        if state.mode != 'stdio_mcp':
            raise TransportError('mcp_channel_shape')
        if self.api.process_exited(state.process_handle) is not True:
            return None
        code = self.api.exit_code(state.process_handle)
        if type(code) is not int or code == 259:
            raise TransportError('process_exit_unverified')
        return code

    def complete_exit(self, channel, *, deadline, cancel):
        state = self._bound(channel)
        stdout_eof = diagnostics_eof = False
        trailing = diagnostics = 0
        while True:
            self._check(deadline, cancel)
            if not stdout_eof:
                data, stdout_eof = self._poll(state, state.result_read, 4096, deadline=deadline, cancel=cancel)
                trailing += len(data)
                if trailing > MAX_TOTAL_BYTES:
                    raise TransportError('worker_trailing_output')
            if not diagnostics_eof:
                data, diagnostics_eof = self._poll(state, state.diagnostics_read, min(4096, MAX_DIAGNOSTIC_BYTES-state.diagnostic_total+1), deadline=deadline, cancel=cancel)
                diagnostics += len(data); state.diagnostic_total += len(data)
                if state.diagnostic_total > MAX_DIAGNOSTIC_BYTES:
                    raise TransportError('diagnostic_budget')
            exited = self.api.process_exited(state.process_handle)
            if exited and stdout_eof and diagnostics_eof:
                code = self.api.exit_code(state.process_handle)
                if type(code) is not int or code == 259:
                    raise TransportError('process_exit_unverified')
                return ProcessCompletion(returncode=code, exit_verified=True,
                    stdout_eof_verified=True, no_trailing_bytes=trailing == 0,
                    diagnostics_drained=True, diagnostic_bytes=diagnostics, trailing_bytes=trailing)
            self.sleep(min(.01, max(0, deadline-self.clock())))

    def close(self, state):
        if type(state) is not ChannelHandles or self._states.get(id(state)) is not state:
            raise TransportError('channel_ownership')
        with state.lock:
            if state.closed:return
            if state.writer is not None:
                writer = state.writer
                self._cancel_writer(state, writer)
                if writer.native_thread_handle:
                    self.api.close(writer.native_thread_handle)
                state.writer = None
            errors = []
            for handle in tuple(state.owned):
                try:self.api.close(handle); state.owned.remove(handle)
                except Exception:errors.append(handle)
            if state.launch_lock_held:
                LAUNCH_HANDLE_LOCK.release(); state.launch_lock_held = False
            if errors:
                state.cleanup_fault = True
                raise TransportError('channel_cleanup_fault')
            state.closed = True


class NativePipeApi:
    """Lazy ctypes implementation. Unit tests use fakes, never instantiate this class."""
    def __init__(self):
        if os.name != 'nt':raise TransportError('windows_required')
        k = self.k = C.WinDLL('kernel32', use_last_error=True)
        specs = {
            'CreatePipe':([C.POINTER(W.HANDLE),C.POINTER(W.HANDLE),C.c_void_p,W.DWORD],W.BOOL),
            'DuplicateHandle':([W.HANDLE,W.HANDLE,W.HANDLE,C.POINTER(W.HANDLE),W.DWORD,W.BOOL,W.DWORD],W.BOOL),
            'GetCurrentProcess':([],W.HANDLE), 'CloseHandle':([W.HANDLE],W.BOOL),
            'GetHandleInformation':([W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
            'GetFileType':([W.HANDLE],W.DWORD),
            'PeekNamedPipe':([W.HANDLE,C.c_void_p,W.DWORD,C.c_void_p,C.POINTER(W.DWORD),C.c_void_p],W.BOOL),
            'ReadFile':([W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p],W.BOOL),
            'WriteFile':([W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p],W.BOOL),
            'GetProcessId':([W.HANDLE],W.DWORD), 'WaitForSingleObject':([W.HANDLE,W.DWORD],W.DWORD),
            'GetExitCodeProcess':([W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
            'GetCurrentThreadId':([],W.DWORD), 'OpenThread':([W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
            'CancelSynchronousIo':([W.HANDLE],W.BOOL),
        }
        for name,(args,result) in specs.items():
            function=getattr(k,name);function.argtypes=args;function.restype=result

    def create_pipe(self):
        read,write=W.HANDLE(),W.HANDLE()
        if not self.k.CreatePipe(C.byref(read),C.byref(write),None,4096):raise TransportError('native_create_pipe')
        return read.value,write.value

    def duplicate(self, handle, *, inheritable):
        current=self.k.GetCurrentProcess();duplicate=W.HANDLE()
        if not self.k.DuplicateHandle(current,handle,current,C.byref(duplicate),0,inheritable,2):raise TransportError('native_duplicate_handle')
        return duplicate.value

    def verify_noninheritable(self, handle):
        flags=W.DWORD()
        if not self.k.GetHandleInformation(handle,C.byref(flags)) or flags.value & 1:raise TransportError('native_handle_inheritance')

    def verify_pipe(self, handle, *, inheritable):
        flags=W.DWORD()
        if not self.k.GetHandleInformation(handle,C.byref(flags)) or bool(flags.value & 1) != inheritable or self.k.GetFileType(handle) != 3:
            raise TransportError('native_pipe_identity')

    def close(self, handle):
        if not self.k.CloseHandle(handle):raise TransportError('native_close_handle')

    def peek(self, handle):
        available=W.DWORD()
        if not self.k.PeekNamedPipe(handle,None,0,None,C.byref(available),None):
            if C.get_last_error() == 109:return 0,True
            raise TransportError('native_peek_pipe')
        return available.value,False

    def read_available(self, handle, size):
        buffer=C.create_string_buffer(size);count=W.DWORD()
        if not self.k.ReadFile(handle,buffer,size,C.byref(count),None):raise TransportError('native_read_pipe')
        return buffer.raw[:count.value]

    def write(self, handle, data):
        buffer=C.create_string_buffer(data);count=W.DWORD()
        if not self.k.WriteFile(handle,buffer,len(data),C.byref(count),None):raise TransportError('native_write_pipe')
        return count.value

    def process_id(self, handle):
        pid=self.k.GetProcessId(handle)
        if not pid:raise TransportError('native_process_identity')
        return pid

    def process_exited(self, handle):
        result=self.k.WaitForSingleObject(handle,0)
        if result not in (0,258):raise TransportError('native_process_wait')
        return result == 0

    def exit_code(self, handle):
        code=W.DWORD()
        if not self.k.GetExitCodeProcess(handle,C.byref(code)):raise TransportError('native_process_exit')
        return code.value

    def open_current_thread(self):
        handle=self.k.OpenThread(1,False,self.k.GetCurrentThreadId())
        if not handle:raise TransportError('native_writer_thread')
        return handle

    def cancel_synchronous(self, handle):
        if not self.k.CancelSynchronousIo(handle) and C.get_last_error() != 1168:
            raise TransportError('native_cancel_writer')
