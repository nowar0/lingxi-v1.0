"""Explicit offline video probe and approved frame conversion; no import-time I/O.

Only host-held bytes are accepted. This module never accepts a URL, caller path,
command, protocol, filter, executable, or serialized trusted receipt. It never
changes a route/model or substitutes frames for a native video. A conversion
approval is NOT model-send approval. Container probing is not full-file decode or
proof of decoder safety. Windows process containment is a real single-process Job,
not a worker-thread timeout; unsupported hosts fail closed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import subprocess
import tempfile
import threading
import time
import zlib

VERSION = 'bounded-local-video-v1'
MAX_ORIGINAL_BYTES = 15 * 1024**2
MAX_ORIGINALS = 8
MAX_FRAMES = 16
MAX_DURATION_MS = 6 * 60 * 60 * 1000
MAX_SOURCE_PIXELS = 16777216
MAX_OUTPUT_DIMENSION = 2048
MAX_FRAME_BYTES = 13 * 1024**2
MAX_RESULT_BYTES = 64 * 1024**2
MAX_PROBE_BYTES = 128 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PROCESS_MEMORY = 512 * 1024**2
PROBE_TIMEOUT_SECONDS = 15
FRAME_TIMEOUT_SECONDS = 20
MAX_CONVERSION_SECONDS = 120
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_VIDEO_CODECS = {'mp4': ('h264', 'hevc', 'av1'), 'webm': ('vp8', 'vp9', 'av1')}
_AUDIO_CODECS = {'mp4': ('aac', 'mp3', 'opus'), 'webm': ('opus', 'vorbis')}
# Process handles whose termination was NOT confirmed remain owned here. Do not
# close their Job, pretend cleanup completed, or kill a PID found by enumeration.
_RETAINED_RUNS: list[tuple] = []
_RUN_LOCK = threading.Lock()


class VideoExtractionError(ValueError):
    """Fixed metadata-only codes; no attachment, path, parser log or tool stderr."""
    def __init__(self, code: str, *, scratch_retained: bool = False):
        self.code = code
        self.scratch_retained = scratch_retained
        super().__init__(code)


def _fail(code):
    raise VideoExtractionError(code)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _integer(value, low, high, code='invalid_video_selection'):
    if type(value) is not int or not low <= value <= high:
        _fail(code)


def _identity(original_id, original_version):
    if type(original_id) is not str or not _ID.fullmatch(original_id):
        _fail('host_original_identity_required')
    _integer(original_version, 1, 2**53-1, 'host_original_identity_required')


@dataclass(frozen=True)
class VideoSelection:
    # Empty never means all frames. Milliseconds are relative to the video start;
    # the decoder returns the first available frame at/after the requested seek.
    timestamps_ms: tuple[int, ...]
    frame_count: int
    max_width: int
    max_height: int
    stream_index: int = 0
    output_format: str = 'png'

    def __post_init__(self):
        _integer(self.frame_count, 1, MAX_FRAMES)
        _integer(self.max_width, 1, MAX_OUTPUT_DIMENSION)
        _integer(self.max_height, 1, MAX_OUTPUT_DIMENSION)
        _integer(self.stream_index, 0, 7)
        if (type(self.timestamps_ms) is not tuple
                or len(self.timestamps_ms) != self.frame_count
                or any(type(v) is not int or not 0 <= v < MAX_DURATION_MS for v in self.timestamps_ms)
                or any(a >= b for a, b in zip(self.timestamps_ms, self.timestamps_ms[1:]))
                or self.output_format != 'png'):
            _fail('explicit_sorted_unique_video_timestamps_required')


def validate_video_request_budget(selection, *, request_original_count, other_video_frames):
    """Counts must come from the host's whole pending submission, never browser claims."""
    if type(selection) is not VideoSelection:
        _fail('typed_video_selection_required')
    _integer(request_original_count, 1, MAX_ORIGINALS, 'request_original_count_limit')
    _integer(other_video_frames, 0, MAX_FRAMES, 'request_video_frame_limit')
    total = other_video_frames + selection.frame_count
    if total > MAX_FRAMES:
        _fail('request_video_frame_limit')
    return total


@dataclass(frozen=True)
class MediaToolIdentity:
    name: str
    version: str  # Reviewed distribution version, not a guessed model/tool version.
    sha256: str
    dependency_manifest_sha256: str  # Also binds the companion DLL inventory.


@dataclass(frozen=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    duration_ms: int | None
    average_frame_rate: str


@dataclass(frozen=True)
class VideoProbe:
    original_sha256: str
    byte_length: int
    kind: str
    mime: str
    container: str
    duration_ms: int
    video_streams: tuple[VideoStream, ...]
    audio_codecs: tuple[str, ...]
    tool: MediaToolIdentity
    actual_probe_version: str
    fingerprint: str
    cleanup_status: str = field(compare=False)
    structural_validation: str = 'bounded_ffprobe_container_streams_validated'
    full_video_decoded: bool = False
    native_provider_support_verified: bool = False

    def metadata(self):
        return asdict(self)


@dataclass(frozen=True)
class VideoConversionPlan:
    original_id: str
    original_version: int
    original_sha256: str
    filename: str
    mime: str
    byte_length: int
    inspection_fingerprint: str
    selection: VideoSelection
    output_width: int
    output_height: int
    frame_count: int
    request_original_count: int
    request_total_video_frames: int
    probe_tool: MediaToolIdentity
    conversion_tool: MediaToolIdentity
    pillow_version: str
    conversion_settings_sha256: str
    fingerprint: str
    loss_notes: tuple[str, ...]
    converter: str = VERSION
    approval_required: bool = True
    sending_approved: bool = False

    def metadata(self):
        return asdict(self)


@dataclass(frozen=True)
class VideoProcessedPart:
    content: bytes = field(repr=False)
    sha256: str
    mime: str
    width: int
    height: int
    locator: tuple[tuple[str, str], ...]
    original_id: str
    original_version: int
    original_sha256: str
    conversion_settings_sha256: str
    converter: str = VERSION


@dataclass(frozen=True)
class VideoExtractionResult:
    parts: tuple[VideoProcessedPart, ...] = field(repr=False)
    original_sha256: str
    original_id: str
    original_version: int
    inspection_fingerprint: str
    conversion_plan_fingerprint: str
    conversion_settings_sha256: str
    frame_count: int
    content_bytes: int
    loss_notes: tuple[str, ...]
    cleanup_statuses: tuple[str, ...] = field(compare=False)
    sending_approved: bool = False
    requires_final_content_preview_and_send_approval: bool = True


@dataclass(frozen=True)
class _ResolvedTool:
    path: Path = field(repr=False)
    identity: MediaToolIdentity


def _tool(name):
    # No alternate executable/PATH, auto-download, unapproved quarantine, or
    # permission/status rewriting. The provider verifies every companion DLL.
    try:
        from app.model_media_dependencies import (EXPECTED_MANIFEST_SHA256,
            get_media_dependency_status, resolve_media_binary)
        path = resolve_media_binary(name)
        status = get_media_dependency_status(verify_binary_hashes=False)
        item = status['binaries'][name]
        if (item['redistribution_status'] != 'approved'
                or type(item['version']) is not str or not 1 <= len(item['version']) <= 200
                or not _SHA.fullmatch(item['sha256'])
                or not path.is_absolute() or path.name.lower() != name + '.exe'):
            _fail('reviewed_media_dependency_unavailable')
        # An additional local byte check binds the exact executable to the plan.
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != item['sha256']:
            _fail('reviewed_media_dependency_changed')
        return _ResolvedTool(path, MediaToolIdentity(name, item['version'], digest,
                                                    EXPECTED_MANIFEST_SHA256))
    except VideoExtractionError:
        raise
    except Exception:
        _fail('reviewed_media_dependency_unavailable')


def video_dependency_diagnostic():
    """Explicit local metadata check only; no native process and no local paths returned."""
    try:
        tools = tuple(_tool(name).identity for name in ('ffprobe', 'ffmpeg'))
        from app.model_media_dependencies import require_python_dependency
        pillow = require_python_dependency('pillow')
        return {'available': True, 'tools': [asdict(tool) for tool in tools],
                'pillow_version': pillow, 'native_executed': False,
                'diagnostic': 'reviewed_local_dependencies_present_not_runtime_validated'}
    except Exception:
        return {'available': False, 'tools': [], 'pillow_version': None,
                'native_executed': False,
                'diagnostic': 'reviewed_provider_unavailable_or_redistribution_review_required'}


def _source(filename, mime, raw):
    if (type(filename) is not str or not 1 <= len(filename) <= 255
            or any(c in filename for c in '/\\:\x00')
            or any(ord(c) < 32 for c in filename) or filename.endswith((' ', '.'))):
        _fail('invalid_video_filename')
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ('mp4', 'webm') or mime != {'mp4':'video/mp4', 'webm':'video/webm'}[ext]:
        _fail('video_extension_mime_mismatch')
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ORIGINAL_BYTES:
        _fail('video_original_byte_limit')
    if ext == 'mp4':
        if len(raw) < 16 or raw[4:8] != b'ftyp':
            _fail('video_container_magic_mismatch')
        size = int.from_bytes(raw[:4], 'big')
        if not 16 <= size <= min(len(raw), 4096) or size % 4:
            _fail('video_container_magic_mismatch')
        brands = (raw[8:12],) + tuple(raw[i:i+4] for i in range(16, size, 4))
        if not set(brands) & {b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'avc1', b'mp41', b'mp42', b'dash'}:
            _fail('unsupported_mp4_brand')
    else:
        # Matroska and WebM share EBML magic. Require a bounded EBML header whose
        # actual DocType is webm, and still run the real restricted container probe.
        def vint(offset, keep_marker=False):
            if offset >= len(raw) or not raw[offset]:
                _fail('invalid_webm_header')
            mask, count = 0x80, 1
            while not raw[offset] & mask:
                mask >>= 1
                count += 1
            if count > 8 or offset+count > len(raw):
                _fail('invalid_webm_header')
            value = int.from_bytes(raw[offset:offset+count], 'big')
            if not keep_marker:
                value &= (1 << (7*count))-1
                if value == (1 << (7*count))-1:
                    _fail('invalid_webm_header')
            return value, offset+count
        if not raw.startswith(b'\x1aE\xdf\xa3'):
            _fail('video_container_magic_mismatch')
        size, start = vint(4)
        end = start+size
        if not 1 <= size <= 4096 or end > len(raw):
            _fail('invalid_webm_header')
        doc_types = []
        while start < end:
            tag, start = vint(start, True)
            length, start = vint(start)
            if start+length > end:
                _fail('invalid_webm_header')
            if tag == 0x4282:
                doc_types.append(raw[start:start+length])
            start += length
        if doc_types != [b'webm']:
            _fail('webm_doctype_required')
    return ext


class _Scratch:
    """One fresh private directory. Never recursive cleanup or guessed ownership."""
    def __init__(self, raw, kind):
        self.path = Path(tempfile.mkdtemp(prefix='lingxi-video-'))
        info = self.path.lstat()
        self.identity = (info.st_dev, info.st_ino)
        self.marker = secrets.token_bytes(32)
        self.input_name = 'original.'+kind
        self.expected = {'owner.bin': self.marker, self.input_name: raw}
        self.file_identities = {}
        self.preserve = False
        self.status = 'retained_cleanup_not_confirmed'
        try:
            for name, content in self.expected.items():
                with (self.path / name).open('xb') as output:
                    output.write(content)
                file_info = (self.path / name).lstat()
                self.file_identities[name] = (file_info.st_dev, file_info.st_ino)
        except Exception:
            self.preserve = True
            raise VideoExtractionError('video_scratch_creation_failed', scratch_retained=True) from None

    def cleanup(self):
        if self.preserve:
            return self.status
        try:
            info = self.path.lstat()
            if ((info.st_dev, info.st_ino) != self.identity
                    or not stat.S_ISDIR(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                self.status = 'retained_scratch_owner_changed'
                return self.status
            entries = tuple(self.path.iterdir())
            if {entry.name for entry in entries} != set(self.expected):
                self.status = 'retained_unknown_scratch_entries'
                return self.status
            # Validate ALL identities/bytes before deleting any known file.
            for entry in entries:
                info = entry.lstat()
                expected = self.expected[entry.name]
                if (not stat.S_ISREG(info.st_mode) or info.st_size != len(expected)
                        or (info.st_dev, info.st_ino) != self.file_identities.get(entry.name)
                        or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400):
                    self.status = 'retained_scratch_owner_changed'
                    return self.status
                with entry.open('rb') as stream:
                    observed = os.fstat(stream.fileno())
                    content = stream.read(len(expected)+1)
                if ((observed.st_dev, observed.st_ino) != self.file_identities[entry.name]
                        or content != expected):
                    self.status = 'retained_scratch_owner_changed'
                    return self.status
            for name in (self.input_name, 'owner.bin'):
                current = (self.path / name).lstat()
                if ((current.st_dev, current.st_ino) != self.file_identities[name]
                        or getattr(current, 'st_file_attributes', 0) & 0x400):
                    self.status = 'retained_scratch_owner_changed'
                    return self.status
                (self.path / name).unlink()
            self.path.rmdir()
            self.status = 'exact_owned_cleanup_completed'
        except OSError:
            self.status = 'retained_cleanup_not_confirmed'
        return self.status


def _wait_job_empty(observe_active, *, deadline, clock=time.monotonic, pause=time.sleep):
    """Observe asynchronous Job accounting only within the SAME held deadline.

    A signalled process handle can precede Job ActiveProcesses accounting reaching
    zero. Never promote a query failure, negative count or expired budget to exit
    proof, and never create a renewed timeout on the successful output path.
    """
    while True:
        if clock() >= deadline:return False
        active=observe_active()
        if type(active) is not int or active<0:return False
        if clock() >= deadline:return False
        if active==0:return True
        remaining=deadline-clock()
        if remaining<=0:return False
        pause(min(.01,remaining))


def _run_native(tool, arguments, scratch, *, timeout, stdout_limit):
    """Create suspended -> assign exact native Job -> resume -> bounded pipe drain.

    No subprocess.Popen, shell, taskkill, PID search, background parser thread,
    network protocol, inherited ambient handles, or unbounded communicate(). The
    subprocess import is used only for Windows argv quoting, never process launch.
    """
    if os.name != 'nt':
        _fail('bounded_video_worker_requires_windows')
    if _RETAINED_RUNS:
        _fail('previous_video_worker_termination_unconfirmed')
    fresh = _tool(tool.identity.name)
    if fresh != tool:
        _fail('reviewed_media_dependency_changed')
    import ctypes as C
    from ctypes import wintypes as W

    kernel = C.WinDLL('kernel32', use_last_error=True)
    ULONG_PTR = C.c_size_t
    SIZE_T = C.c_size_t

    class SECURITY_ATTRIBUTES(C.Structure):
        _fields_ = [('nLength', W.DWORD), ('lpSecurityDescriptor', W.LPVOID), ('bInheritHandle', W.BOOL)]

    class STARTUPINFO(C.Structure):
        _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR),
                    ('lpTitle', W.LPWSTR), ('dwX', W.DWORD), ('dwY', W.DWORD),
                    ('dwXSize', W.DWORD), ('dwYSize', W.DWORD), ('dwXCountChars', W.DWORD),
                    ('dwYCountChars', W.DWORD), ('dwFillAttribute', W.DWORD),
                    ('dwFlags', W.DWORD), ('wShowWindow', W.WORD), ('cbReserved2', W.WORD),
                    ('lpReserved2', C.POINTER(W.BYTE)), ('hStdInput', W.HANDLE),
                    ('hStdOutput', W.HANDLE), ('hStdError', W.HANDLE)]

    class STARTUPINFOEX(C.Structure):
        _fields_ = [('StartupInfo', STARTUPINFO), ('lpAttributeList', W.LPVOID)]

    class PROCESS_INFORMATION(C.Structure):
        _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE),
                    ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]

    class BASIC_LIMIT(C.Structure):
        _fields_ = [('PerProcessUserTimeLimit', C.c_longlong), ('PerJobUserTimeLimit', C.c_longlong),
                    ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', SIZE_T),
                    ('MaximumWorkingSetSize', SIZE_T), ('ActiveProcessLimit', W.DWORD),
                    ('Affinity', ULONG_PTR), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]

    class IO_COUNTERS(C.Structure):
        _fields_ = [(name, C.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                    'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class EXTENDED_LIMIT(C.Structure):
        _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                    ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                    ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]

    class BASIC_ACCOUNTING(C.Structure):
        _fields_ = [('TotalUserTime', C.c_longlong), ('TotalKernelTime', C.c_longlong),
                    ('ThisPeriodTotalUserTime', C.c_longlong), ('ThisPeriodTotalKernelTime', C.c_longlong),
                    ('TotalPageFaultCount', W.DWORD), ('TotalProcesses', W.DWORD),
                    ('ActiveProcesses', W.DWORD), ('TotalTerminatedProcesses', W.DWORD)]

    def bind(name, args, result):
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
        return fn

    CloseHandle = bind('CloseHandle', [W.HANDLE], W.BOOL)
    CreatePipe = bind('CreatePipe', [C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), C.POINTER(SECURITY_ATTRIBUTES), W.DWORD], W.BOOL)
    SetHandleInformation = bind('SetHandleInformation', [W.HANDLE, W.DWORD, W.DWORD], W.BOOL)
    CreateFile = bind('CreateFileW', [W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(SECURITY_ATTRIBUTES), W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
    CreateJob = bind('CreateJobObjectW', [W.LPVOID, W.LPCWSTR], W.HANDLE)
    SetJob = bind('SetInformationJobObject', [W.HANDLE, C.c_int, W.LPVOID, W.DWORD], W.BOOL)
    QueryJob = bind('QueryInformationJobObject', [W.HANDLE, C.c_int, W.LPVOID, W.DWORD, W.LPVOID], W.BOOL)
    AssignJob = bind('AssignProcessToJobObject', [W.HANDLE, W.HANDLE], W.BOOL)
    TerminateJob = bind('TerminateJobObject', [W.HANDLE, W.UINT], W.BOOL)
    TerminateProcess = bind('TerminateProcess', [W.HANDLE, W.UINT], W.BOOL)
    Wait = bind('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
    Resume = bind('ResumeThread', [W.HANDLE], W.DWORD)
    GetExit = bind('GetExitCodeProcess', [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL)
    Peek = bind('PeekNamedPipe', [W.HANDLE, W.LPVOID, W.DWORD, W.LPVOID, C.POINTER(W.DWORD), W.LPVOID], W.BOOL)
    Read = bind('ReadFile', [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID], W.BOOL)
    InitAttr = bind('InitializeProcThreadAttributeList', [W.LPVOID, W.DWORD, W.DWORD, C.POINTER(SIZE_T)], W.BOOL)
    UpdateAttr = bind('UpdateProcThreadAttribute', [W.LPVOID, W.DWORD, ULONG_PTR, W.LPVOID, SIZE_T, W.LPVOID, W.LPVOID], W.BOOL)
    DeleteAttr = bind('DeleteProcThreadAttributeList', [W.LPVOID], None)
    CreateProcess = bind('CreateProcessW', [W.LPCWSTR, W.LPWSTR, W.LPVOID, W.LPVOID, W.BOOL, W.DWORD,
                                          W.LPVOID, W.LPCWSTR, C.POINTER(STARTUPINFOEX), C.POINTER(PROCESS_INFORMATION)], W.BOOL)
    GetWindowsDirectory = bind('GetWindowsDirectoryW', [W.LPWSTR, W.UINT], W.UINT)

    def active_job_processes():
        accounting=BASIC_ACCOUNTING()
        job_exit_observation['queries']+=1
        if not QueryJob(job,1,C.byref(accounting),C.sizeof(accounting),None):
            job_exit_observation['query_failed']=True
            return None
        value=int(accounting.ActiveProcesses)
        if job_exit_observation['first_active'] is None:job_exit_observation['first_active']=value
        job_exit_observation['last_active']=value
        return value

    handles = []
    process = PROCESS_INFORMATION()
    job, assigned, confirmed, attrs_ready = None, False, False, False
    attr_buffer = None
    stdout, stderr = bytearray(), bytearray()
    pipe_eof={}
    job_exit_observation={'queries':0,'query_failed':False,'first_active':None,'last_active':None}
    deadline = time.monotonic()+timeout
    invalid_handle = C.c_void_p(-1).value
    sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)

    def own(handle):
        value = handle.value if isinstance(handle, W.HANDLE) else handle
        if value in (None, 0, invalid_handle):
            _fail('video_worker_handle_creation_failed')
        handles.append(value)
        return value

    def close(handle):
        if handle in handles:
            if not CloseHandle(handle):
                _fail('video_worker_handle_close_failed')
            handles.remove(handle)

    def drain(handle, target, maximum):
        handle=handle.value if isinstance(handle,W.HANDLE) else handle
        if pipe_eof.get(handle) is True:return
        while True:
            if time.monotonic() >= deadline:
                _fail('video_worker_timeout')
            available = W.DWORD()
            if not Peek(handle, None, 0, None, C.byref(available), None):
                if C.get_last_error() == 109:  # Actual EOF, not merely zero available bytes.
                    pipe_eof[handle]=True
                    return
                _fail('video_worker_pipe_failed')
            if not available.value:
                return
            count = min(available.value, 65536, maximum-len(target)+1)
            buffer = C.create_string_buffer(count)
            read = W.DWORD()
            if not Read(handle, buffer, count, C.byref(read), None) or not read.value:
                _fail('video_worker_pipe_failed')
            if len(target)+read.value > maximum:
                _fail('video_worker_output_budget')
            target.extend(buffer.raw[:read.value])

    try:
        out_r, out_w, err_r, err_w = W.HANDLE(), W.HANDLE(), W.HANDLE(), W.HANDLE()
        for r, w in ((out_r, out_w), (err_r, err_w)):
            if not CreatePipe(C.byref(r), C.byref(w), C.byref(sa), 65536):
                _fail('video_worker_pipe_creation_failed')
            own(r); own(w)
            if not SetHandleInformation(r, 1, 0):
                _fail('video_worker_handle_inheritance_failed')
        stdin = own(CreateFile('NUL', 0x80000000, 3, C.byref(sa), 3, 0, None))
        job = own(CreateJob(None, None))
        limits = EXTENDED_LIMIT()
        # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS | PROCESS_MEMORY | JOB_MEMORY.
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x8 | 0x100 | 0x200
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.ProcessMemoryLimit = limits.JobMemoryLimit = MAX_PROCESS_MEMORY
        if not SetJob(job, 9, C.byref(limits), C.sizeof(limits)):
            _fail('video_worker_job_limits_unavailable')
        attr_size = SIZE_T()
        InitAttr(None, 1, 0, C.byref(attr_size))
        if not 0 < attr_size.value <= 65536:
            _fail('video_worker_handle_list_unavailable')
        attr_buffer = C.create_string_buffer(attr_size.value)
        if not InitAttr(attr_buffer, 1, 0, C.byref(attr_size)):
            _fail('video_worker_handle_list_unavailable')
        attrs_ready = True
        inherit = (W.HANDLE*3)(stdin, out_w.value, err_w.value)
        if not UpdateAttr(attr_buffer, 0, 0x20002, inherit, C.sizeof(inherit), None, None):
            _fail('video_worker_handle_list_unavailable')
        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = C.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin
        startup.StartupInfo.hStdOutput = out_w.value
        startup.StartupInfo.hStdError = err_w.value
        startup.lpAttributeList = C.cast(attr_buffer, W.LPVOID)
        win_buffer = C.create_unicode_buffer(32768)
        size = GetWindowsDirectory(win_buffer, len(win_buffer))
        if not 0 < size < len(win_buffer):
            _fail('video_worker_system_directory_unavailable')
        # Do NOT inherit FFREPORT, proxies, Python variables, user PATH or plugin
        # environment. Only OS DLLs and reviewed exe-directory DLLs are discoverable.
        windows = win_buffer.value
        env = {'SystemRoot':windows, 'WINDIR':windows, 'PATH':str(Path(windows)/'System32'),
               'TEMP':str(scratch.path), 'TMP':str(scratch.path), 'AV_LOG_FORCE_NOCOLOR':'1'}
        environment = C.create_unicode_buffer('\x00'.join(k+'='+env[k] for k in sorted(env, key=str.casefold))+'\x00\x00')
        command = C.create_unicode_buffer(subprocess.list2cmdline([str(tool.path), *arguments]))
        flags = 0x4 | 0x400 | 0x80000 | 0x8000000  # suspended, unicode env, extended startup, no window
        if not CreateProcess(str(tool.path), command, None, None, True, flags, environment,
                             str(scratch.path), C.byref(startup), C.byref(process)):
            _fail('video_worker_creation_failed')
        own(process.hProcess); own(process.hThread)
        # No untrusted instruction can execute before assignment succeeds.
        if not AssignJob(job, process.hProcess):
            _fail('video_worker_job_assignment_failed')
        assigned = True
        for handle in (out_w.value, err_w.value, stdin):
            close(handle)
        if Resume(process.hThread) == 0xffffffff:
            _fail('video_worker_resume_failed')
        close(process.hThread)
        while True:
            drain(out_r, stdout, stdout_limit)
            drain(err_r, stderr, MAX_STDERR_BYTES)
            state = Wait(process.hProcess, 10)
            if state == 0:
                # Once exit is signalled no writer can remain: children are denied
                # by the single-process Job and only these standard handles leaked.
                drain(out_r, stdout, stdout_limit)
                drain(err_r, stderr, MAX_STDERR_BYTES)
                if not _wait_job_empty(active_job_processes,deadline=deadline):
                    _fail('video_worker_job_exit_unconfirmed')
                while not (pipe_eof.get(out_r.value) and pipe_eof.get(err_r.value)):
                    drain(out_r.value,stdout,stdout_limit)
                    drain(err_r.value,stderr,MAX_STDERR_BYTES)
                    if not (pipe_eof.get(out_r.value) and pipe_eof.get(err_r.value)):
                        if time.monotonic()>=deadline:_fail('video_worker_pipe_eof_unconfirmed')
                        time.sleep(min(.005,max(0,deadline-time.monotonic())))
                confirmed = True
                code = W.DWORD()
                if not GetExit(process.hProcess, C.byref(code)) or code.value:
                    _fail('video_worker_failed')
                return bytes(stdout)
            if state != 258:
                _fail('video_worker_wait_failed')
            if time.monotonic() >= deadline:
                _fail('video_worker_timeout')
    finally:
        if process.hProcess and not confirmed:
            # Only this invocation's exact native handle/Job is terminated. Never a
            # PID/name search, and never a claim that an unconfirmed kill succeeded.
            stopped = TerminateJob(job, 1) if assigned else TerminateProcess(process.hProcess, 1)
            exited = Wait(process.hProcess, 5000) == 0
            empty = True
            if assigned:
                # Cleanup has a separate bounded proof window, never a renewed
                # successful conversion budget. Failed cleanup retains all owners.
                empty=_wait_job_empty(active_job_processes,deadline=time.monotonic()+5)
            confirmed = bool(exited and empty)
            if not confirmed:
                scratch.preserve = True
                scratch.status = 'retained_process_termination_unconfirmed'
                _RETAINED_RUNS.append((kernel, tuple(handles), process.dwProcessId, scratch,
                                       bool(stopped), assigned))
                handles.clear()  # Retained, NOT closed/disowned; host intervention required.
        if attrs_ready:
            DeleteAttr(attr_buffer)
        for handle in reversed(handles):
            CloseHandle(handle)
        if process.hProcess and not confirmed:
            raise VideoExtractionError('video_worker_termination_unconfirmed', scratch_retained=True)


def _run_with_input(tool, raw, kind, make_arguments, *, timeout, stdout_limit):
    # Serializing native video work also bounds scratch/disk/native memory across
    # simultaneous host requests. Do not queue behind an unbounded worker thread.
    if not _RUN_LOCK.acquire(blocking=False):
        _fail('video_worker_busy')
    scratch = None
    try:
        scratch = _Scratch(raw, kind)
        output = _run_native(tool, make_arguments(scratch.path / scratch.input_name), scratch,
                             timeout=timeout, stdout_limit=stdout_limit)
        return output, scratch.cleanup()
    except VideoExtractionError as exc:
        if scratch is not None:
            status = scratch.cleanup()
            if status != 'exact_owned_cleanup_completed':
                exc.scratch_retained = True
        raise
    except Exception:
        retained = scratch is not None and scratch.cleanup() != 'exact_owned_cleanup_completed'
        raise VideoExtractionError('video_worker_unavailable', scratch_retained=retained) from None
    finally:
        _RUN_LOCK.release()


def _input_format_arguments(kind):
    # mov's data references are explicitly disabled. A local file protocol whitelist
    # alone is NOT permission to follow attachment-controlled filesystem references.
    return (['-f', 'mov', '-enable_drefs', '0', '-use_absolute_path', '0']
            if kind == 'mp4' else ['-f', 'matroska'])


def _probe_arguments(path, kind):
    return ['-hide_banner', '-v', 'error', '-max_alloc', '67108864',
            '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm',
            *_input_format_arguments(kind), '-threads', '1', '-max_pixels', str(MAX_SOURCE_PIXELS),
            '-probesize', str(MAX_ORIGINAL_BYTES), '-analyzeduration', '5000000',
            '-max_probe_packets', '4096', '-show_program_version',
            '-show_entries', 'program_version=version:format=format_name,duration,size,nb_streams:'
            'stream=index,codec_type,codec_name,width,height,duration,avg_frame_rate,sample_aspect_ratio:'
            'stream_disposition=attached_pic', '-of', 'json', '-i', str(path)]


def _probe_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail('video_probe_duplicate_json')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: _fail('video_probe_nonfinite_json'))
    except (ValueError, UnicodeError, RecursionError):
        _fail('video_probe_invalid_json')
    count, stack = 0, [(value, 0)]
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 4096 or depth > 12:
            _fail('video_probe_json_budget')
        if type(item) is dict:
            if any(type(key) is not str or len(key) > 100 for key in item):
                _fail('video_probe_invalid_json')
            stack.extend((child, depth+1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth+1) for child in item)
        elif type(item) is str and len(item) > 1000:
            _fail('video_probe_json_budget')
    return value


def _duration(value, *, optional=False):
    if optional and value in (None, 'N/A'):
        return None
    if type(value) is not str or not re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,9})?', value):
        _fail('video_probe_duration_unknown_or_invalid')
    try:
        number = Decimal(value)*1000
        if not number.is_finite() or not 1 <= number <= MAX_DURATION_MS:
            _fail('video_duration_limit')
        return int(number.to_integral_value(rounding=ROUND_FLOOR))
    except InvalidOperation:
        _fail('video_probe_duration_unknown_or_invalid')


def _parse_probe(value, raw, kind, mime, tool, cleanup):
    if type(value) is not dict or not {'format', 'streams', 'program_version'} <= value.keys():
        _fail('video_probe_incomplete')
    fmt, streams, program = value['format'], value['streams'], value['program_version']
    if (type(fmt) is not dict or type(streams) is not list or not 1 <= len(streams) <= 8
            or type(program) is not dict or type(program.get('version')) is not str
            or not 1 <= len(program['version']) <= 200):
        _fail('video_probe_invalid_container')
    expected = 'mov,mp4,m4a,3gp,3g2,mj2' if kind == 'mp4' else 'matroska,webm'
    if (fmt.get('format_name') != expected or fmt.get('size') != str(len(raw))
            or type(fmt.get('nb_streams')) is not int or fmt['nb_streams'] != len(streams)):
        _fail('video_probe_container_mime_or_size_mismatch')
    duration = _duration(fmt.get('duration'))
    video, audio, indices = [], [], set()
    for stream in streams:
        if type(stream) is not dict:
            _fail('video_probe_invalid_stream')
        index = stream.get('index')
        _integer(index, 0, 7, 'video_probe_invalid_stream')
        if index in indices:
            _fail('video_probe_duplicate_stream')
        indices.add(index)
        disposition = stream.get('disposition')
        if (type(disposition) is not dict or type(disposition.get('attached_pic')) is not int
                or disposition['attached_pic'] != 0):
            _fail('video_attached_picture_or_unknown_disposition')
        codec = stream.get('codec_name')
        if stream.get('codec_type') == 'video':
            if codec not in _VIDEO_CODECS[kind]:
                _fail('video_codec_unsupported')
            width, height = stream.get('width'), stream.get('height')
            _integer(width, 1, 8192, 'video_source_dimension_limit')
            _integer(height, 1, 8192, 'video_source_dimension_limit')
            if width*height > MAX_SOURCE_PIXELS:
                _fail('video_source_pixel_limit')
            if stream.get('sample_aspect_ratio', '1:1') not in ('1:1', '0:1', 'N/A'):
                _fail('video_nonsquare_pixel_unsupported')
            fps = stream.get('avg_frame_rate')
            match = re.fullmatch(r'([0-9]{1,9})/([0-9]{1,9})', fps) if type(fps) is str else None
            if match is None or int(match[2]) == 0 or not 0 < Decimal(match[1])/Decimal(match[2]) <= 240:
                _fail('video_frame_rate_unknown_or_limit')
            stream_duration = _duration(stream.get('duration'), optional=True)
            if stream_duration is not None and stream_duration > duration+1000:
                _fail('video_stream_duration_mismatch')
            video.append(VideoStream(index, codec, width, height, stream_duration, fps))
        elif stream.get('codec_type') == 'audio':
            if codec not in _AUDIO_CODECS[kind]:
                _fail('video_audio_codec_unsupported')
            audio.append(codec)
        else:
            _fail('video_subtitle_data_or_attachment_stream_unsupported')
    if not 1 <= len(video) <= 4 or len(audio) > 4:
        _fail('video_stream_count_limit')
    body = {'original_sha256':_hash(raw), 'byte_length':len(raw), 'kind':kind, 'mime':mime,
            'container':expected, 'duration_ms':duration,
            'video_streams':[asdict(stream) for stream in video], 'audio_codecs':audio,
            'tool':asdict(tool.identity), 'actual_probe_version':program['version'], 'inspector':VERSION,
            'probe_argv':_probe_arguments(Path('HOST_ORIGINAL.'+kind), kind),
            'limits':{'stdout_bytes':MAX_PROBE_BYTES, 'stderr_bytes':MAX_STDERR_BYTES,
                      'memory_bytes':MAX_PROCESS_MEMORY, 'timeout_seconds':PROBE_TIMEOUT_SECONDS}}
    return VideoProbe(body['original_sha256'], len(raw), kind, mime, expected, duration,
                      tuple(video), tuple(audio), tool.identity, program['version'], _digest(body), cleanup)


def probe_video(filename, mime, raw):
    """Actually run bounded ffprobe on original bytes. Never run ffmpeg/transcode.

    The original MIME is only MP4 or actual WebM. A successful report validates the
    admitted container/stream metadata, not every packet, provider support, malware
    absence or full-video decodability. Unsupported/unknown durations fail closed.
    """
    kind = _source(filename, mime, raw)
    tool = _tool('ffprobe')
    output, cleanup = _run_with_input(tool, raw, kind, lambda path:_probe_arguments(path, kind),
                                    timeout=PROBE_TIMEOUT_SECONDS, stdout_limit=MAX_PROBE_BYTES)
    return _parse_probe(_probe_json(output), raw, kind, mime, tool, cleanup)


def preview_video_conversion(filename, mime, raw, selection, *, original_id, original_version,
                             request_original_count, other_video_frames):
    """Probe only, then describe exact local conversion; never extract/send here."""
    _identity(original_id, original_version)
    total = validate_video_request_budget(selection, request_original_count=request_original_count,
                                         other_video_frames=other_video_frames)
    probe = probe_video(filename, mime, raw)
    selected = next((stream for stream in probe.video_streams if stream.index == selection.stream_index), None)
    if selected is None:
        _fail('selected_video_stream_missing')
    duration = min(probe.duration_ms, selected.duration_ms or probe.duration_ms)
    if any(timestamp >= duration for timestamp in selection.timestamps_ms):
        _fail('selected_video_timestamp_out_of_bounds')
    # Integer arithmetic: never upscale, never derive a filter from raw user text.
    numerator, denominator = min((1, 1), (selection.max_width, selected.width),
                                 (selection.max_height, selected.height), key=lambda ratio:Decimal(ratio[0])/ratio[1])
    width = max(1, selected.width*numerator//denominator)
    height = max(1, selected.height*numerator//denominator)
    tool = _tool('ffmpeg')
    try:
        from app.model_media_dependencies import require_python_dependency
        pillow = require_python_dependency('pillow')
    except Exception:
        _fail('reviewed_pillow_dependency_unavailable')
    notes = ('仅输出明确时间点的静态PNG帧；丢弃所有音频、字幕、附件和其他视频流。',
             '丢失连续运动、帧间变化、音画同步和未选择时间段；不代表完整视频理解。',
             '时间点为相对视频起点的请求定位；准确寻址后取首个可用帧，并非原始帧PTS或逐帧精确定位。',
             '按编码像素方向提取，不自动旋转；等比缩小且不放大，转换为8位RGB，可能损失色彩、HDR和元数据。',
             '转换批准不等于模型发送批准；必须再预览实际输出帧并批准最终模型、交付方式和内容。')
    settings = _digest({'converter':VERSION, 'selection':asdict(selection),
        'output_width':width, 'output_height':height, 'probe_tool':asdict(probe.tool),
        'conversion_tool':asdict(tool.identity), 'pillow_version':pillow,
        'recipe':'fixed-accurate-input-seek-single-map-noautorotate-scale-lanczos-reset-sar-rgb24-png-pipe-v2',
        # Bind the complete fixed argv, with only the random scratch root normalized.
        'probe_argv':_probe_arguments(Path('HOST_ORIGINAL.'+probe.kind), probe.kind),
        'frame_argv':[_frame_arguments(Path('HOST_ORIGINAL.'+probe.kind), probe.kind,
            selection, timestamp, width, height) for timestamp in selection.timestamps_ms],
        'limits':{'process_memory':MAX_PROCESS_MEMORY, 'frame_bytes':MAX_FRAME_BYTES,
                  'result_bytes':MAX_RESULT_BYTES, 'frame_timeout':FRAME_TIMEOUT_SECONDS,
                  'conversion_timeout':MAX_CONVERSION_SECONDS}, 'loss_notes':notes})
    fingerprint = _digest({'original_id':original_id, 'original_version':original_version,
        'original_sha256':probe.original_sha256, 'filename':filename, 'mime':mime,
        'inspection':probe.fingerprint, 'settings':settings,
        'request_original_count':request_original_count, 'request_total_video_frames':total})
    return VideoConversionPlan(original_id, original_version, probe.original_sha256, filename, mime,
        len(raw), probe.fingerprint, selection, width, height, selection.frame_count,
        request_original_count, total, probe.tool, tool.identity, pillow, settings, fingerprint, notes)


def _frame_arguments(path, kind, selection, timestamp, width, height):
    # All strings apart from host-generated local input are constants or decimal
    # integers validated above. No URL, playlist, protocol/filter/template injection.
    seconds = str(timestamp//1000)+'.'+str(timestamp%1000).zfill(3)
    return ['-hide_banner', '-v', 'error', '-nostdin', '-xerror', '-max_alloc', '67108864',
            '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm',
            *_input_format_arguments(kind), '-threads', '1', '-max_pixels', str(MAX_SOURCE_PIXELS),
            '-probesize', str(MAX_ORIGINAL_BYTES), '-analyzeduration', '5000000',
            '-max_probe_packets', '4096', '-ss', seconds, '-accurate_seek', '-noautorotate',
            '-i', str(path), '-map', '0:'+str(selection.stream_index), '-an', '-sn', '-dn',
            '-map_metadata', '-1', '-map_chapters', '-1', '-frames:v', '1',
            '-filter_threads', '1', '-filter_complex_threads', '1',
            '-vf', 'scale='+str(width)+':'+str(height)+':flags=lanczos:reset_sar=1',
            '-c:v', 'png', '-threads:v', '1', '-pix_fmt', 'rgb24', '-compression_level', '6',
            '-fflags', '+bitexact', '-flags:v', '+bitexact',
            '-protocol_whitelist', 'pipe', '-f', 'image2pipe', 'pipe:1']


def _validate_output_png(raw, width, height):
    """Exact single RGB PNG structure/CRC/deflate length AND actual pinned Pillow decode."""
    if not 0 < len(raw) <= MAX_FRAME_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        _fail('video_output_png_invalid')
    offset, chunks, header, ended, data_closed = 8, 0, False, False, False
    compressed = bytearray()
    while offset < len(raw):
        if offset+12 > len(raw) or ended:
            _fail('video_output_png_trailing_or_incomplete')
        length = int.from_bytes(raw[offset:offset+4], 'big')
        typ = raw[offset+4:offset+8]
        if length > MAX_FRAME_BYTES or offset+length+12 > len(raw):
            _fail('video_output_png_chunk_budget')
        body = raw[offset+8:offset+8+length]
        expected_crc = int.from_bytes(raw[offset+8+length:offset+12+length], 'big')
        if zlib.crc32(typ+body) & 0xffffffff != expected_crc:
            _fail('video_output_png_crc_mismatch')
        chunks += 1
        if chunks > 1024 or chunks == 1 and typ != b'IHDR':
            _fail('video_output_png_structure')
        if typ == b'IHDR':
            if header or length != 13 or struct.unpack('>IIBBBBB', body) != (width, height, 8, 2, 0, 0, 0):
                _fail('video_output_png_dimensions_or_format_changed')
            header = True
        elif typ == b'IDAT':
            if not header or data_closed or not length:
                _fail('video_output_png_structure')
            compressed.extend(body)
        elif typ == b'IEND':
            if length or not compressed:
                _fail('video_output_png_structure')
            ended = True
        elif typ == b'pHYs':
            if length != 9 or compressed:
                _fail('video_output_png_structure')
        else:
            # Generated images need no external profile/text/script/APNG/EXIF.
            _fail('video_output_png_unexpected_chunk')
        if typ != b'IDAT' and compressed:
            data_closed = True
        offset += length+12
    if not header or not ended:
        _fail('video_output_png_incomplete')
    expected = (width*3+1)*height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected+1)
        if (len(pixels) != expected or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or any(pixels[row*(width*3+1)] > 4 for row in range(height))):
            _fail('video_output_png_decoded_size_or_filter')
    except zlib.error:
        _fail('video_output_png_deflate_invalid')
    try:
        from app.model_media_dependencies import require_python_dependency
        require_python_dependency('pillow')
        import warnings
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if (image.format != 'PNG' or image.size != (width, height)
                        or image.mode != 'RGB' or getattr(image, 'n_frames', 1) != 1):
                    _fail('video_output_png_pillow_mismatch')
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                if image.size != (width, height) or image.mode != 'RGB':
                    _fail('video_output_png_pillow_mismatch')
    except VideoExtractionError:
        raise
    except Exception:
        _fail('video_output_png_real_decode_failed')


def extract_video_frames(filename, mime, raw, selection, *, original_id, original_version,
                         request_original_count, other_video_frames, expected_raw_sha,
                         expected_inspection_fingerprint, expected_plan_fingerprint,
                         user_confirmed_conversion):
    """Run FFmpeg only after explicit approval bound to held host bytes and plan.

    Host must recheck source owner/chat scope before/after this call, store the typed
    result or re-extract it in its registry, and enforce the shared request budget
    atomically. A browser-supplied dataclass/JSON result is never authority.
    """
    if (user_confirmed_conversion is not True or any(type(value) is not str or not _SHA.fullmatch(value)
            for value in (expected_raw_sha, expected_inspection_fingerprint, expected_plan_fingerprint))):
        _fail('video_conversion_confirmation_required')
    started = time.monotonic()
    plan = preview_video_conversion(filename, mime, raw, selection, original_id=original_id,
        original_version=original_version, request_original_count=request_original_count,
        other_video_frames=other_video_frames)
    if (expected_raw_sha != plan.original_sha256
            or expected_inspection_fingerprint != plan.inspection_fingerprint
            or expected_plan_fingerprint != plan.fingerprint):
        _fail('video_conversion_confirmation_changed')
    tool = _tool('ffmpeg')
    if tool.identity != plan.conversion_tool:
        _fail('reviewed_media_dependency_changed')
    kind = _source(filename, mime, raw)
    parts, total, cleanups = [], 0, []
    for frame, timestamp in enumerate(selection.timestamps_ms, 1):
        remaining = MAX_CONVERSION_SECONDS-(time.monotonic()-started)
        if remaining <= 0:
            _fail('video_conversion_total_timeout')
        output, cleanup = _run_with_input(tool, raw, kind,
            lambda path:_frame_arguments(path, kind, selection, timestamp, plan.output_width, plan.output_height),
            timeout=min(FRAME_TIMEOUT_SECONDS, remaining), stdout_limit=min(MAX_FRAME_BYTES, MAX_RESULT_BYTES-total))
        _validate_output_png(output, plan.output_width, plan.output_height)
        total += len(output)
        if total > MAX_RESULT_BYTES or time.monotonic()-started >= MAX_CONVERSION_SECONDS:
            _fail('video_conversion_output_or_total_time_budget')
        cleanups.append(cleanup)
        parts.append(VideoProcessedPart(output, _hash(output), 'image/png', plan.output_width,
            plan.output_height, (('timestamp_ms',str(timestamp)), ('frame',str(frame))),
            original_id, original_version, plan.original_sha256, plan.conversion_settings_sha256))
    return VideoExtractionResult(tuple(parts), plan.original_sha256, original_id, original_version,
        plan.inspection_fingerprint, plan.fingerprint, plan.conversion_settings_sha256,
        len(parts), total, plan.loss_notes, tuple(cleanups))


def native_video_attachment(filename, mime, raw, *, original_id, original_version,
                            expected_inspection_fingerprint):
    """Host-only native material helper: re-probe actual bytes, no transcoding.

    The registry must still bind source ownership/current bytes and a separate
    material approval. This is not a route capability proof or final send grant.
    """
    _identity(original_id, original_version)
    probe = probe_video(filename, mime, raw)
    if expected_inspection_fingerprint != probe.fingerprint:
        _fail('native_video_inspection_changed')
    from app.model_submission import AttachmentPart, InspectionReceipt
    settings = _digest({'inspector':VERSION, 'probe':probe.fingerprint,
                        'original_id':original_id, 'original_version':original_version})
    notes = ('完整原始MP4/WebM字节，未转码、抽帧或默换交付方式；未删除原件中的音频。',
             '仅受控ffprobe验证容器、编码、时长和流；不声称全部媒体包已解码或提供方原生能力已实测。')
    return AttachmentPart(original_id, probe.original_sha256, original_version, 'video', mime,
        len(raw), 'video', mime, raw, probe.original_sha256,
        InspectionReceipt(probe.original_sha256, original_version, probe.original_sha256,
                          VERSION, '1', settings, True),
        conversion_settings_sha256=settings, loss_notes=notes)
