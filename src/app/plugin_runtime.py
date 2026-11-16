"""Bounded subprocess protocol and best-effort process-tree cleanup, not a sandbox.

Only a trusted host may select worker_command. Arbitrary Python retains the OS user's
rights and can make direct network/file/credential calls regardless of declarations.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class RuntimeFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def python_worker_command(mode):
    if mode not in {'plugin_worker', 'package_mcp_worker'}:
        raise ValueError('unknown worker')
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--' + mode.replace('_', '-')]
    source = Path(__file__).resolve().parent.parent
    paths = [str(source)] + [str(Path(value).resolve()) for value in sys.path
                            if value and 'site-packages' in Path(value).parts and Path(value).is_dir()]
    return [sys.executable, '-I', '-B', '-u', str(Path(__file__).with_name('plugin_worker_bootstrap.py')),
            mode, json.dumps(list(dict.fromkeys(paths)))]


class _WindowsJob:
    """Kill-on-close Job, assigned before any plugin input is sent; fail closed."""
    def __init__(self, process):
        from ctypes import wintypes
        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(x, ctypes.c_uint64) for x in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                or not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process._handle))):
            self.close()
            raise RuntimeFailure("process_control_unavailable", "无法建立 Windows 进程树清理 Job；拒绝执行")

    def close(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class PluginRuntime:
    def __init__(self, worker_command=None, timeout=10.0, max_output=262_144, max_input=1_048_576):
        self.worker_command = list(worker_command) if worker_command else python_worker_command('plugin_worker')
        if not (0.05 <= timeout <= 300) or not (1024 <= max_output <= 1_048_576) or not (1024 <= max_input <= 1_048_576):
            raise ValueError("运行时限制超出允许范围")
        self.timeout = timeout
        self.max_output = max_output
        self.max_input = max_input
        self._lock = threading.RLock()
        self._running: dict[int, tuple[subprocess.Popen, object]] = {}

    def _stop(self, process, job):
        if job is not None:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def close(self):
        with self._lock:
            active = list(self._running.values())
        for process, job in active:
            self._stop(process, job)

    def run(self, package_root: Path, entrypoint: dict, request: dict, *, cancel_event=None):
        envelope = {**request, "package_root": str(package_root), "entrypoint": entrypoint}
        raw = (json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > self.max_input:
            raise RuntimeFailure("input_limit", "插件输入超过上限")
        # Do not inherit PATH, HOME, APPDATA, proxies, model/API keys, or Python paths.
        # Windows loader requires SystemRoot; this reveals the OS path, not credentials.
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if os.name == "nt" and os.environ.get("SystemRoot"):
            env["SystemRoot"] = os.environ["SystemRoot"]
        process = None
        job = None
        readers = []
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        events: queue.Queue = queue.Queue(maxsize=16)
        overflow = threading.Event()
        count = [0]
        count_lock = threading.Lock()
        def read_stream(name, stream):
            try:
                while True:
                    block = stream.read(4096)
                    if not block:
                        break
                    with count_lock:
                        count[0] += len(block)
                        if count[0] > self.max_output:
                            overflow.set()
                            break
                        chunks[name].extend(block)
            finally:
                events.put(name)
        try:
            try:
                process = subprocess.Popen(self.worker_command, cwd=package_root, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=os.name != "nt",
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if os.name == "nt":
                    job = _WindowsJob(process)
            except (OSError, RuntimeFailure) as exc:
                raise RuntimeFailure("process_control_unavailable", "无法启动插件工作进程") from exc
            with self._lock:
                self._running[process.pid] = (process, job)
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = threading.Thread(target=read_stream, args=(name, stream), daemon=True)
                thread.start()
                readers.append(thread)
            def write_input():
                try:
                    process.stdin.write(raw)
                    process.stdin.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    try: process.stdin.close()
                    except OSError: pass
            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
            deadline = time.monotonic() + self.timeout
            ended = 0
            while ended < 2:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeFailure('cancelled', '插件调用已停止')
                if overflow.is_set():
                    raise RuntimeFailure("output_limit", "插件输出超过上限")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeFailure("timeout", "插件调用超时；已终止进程树")
                try:
                    events.get(timeout=min(remaining, 0.05))
                    ended += 1
                except queue.Empty:
                    pass
            if overflow.is_set():
                raise RuntimeFailure("output_limit", "插件输出超过上限")
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise RuntimeFailure("worker_exit", "插件进程异常退出")
            try:
                text = bytes(chunks["stdout"]).decode("utf-8", errors="strict")
                response = json.loads(text, object_pairs_hook=_strict_object,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
                if (not isinstance(response, dict) or response.get("protocol") != 1
                        or response.get("request_id") != request["request_id"] or type(response.get("ok")) is not bool):
                    raise ValueError("protocol")
            except (UnicodeError, ValueError, KeyError) as exc:
                raise RuntimeFailure("protocol_error", "插件响应不是合法协议") from exc
            if not response["ok"]:
                raise RuntimeFailure("plugin_exception", "插件执行失败（未返回敏感异常细节）")
            if set(response) != {"protocol", "request_id", "ok", "result"}:
                raise RuntimeFailure("protocol_error", "插件响应字段不匹配")
            return response["result"]
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure("timeout", "插件调用超时") from exc
        finally:
            if process is not None:
                self._stop(process, job)
                for stream in (process.stdin, process.stdout, process.stderr):
                    try: stream.close()
                    except (OSError, ValueError): pass
                for thread in readers:
                    thread.join(timeout=1)
                with self._lock:
                    self._running.pop(process.pid, None)
