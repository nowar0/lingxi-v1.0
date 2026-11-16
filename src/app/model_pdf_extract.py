"""Fixed read-only PDF parser in a Windows host document-safety Job.

This is NOT the plugin AppContainer/sandbox and grants no plugin capability.
Import is inert. No parser, worker, dependency probe or file read runs on import.
The source runner uses only this interpreter and this fixed entry, never PATH.

Frozen entry integration (before GUI/server imports, including on normal boot)::

    from app.model_pdf_extract import dispatch_pdf_worker
    dispatch_pdf_worker()  # exits only for the exact internal worker switch

Calling that dispatcher on normal boot arms the frozen host entry. Without it,
frozen PDF parsing fails closed; it must not launch the normal GUI as a worker.
Package this module and the reviewed pypdf dependency/provider inventory. Do not
route it through plugin launchers or AppContainer grants. No OCR/rendering exists.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

PDF_VERSION = 'pypdf-6.21.4-text-v1-job-v1'
PDF_LIMITS = {
    'max_pdf_pages': 200, 'max_pdf_objects': 20000, 'max_pdf_nodes': 100000,
    'max_pdf_depth': 64, 'max_pdf_stream_bytes': 4 * 1024**2,
    'max_pdf_decoded_work_bytes': 32 * 1024**2, 'max_pdf_characters': 100000,
    'max_pdf_memory_bytes': 384 * 1024**2, 'max_pdf_user_cpu_seconds': 6,
    'max_pdf_processes': 2,  # Windows venv redirector + its fixed interpreter
    'max_pdf_wall_seconds': 10, 'max_pdf_output_bytes': 1024**2,
}
PDF_LOSS_NOTES = (
    '仅按明确批准的1起始页码提取PDF已有文字层；页码是物理页顺序，不是印刷页码。未选页不提取正文。',
    '不保留分页内版式、字体、颜色、阅读顺序或表格结构；多栏、连字、字体编码与公式可能丢失或错误，公式不计算。',
    '图片、扫描页、图表、图形与手写内容不转文字；没有OCR。空文字结果不证明页面无信息。',
    '不提取批注、表单字段值、附件或文档信息字段；拒绝加密、脚本、XFA、嵌入文件、动作及外部文件引用；不打开或执行链接、脚本、宏或外部对象。',
    '只支持受限的只读PDF子集；未知结构/解码器、损坏文件或任一资源超限均拒绝，绝不静默截断。',
)
_SWITCH = '--lingxi-host-pdf-worker-v1'
_FROZEN_ENTRY_READY = False
_WORKER_FAILURE = None
_MAX_SOURCE = 15 * 1024**2
_CACHE_ENTRIES, _CACHE_BYTES = 64, 16 * 1024**2
_CACHE = {}
_CACHE_SIZE = 0
_LOCK = threading.RLock()
_SHA = re.compile(r'^[0-9a-f]{64}$')


class PdfExtractionError(ValueError):
    """A bounded stable diagnostic code, never parser text or attachment content."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fail(code):
    global _WORKER_FAILURE
    # pypdf deliberately catches some XForm/font exceptions. A sticky code
    # prevents a swallowed resource failure from becoming partial success.
    _WORKER_FAILURE = _WORKER_FAILURE or code
    raise PdfExtractionError(code)


def _pages(value, *, required):
    if (type(value) is not tuple or len(value) > PDF_LIMITS['max_pdf_pages']
            or any(type(page) is not int or not 1 <= page <= PDF_LIMITS['max_pdf_pages'] for page in value)
            or len(set(value)) != len(value) or tuple(sorted(value)) != value
            or required and not value):
        _fail('pdf_explicit_ordered_pages_required')
    return value


def _checked_metadata(value):
    ceilings = {'pages':200, 'objects':20000, 'nodes':100000,
                'decoded_work_bytes':32 * 1024**2, 'image_objects':20000}
    if (type(value) is not dict or set(value) != set(ceilings)
            or any(type(value[key]) is not int or not 0 <= value[key] <= maximum
                   for key, maximum in ceilings.items()) or value['pages'] < 1):
        _fail('pdf_worker_metadata_invalid')
    return value


def _require_dependency():
    # Keep the shared provider as the sole inventory/layout authority. In
    # addition to its pinned METADATA check, require the reviewed BSD license
    # approval and exact bundled notice bytes; never edit pins or install here.
    from app.model_media_dependencies import (require_python_dependency, _manifest,
                                               _relative, _verify)
    if require_python_dependency('pypdf') != '6.21.4':
        _fail('pdf_reviewed_dependency_unavailable')
    manifest, root, _ = _manifest()
    package = next(item for item in manifest['python_dependencies'] if item['name']=='pypdf')
    if (package['version'] != '6.21.4' or package['license_expression'] != 'BSD-3-Clause'
            or package['redistribution_status'] != 'approved'):
        _fail('pdf_reviewed_license_unavailable')
    record = package['license_file']
    _verify(root / _relative(record['path']), record)


def _request(raw, mode, pages=(), max_characters=100000, expected_metadata=None):
    """Cache immutable receipts and failure tombstones; never retry a parser failure.

    The bounded process-global lifetime has no eviction/retry fallback. Capacity
    exhaustion fails closed until the host service lifetime is explicitly renewed.
    Only hashes, diagnostics, metadata and approved extracted text are retained.
    """
    global _CACHE_SIZE
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_SOURCE:
        _fail('pdf_source_byte_limit')
    if mode not in ('inspect', 'extract'):
        _fail('pdf_worker_mode_invalid')
    _pages(pages, required=mode == 'extract')
    if mode == 'inspect' and pages:
        _fail('pdf_inspection_is_metadata_only')
    if type(max_characters) is not int or not 1 <= max_characters <= 100000:
        _fail('pdf_character_limit_invalid')
    sha = _hash(raw)
    key = (PDF_VERSION, sha, mode, pages, max_characters,
           _hash(_json(expected_metadata)) if expected_metadata is not None else None)
    with _LOCK:
        # A failed structural inspection also blocks conversion without launching
        # another parser. Approved extraction receipts bind the exact selection.
        inspection_key = (PDF_VERSION, sha, 'inspect', (), 100000, None)
        # Any parser/worker failure becomes a source+version tombstone, even if
        # a client changes pages/budget. No failed readonly parse is auto-retried.
        old = _CACHE.get(inspection_key)
        if old is not None and old[0] == 'error':
            _fail(old[1])
        if mode == 'extract' and (old is None or json.loads(old[1])['metadata'] != expected_metadata):
            _fail('pdf_host_inspection_receipt_required')
        old = _CACHE.get(key) or (old if mode == 'extract' else None)
        if old is not None:
            if old[0] == 'error':
                _fail(old[1])
            if key in _CACHE:
                return json.loads(old[1])
        if len(_CACHE) >= _CACHE_ENTRIES or _CACHE_SIZE + PDF_LIMITS['max_pdf_output_bytes'] > _CACHE_BYTES:
            _fail('pdf_receipt_capacity_no_retry')
        try:
            _require_dependency()
        except PdfExtractionError as error:
            _CACHE[key] = ('error', error.code)
            previous = _CACHE.get(inspection_key)
            if previous is not None and previous[0] == 'ok':
                _CACHE_SIZE -= len(previous[1])
            _CACHE[inspection_key] = ('error', error.code)
            raise
        except Exception:
            code = 'pdf_reviewed_dependency_unavailable'
            _CACHE[key] = ('error', code)
            previous = _CACHE.get(inspection_key)
            if previous is not None and previous[0] == 'ok':
                _CACHE_SIZE -= len(previous[1])
            _CACHE[inspection_key] = ('error', code)
            _fail(code)
        try:
            value = _run_job(raw, mode, pages, max_characters, expected_metadata)
            encoded = _json(value)
            _CACHE[key] = ('ok', encoded)
            _CACHE_SIZE += len(encoded)
            return json.loads(encoded)
        except PdfExtractionError as error:
            _CACHE[key] = ('error', error.code)
            previous = _CACHE.get(inspection_key)
            if previous is not None and previous[0] == 'ok':
                _CACHE_SIZE -= len(previous[1])
            _CACHE[inspection_key] = ('error', error.code)
            raise
        except Exception:
            code = 'pdf_worker_host_failure_no_retry'
            _CACHE[key] = ('error', code)
            previous = _CACHE.get(inspection_key)
            if previous is not None and previous[0] == 'ok':
                _CACHE_SIZE -= len(previous[1])
            _CACHE[inspection_key] = ('error', code)
            _fail(code)


def inspect_pdf_bytes(raw):
    """Metadata only: no extract_text call or conversion in this mode."""
    return _request(raw, 'inspect')['metadata']


def extract_pdf_pages(raw, pages, max_characters, *, expected_metadata):
    """Host-only approved conversion path; caller owns the bound approval token."""
    _checked_metadata(expected_metadata)
    return _request(raw, 'extract', pages, max_characters, expected_metadata)


def dispatch_pdf_worker():
    """Call early and unconditionally in a frozen entry, not in request handlers."""
    global _FROZEN_ENTRY_READY
    if len(sys.argv) > 1 and sys.argv[1] == _SWITCH:
        raise SystemExit(_worker_main())
    _FROZEN_ENTRY_READY = True


def _windows_api():
    # Deliberately independent from all plugin launcher / capability code.
    import ctypes as C
    from ctypes import wintypes as W
    class Basic(C.Structure):
        _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong),
                    ('flags', W.DWORD), ('minimum_working_set', C.c_size_t),
                    ('maximum_working_set', C.c_size_t), ('active_processes', W.DWORD),
                    ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]
    class IO(C.Structure):
        _fields_ = [(key, C.c_ulonglong) for key in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(C.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', C.c_size_t),
                    ('job_memory', C.c_size_t), ('peak_process_memory', C.c_size_t), ('peak_job_memory', C.c_size_t)]
    class Startup(C.Structure):
        _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR),
                    ('title', W.LPWSTR), ('x',W.DWORD), ('y',W.DWORD), ('x_size',W.DWORD),
                    ('y_size',W.DWORD), ('x_chars',W.DWORD), ('y_chars',W.DWORD),
                    ('fill',W.DWORD), ('flags',W.DWORD), ('show',W.WORD), ('reserved_size',W.WORD),
                    ('reserved_bytes',C.POINTER(C.c_byte)), ('stdin',W.HANDLE), ('stdout',W.HANDLE), ('stderr',W.HANDLE)]
    class Process(C.Structure):
        _fields_ = [('process',W.HANDLE), ('thread',W.HANDLE), ('pid',W.DWORD), ('tid',W.DWORD)]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    def bind(name, args, result):
        function = getattr(kernel, name); function.argtypes = args; function.restype = result
        return function
    P = C.c_void_p
    bind('CreateJobObjectW',[P,W.LPCWSTR],W.HANDLE)
    bind('SetInformationJobObject',[W.HANDLE,C.c_int,P,W.DWORD],W.BOOL)
    bind('QueryInformationJobObject',[W.HANDLE,C.c_int,P,W.DWORD,P],W.BOOL)
    bind('IsProcessInJob',[W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)],W.BOOL)
    bind('GetCurrentProcess',[],W.HANDLE)
    bind('CreateProcessW',[W.LPCWSTR,W.LPWSTR,P,P,W.BOOL,W.DWORD,P,W.LPCWSTR,C.POINTER(Startup),C.POINTER(Process)],W.BOOL)
    bind('AssignProcessToJobObject',[W.HANDLE,W.HANDLE],W.BOOL)
    bind('ResumeThread',[W.HANDLE],W.DWORD)
    bind('WaitForSingleObject',[W.HANDLE,W.DWORD],W.DWORD)
    bind('GetExitCodeProcess',[W.HANDLE,C.POINTER(W.DWORD)],W.BOOL)
    bind('TerminateJobObject',[W.HANDLE,W.UINT],W.BOOL)
    bind('TerminateProcess',[W.HANDLE,W.UINT],W.BOOL)
    bind('CloseHandle',[W.HANDLE],W.BOOL)
    return C, W, kernel, Extended, Startup, Process


# User-mode CPU time + fixed process-count/commit-memory + kill-on-close limits; the
# parent additionally enforces wall time and confirms actual process exit.
_JOB_FLAGS = 0x2 | 0x4 | 0x8 | 0x100 | 0x200 | 0x400 | 0x2000


def _job_drained(C, kernel, job, *, timeout=5):
    class Accounting(C.Structure):
        _fields_ = [('user',C.c_longlong),('kernel',C.c_longlong),
                    ('period_user',C.c_longlong),('period_kernel',C.c_longlong),
                    ('page_faults',C.c_ulong),('processes',C.c_ulong),
                    ('active',C.c_ulong),('terminated',C.c_ulong)]
    end = time.monotonic()+timeout
    while True:
        accounting = Accounting()
        if not kernel.QueryInformationJobObject(job,1,C.byref(accounting),C.sizeof(accounting),None):
            return False
        if accounting.active == 0:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.01)


def _run_job(raw, mode, pages, max_characters, expected_metadata):
    if sys.platform != 'win32':
        _fail('pdf_windows_host_job_required')
    if getattr(sys, 'frozen', False) and not _FROZEN_ENTRY_READY:
        _fail('pdf_frozen_worker_entry_not_connected')
    C, W, kernel, Extended, Startup, Process = _windows_api()
    folder = Path(tempfile.mkdtemp(prefix='lingxi-host-pdf-'))
    job = None; process = Process(); assigned = False; exited = False
    deadline = time.monotonic() + PDF_LIMITS['max_pdf_wall_seconds']
    try:
        nonce = secrets.token_hex(16)
        request = {'mode':mode, 'pages':list(pages), 'max_characters':max_characters,
                   'source_sha256':_hash(raw), 'nonce':nonce, 'converter':PDF_VERSION,
                   'expected_metadata':expected_metadata}
        (folder/'source.pdf').write_bytes(raw)
        (folder/'request.json').write_bytes(_json(request))
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            _fail('pdf_job_creation_failed')
        limits = Extended(); limits.basic.flags = _JOB_FLAGS
        limits.basic.process_time = limits.basic.job_time = PDF_LIMITS['max_pdf_user_cpu_seconds'] * 10_000_000
        # A Windows venv python.exe can be a redirector that starts exactly one
        # base interpreter. Both stay in this Job and share the same job budget.
        limits.basic.active_processes = 1 if getattr(sys, 'frozen', False) else 2
        limits.process_memory = limits.job_memory = PDF_LIMITS['max_pdf_memory_bytes']
        action = W.DWORD(0)  # JOB_OBJECT_TERMINATE_AT_END_OF_JOB
        if (not kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits))
                or not kernel.SetInformationJobObject(job, 6, C.byref(action), C.sizeof(action))):
            _fail('pdf_job_limits_unavailable')
        if getattr(sys, 'frozen', False):
            command = [sys.executable, _SWITCH, str(folder)]
        else:
            root = str(Path(__file__).resolve().parents[1])
            bootstrap = ('import sys;sys.path.insert(0,'+repr(root)+');'
                         'from app.model_pdf_extract import _worker_main;raise SystemExit(_worker_main())')
            command = [sys.executable, '-I', '-c', bootstrap, _SWITCH, str(folder)]
        # No shell, PATH search, inherited handles, user-selected executable or
        # user environment overrides. The worker is suspended before any Python.
        environment = {'SystemRoot':os.environ.get('SystemRoot', r'C:\Windows'),
                       'TEMP':str(folder), 'TMP':str(folder)}
        env = C.create_unicode_buffer('\x00'.join(key+'='+value for key,value in sorted(environment.items()))+'\x00\x00')
        startup = Startup(); startup.cb = C.sizeof(startup)
        command_line = C.create_unicode_buffer(subprocess.list2cmdline(command))
        if not kernel.CreateProcessW(sys.executable, command_line, None, None, False,
                0x4 | 0x400 | 0x08000000, env, str(folder), C.byref(startup), C.byref(process)):
            _fail('pdf_worker_start_failed')
        if not kernel.AssignProcessToJobObject(job, process.process):
            _fail('pdf_job_assignment_failed')
        assigned = True
        if kernel.ResumeThread(process.thread) == 0xffffffff:
            _fail('pdf_worker_resume_failed')
        remaining = max(0, int((deadline-time.monotonic())*1000))
        waited = kernel.WaitForSingleObject(process.process, remaining)
        if waited != 0:
            _fail('pdf_worker_wall_limit_terminated' if waited == 258 else 'pdf_worker_wait_failed')
        exited = True
        code = W.DWORD()
        if not kernel.GetExitCodeProcess(process.process, C.byref(code)) or code.value != 0:
            _fail('pdf_worker_resource_limit_or_exit_failure')
        if not _job_drained(C, kernel, job, timeout=max(0,deadline-time.monotonic())):
            _fail('pdf_worker_descendant_exit_unconfirmed')
        path = folder/'result.json'
        if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size > PDF_LIMITS['max_pdf_output_bytes']:
            _fail('pdf_worker_output_limit_or_missing')
        with path.open('rb') as stream:
            encoded = stream.read(PDF_LIMITS['max_pdf_output_bytes']+1)
        if len(encoded) > PDF_LIMITS['max_pdf_output_bytes']:
            _fail('pdf_worker_output_limit_or_missing')
        result = json.loads(encoded)
        if (type(result) is not dict or set(result) != {'nonce','source_sha256','converter','ok','code','metadata','pages','characters'}
                or result['nonce'] != nonce or result['source_sha256'] != request['source_sha256']
                or result['converter'] != PDF_VERSION or type(result['ok']) is not bool):
            _fail('pdf_worker_response_unbound')
        if not result['ok']:
            diagnostic = result['code']
            if type(diagnostic) is not str or not re.fullmatch(r'pdf_[a-z0-9_]{1,100}', diagnostic):
                _fail('pdf_worker_diagnostic_invalid')
            _fail(diagnostic)
        if result['code'] is not None:
            _fail('pdf_worker_response_invalid')
        metadata = _checked_metadata(result['metadata'])
        if expected_metadata is not None and metadata != expected_metadata:
            _fail('pdf_structure_changed_after_inspection')
        if type(result['pages']) is not list or type(result['characters']) is not int:
            _fail('pdf_worker_response_invalid')
        if mode == 'inspect':
            if result['pages'] or result['characters'] != 0:
                _fail('pdf_inspection_returned_converted_text')
        else:
            if len(result['pages']) != len(pages):
                _fail('pdf_worker_page_selection_changed')
            characters = 0
            for page, selected in zip(result['pages'], pages):
                if (type(page) is not dict or set(page) != {'page','text'} or type(page['page']) is not int
                        or page['page'] != selected or type(page['text']) is not str):
                    _fail('pdf_worker_page_selection_changed')
                characters += len(page['text'])
            if characters != result['characters'] or not 0 <= characters <= max_characters:
                _fail('pdf_character_limit_no_truncation')
        return {'metadata':metadata, 'pages':result['pages'], 'characters':result['characters']}
    finally:
        if process.process and not exited:
            # This is real process termination, NOT a thread timeout. Never report
            # the timeout code unless the OS confirms the process has exited.
            if assigned and job:
                kernel.TerminateJobObject(job, 1)
            else:
                kernel.TerminateProcess(process.process, 1)  # still suspended
            exited = kernel.WaitForSingleObject(process.process, 5000) == 0
        job_exited = True
        if job and assigned:
            # Confirm the redirector AND every descendant, not just the first PID.
            # Closing a Job kills descendants but alone is not exit confirmation.
            kernel.TerminateJobObject(job, 1)
            job_exited = _job_drained(C, kernel, job)
        if process.thread: kernel.CloseHandle(process.thread)
        if job: kernel.CloseHandle(job)  # KILL_ON_JOB_CLOSE, including error paths
        if process.process:
            if not exited:
                exited = kernel.WaitForSingleObject(process.process, 5000) == 0
            kernel.CloseHandle(process.process)
        if (exited or not process.process) and job_exited:
            shutil.rmtree(folder, ignore_errors=True)
        if process.process and (not exited or not job_exited):
            # Keep the private temporary input rather than race a live process;
            # no success/termination claim is permitted for this diagnostic.
            _fail('pdf_worker_termination_unconfirmed')


def _worker_job_guard():
    if sys.platform != 'win32':
        _fail('pdf_windows_host_job_required')
    C, W, kernel, Extended, _, _ = _windows_api()
    inside = W.BOOL()
    info = Extended()
    if (not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, C.byref(inside)) or not inside.value
            or not kernel.QueryInformationJobObject(None, 9, C.byref(info), C.sizeof(info), None)
            or info.basic.flags & _JOB_FLAGS != _JOB_FLAGS or not 1 <= info.basic.active_processes <= 2
            or not 0 < info.basic.process_time <= PDF_LIMITS['max_pdf_user_cpu_seconds']*10_000_000
            or not 0 < info.basic.job_time <= PDF_LIMITS['max_pdf_user_cpu_seconds']*10_000_000
            or not 0 < info.process_memory <= PDF_LIMITS['max_pdf_memory_bytes']
            or not 0 < info.job_memory <= PDF_LIMITS['max_pdf_memory_bytes']):
        _fail('pdf_worker_job_limits_not_confirmed')


def _parse_in_worker(raw, mode, pages, max_characters, expected_metadata):
    """Never call in the host. Guard is repeated before importing the parser."""
    _worker_job_guard()
    _require_dependency()
    def deny_execution(event, args):
        if (event.startswith(('subprocess.', 'socket.', 'os.exec', 'os.spawn'))
                or event in ('os.system','os.startfile','os.startfile/2','os.posix_spawn','os.fork','os.forkpty')):
            _fail('pdf_worker_external_execution_forbidden')
    # Defense in depth for this fixed read-only Python entry; not an OS security
    # sandbox or a plugin grant. The Job remains the actual resource boundary.
    sys.addaudithook(deny_execution)
    import logging
    import warnings
    import zlib
    class RejectDiagnostics(logging.Handler):
        def emit(self, record):
            global _WORKER_FAILURE
            _WORKER_FAILURE = _WORKER_FAILURE or 'pdf_parser_warning_rejected_no_partial_text'
            # Never retain/format record.msg/args, which can contain document data.
    logging.disable(logging.NOTSET)
    parser_log = logging.getLogger('pypdf')
    parser_log.handlers = [RejectDiagnostics()]
    parser_log.setLevel(logging.WARNING)
    parser_log.propagate = False
    sys.setrecursionlimit(384)
    class ReviewedSubsetImports:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.',1)[0] in {'fontTools','PIL','brotli','cryptography','Crypto'}:
                raise ModuleNotFoundError('optional PDF parser dependency is outside reviewed text subset')
            return None
    sys.meta_path.insert(0, ReviewedSubsetImports())
    from pypdf import PdfReader, apply_configuration
    from pypdf import filters, _reader as reader_module
    from pypdf.generic import _data_structures as structures
    from pypdf.generic import _font as font_module
    font_module.HAS_FONTTOOLS = False
    from pypdf.generic import (ArrayObject, DictionaryObject, IndirectObject,
                              StreamObject, EncodedStreamObject, DecodedStreamObject, NullObject)
    original_read_object = structures.read_object
    parsing_depth = [0]
    def read_object(*args, **kwargs):
        parsing_depth[0] += 1
        try:
            if parsing_depth[0] > PDF_LIMITS['max_pdf_depth']:
                _fail('pdf_parser_recursion_limit')
            return original_read_object(*args, **kwargs)
        finally:
            parsing_depth[0] -= 1
    structures.read_object = reader_module.read_object = read_object
    # ALL decoding is restricted before PdfReader construction (including xref
    # and object streams). No JBIG2 helper, image decoder or external tool exists.
    work = [0]; accounted = set(); held = []
    def account(data, token=None):
        if type(data) is not bytes or len(data) > PDF_LIMITS['max_pdf_stream_bytes']:
            _fail('pdf_stream_decoded_byte_limit')
        if token is None or id(token) not in accounted:
            work[0] += len(data)
            if work[0] > PDF_LIMITS['max_pdf_decoded_work_bytes']:
                _fail('pdf_aggregate_decoded_work_limit')
            if token is not None: accounted.add(id(token)); held.append(token)
        return data
    def strict_decompress(data):
        limit = min(PDF_LIMITS['max_pdf_stream_bytes'], PDF_LIMITS['max_pdf_decoded_work_bytes']-work[0])
        decoder = zlib.decompressobj()
        value = decoder.decompress(data, limit+1)
        if len(value)>limit or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            _fail('pdf_flate_stream_limit_or_invalid')
        return account(value)  # count decompressed bytes before predictor work
    def resolved(value):
        return value.get_object() if isinstance(value, IndirectObject) else value
    def filter_list(stream):
        value = resolved(stream.get('/Filter', ()))
        values = list(value) if isinstance(value, (ArrayObject, list, tuple)) else [value]
        if len(values)>2: _fail('pdf_filter_chain_limit')
        return [resolved(value) for value in values]
    allowed = {'/FlateDecode','/ASCIIHexDecode','/ASCII85Decode'}
    image_filters = allowed | {'/DCTDecode','/JPXDecode','/CCITTFaxDecode','/JBIG2Decode','/RunLengthDecode','/LZWDecode'}
    def check_stream(stream):
        if any(key in stream for key in ('/F','/FFilter','/FDecodeParms')):
            _fail('pdf_external_stream_forbidden')
        names = filter_list(stream)
        accepted = image_filters if stream.get('/Subtype') == '/Image' else allowed
        if any(name not in accepted for name in names):
            _fail('pdf_filter_unsupported_or_external')
        if len(stream._data) > _MAX_SOURCE: _fail('pdf_encoded_stream_byte_limit')
        if resolved(stream.get('/Type')) == '/ObjStm':
            count = resolved(stream.get('/N'))
            if not isinstance(count, int) or not 0 <= count <= PDF_LIMITS['max_pdf_objects']:
                _fail('pdf_object_stream_entry_limit')
        return names
    def decode(stream):
        if stream.get('/Subtype') == '/Image': _fail('pdf_image_decode_not_allowed')
        names = check_stream(stream)
        parameters = resolved(stream.get('/DecodeParms'))
        if parameters is None or isinstance(parameters, NullObject):
            parameters = [None]*len(names)
        elif isinstance(parameters, ArrayObject):
            parameters = list(parameters)
        else:
            parameters = [parameters]
        if len(parameters) != len(names): _fail('pdf_decode_parameters_invalid')
        data = stream._data
        if len(data) > PDF_LIMITS['max_pdf_stream_bytes']: _fail('pdf_stream_byte_limit')
        if not names: return account(data)
        for name, param in zip(names, parameters):
            param = resolved(param)
            if param is not None and not isinstance(param, (DictionaryObject, NullObject)):
                _fail('pdf_decode_parameters_invalid')
            if name == '/FlateDecode': data = filters.FlateDecode.decode(data, param)
            elif name == '/ASCIIHexDecode': data = filters.ASCIIHexDecode.decode(data)
            elif name == '/ASCII85Decode': data = filters.ASCII85Decode.decode(data)
            else: _fail('pdf_filter_unsupported_or_external')
            account(data)
        return data
    original_decoded = DecodedStreamObject.get_data
    original_encoded = EncodedStreamObject.get_data
    def decoded_data(stream):
        if stream.get('/Subtype') == '/Image': _fail('pdf_image_decode_not_allowed')
        check_stream(stream)
        return account(original_decoded(stream), stream)
    def encoded_data(stream):
        if stream.get('/Subtype') == '/Image': _fail('pdf_image_decode_not_allowed')
        check_stream(stream)
        return original_encoded(stream)
    filters.decompress = strict_decompress
    filters.decode_stream_data = decode
    DecodedStreamObject.get_data = decoded_data
    EncodedStreamObject.get_data = encoded_data
    class BoundedReader(PdfReader):
        def _read_standard_xref_table__handle_size(self, stream):
            size = super()._read_standard_xref_table__handle_size(stream)
            if not isinstance(size, int) or size < 0:
                _fail('pdf_standard_xref_invalid')
            self._host_xref_entries = getattr(self, '_host_xref_entries', 0)+size
            if self._host_xref_entries > PDF_LIMITS['max_pdf_objects']:
                _fail('pdf_xref_entry_limit')
            return size
        def _rebuild_xref_table(self, stream):
            _fail('pdf_xref_recovery_not_allowed')
        def _load_recovery_cache(self, data):
            _fail('pdf_xref_recovery_not_allowed')
        def _handle_encryption(self, password):
            _fail('pdf_encryption_forbidden')  # do not even try an empty password
        def get_object(self, reference):
            self._host_depth = getattr(self, '_host_depth', 0)+1
            if self._host_depth > PDF_LIMITS['max_pdf_depth']: _fail('pdf_reference_depth_limit')
            try:
                result = super().get_object(reference)
                if len(self.resolved_objects)>PDF_LIMITS['max_pdf_objects']: _fail('pdf_object_limit')
                return result
            finally:
                self._host_depth -= 1
        def cache_indirect_object(self, generation, idnum, obj):
            if (generation,idnum) not in self.resolved_objects and len(self.resolved_objects)>=PDF_LIMITS['max_pdf_objects']:
                _fail('pdf_object_limit')
            return super().cache_indirect_object(generation,idnum,obj)
    forbidden_keys = {'/Encrypt','/JS','/JavaScript','/XFA','/EmbeddedFiles','/EF','/AF',
                      '/OpenAction','/AA','/A','/FS','/RichMediaContent','/RichMediaSettings',
                      '/Collection','/Ref','/OPI','/URI','/RF','/UF','/DOS','/Mac','/Unix'}
    actions = {'/GoTo','/GoToR','/GoToE','/Launch','/Thread','/URI','/Sound','/Movie',
               '/Hide','/Named','/SubmitForm','/ResetForm','/ImportData','/JavaScript',
               '/SetOCGState','/Rendition','/Trans','/GoTo3DView'}
    with warnings.catch_warnings(), apply_configuration(
            maximum_declared_stream_length=_MAX_SOURCE,
            array_based_stream_maximum_output_length=PDF_LIMITS['max_pdf_stream_bytes'],
            array_maximum_element_count=20000, dictionary_maximum_element_count=20000,
            zlib_maximum_output_length=PDF_LIMITS['max_pdf_stream_bytes'],
            zlib_maximum_recovery_input_length=1, stream_filters_maximum_length=2,
            stream_decoding_work_maximum_length=8*1024**2, stream_parsing_work_maximum_count=100000,
            flate_maximum_columns=20000, flate_maximum_row_length=65536,
            page_tree_maximum_entries=400, page_tree_maximum_depth=64,
            page_tree_maximum_page_entries=20000, xform_maximum_invocations_per_extraction=128,
            jbig2dec_binary=None, disable_legacy_handling=True):
        warnings.simplefilter('error')
        reader = BoundedReader(io.BytesIO(raw), strict=True, root_object_recovery_limit=0,
                               xref_stream_maximum_entries=20000)
        if reader.is_encrypted: _fail('pdf_encryption_forbidden')
        refs = {(generation, number) for generation, entries in reader.xref.items()
                for number in entries if number != 0}
        refs.update((0, number) for number in reader.xref_objStm)
        if len(refs)>20000: _fail('pdf_object_limit')
        nodes = 0; images = 0; visited = set(); indirect = set()
        # Audit every xref object, not just the selected pages/catalog. Thus name
        # escapes and compressed object streams cannot hide actions/attachments.
        stack = [(reader.trailer,0)] + [(IndirectObject(number,generation,reader),0) for generation,number in sorted(refs)]
        while stack:
            value, depth = stack.pop(); nodes += 1
            if nodes>100000 or depth>64: _fail('pdf_object_graph_limit')
            if isinstance(value, IndirectObject):
                key = (value.generation,value.idnum)
                if key in indirect: continue
                indirect.add(key); stack.append((reader.get_object(value),depth+1)); continue
            if isinstance(value, (DictionaryObject, ArrayObject)):
                if id(value) in visited: continue
                visited.add(id(value))
            if isinstance(value, DictionaryObject):
                if forbidden_keys.intersection(value): _fail('pdf_active_embedded_or_external_forbidden')
                # Annotation /F is an integer flags field; file-spec /F strings
                # (including dictionaries without an explicit /Type) are refused.
                if '/F' in value and not isinstance(resolved(value.get('/F')), int):
                    _fail('pdf_external_file_reference_forbidden')
                if resolved(value.get('/Type')) in ('/Action','/Filespec','/EmbeddedFile'):
                    _fail('pdf_active_embedded_or_external_forbidden')
                if resolved(value.get('/S')) in actions or resolved(value.get('/Subtype')) in ('/FileAttachment','/RichMedia','/Movie','/Sound','/Screen','/3D'):
                    _fail('pdf_active_embedded_or_external_forbidden')
                if isinstance(value, StreamObject):
                    check_stream(value)
                    if value.get('/Subtype') == '/Image': images += 1
                if len(stack)+len(value)>100000: _fail('pdf_object_graph_limit')
                stack.extend((child,depth+1) for child in value.values())
            elif isinstance(value, ArrayObject):
                if len(stack)+len(value)>100000: _fail('pdf_object_graph_limit')
                stack.extend((child,depth+1) for child in value)
        page_count = len(reader.pages)
        if not 1 <= page_count <= 200: _fail('pdf_page_limit')
        metadata = {'pages':page_count,'objects':len(refs),'nodes':nodes,
                    'decoded_work_bytes':work[0],'image_objects':images}
        _checked_metadata(metadata)
        if expected_metadata is not None and metadata != expected_metadata:
            _fail('pdf_structure_changed_after_inspection')
        if mode == 'inspect':
            return {'metadata':metadata,'pages':[],'characters':0}
        if any(page>page_count for page in pages): _fail('pdf_selected_page_missing')
        result = []; characters = 0
        for page in pages:
            value = reader.pages[page-1].extract_text(extraction_mode='plain')
            if type(value) is not str: _fail('pdf_text_result_invalid')
            characters += len(value)
            if characters > max_characters: _fail('pdf_character_limit_no_truncation')
            result.append({'page':page,'text':value})
        return {'metadata':metadata,'pages':result,'characters':characters}


def _worker_main():
    """Fixed worker entry: only a private host-created folder, never browser paths."""
    if len(sys.argv) != 3 or sys.argv[1] != _SWITCH:
        return 64
    folder = Path(sys.argv[2])
    nonce = source_sha = None
    try:
        _worker_job_guard()
        if not folder.is_absolute() or not folder.is_dir(): return 64
        for path in (folder, *folder.parents, folder/'request.json', folder/'source.pdf'):
            if path.is_symlink() or path.is_junction() or getattr(path.lstat(),'st_file_attributes',0)&0x400:
                return 64
        with (folder/'request.json').open('rb') as stream:
            data = stream.read(16385)
        if len(data)>16384: return 64
        request = json.loads(data)
        if type(request) is not dict or set(request) != {'mode','pages','max_characters','source_sha256','nonce','converter','expected_metadata'}:
            return 64
        nonce, source_sha = request['nonce'], request['source_sha256']
        if (type(nonce) is not str or not re.fullmatch(r'[0-9a-f]{32}',nonce)
                or type(source_sha) is not str or not _SHA.fullmatch(source_sha)
                or request['converter'] != PDF_VERSION or request['mode'] not in ('inspect','extract')
                or type(request['pages']) is not list or type(request['max_characters']) is not int
                or not 1 <= request['max_characters'] <= 100000):
            return 64
        pages = _pages(tuple(request['pages']), required=request['mode']=='extract')
        if request['mode']=='inspect' and (pages or request['expected_metadata'] is not None): return 64
        if request['mode']=='extract': _checked_metadata(request['expected_metadata'])
        with (folder/'source.pdf').open('rb') as stream:
            raw = stream.read(_MAX_SOURCE+1)
        if not 0 < len(raw) <= _MAX_SOURCE or _hash(raw) != source_sha:
            _fail('pdf_worker_source_changed')
        if not re.match(rb'%PDF-(?:1\.[0-7]|2\.0)(?:\r|\n|\s)',raw[:16]):
            _fail('pdf_file_magic_mismatch')
        value = _parse_in_worker(raw,request['mode'],pages,request['max_characters'],request['expected_metadata'])
        if _WORKER_FAILURE is not None:
            _fail(_WORKER_FAILURE)
        response = {'nonce':nonce,'source_sha256':source_sha,'converter':PDF_VERSION,
                    'ok':True,'code':None,**value}
    except PdfExtractionError as error:
        response = {'nonce':nonce,'source_sha256':source_sha,'converter':PDF_VERSION,
                    'ok':False,'code':error.code,'metadata':None,'pages':[],'characters':0}
    except BaseException as error:
        # Diagnostic class only; exception strings/tracebacks can contain body,
        # filenames, object contents or attacker-controlled dictionary values.
        code = {'MemoryError':'pdf_worker_memory_limit', 'RecursionError':'pdf_parser_recursion_limit',
                'LimitReachedError':'pdf_pypdf_resource_limit', 'PdfReadError':'pdf_readonly_structure_invalid',
                'PdfStreamError':'pdf_readonly_stream_invalid', 'ModuleNotFoundError':'pdf_reviewed_dependency_unavailable',
                'MediaDependencyUnavailable':'pdf_reviewed_dependency_unavailable'}.get(
                    type(error).__name__, 'pdf_readonly_parser_failed_no_retry')
        response = {'nonce':nonce,'source_sha256':source_sha,'converter':PDF_VERSION,
                    'ok':False,'code':code,'metadata':None,'pages':[],'characters':0}
    try:
        encoded = _json(response)
        if len(encoded)>PDF_LIMITS['max_pdf_output_bytes']:
            encoded = _json({'nonce':nonce,'source_sha256':source_sha,'converter':PDF_VERSION,
                'ok':False,'code':'pdf_output_limit_no_truncation','metadata':None,'pages':[],'characters':0})
        # Exclusive creation; no alternate destination or arbitrary file output.
        with (folder/'result.json').open('xb') as stream:
            stream.write(encoded)
        return 0
    except BaseException:
        return 74
