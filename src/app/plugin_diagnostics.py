"""Bounded, explicit-root, metadata-only plugin call diagnostics.

No import/constructor I/O, APPDATA discovery, worker launch, backup, or retention deletion.
append() NEVER persists stderr bytes, a traceback, a body hash, or a guessed redaction.
Only trusted-host enum codes and numeric observations are readable diagnostics. This
module is not wired to the runtime and does not claim same-user hostile filesystem
isolation; production callers must still enforce their owned-root/handle boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading

FORMAT = 'lingxi.plugin-diagnostics.v1'
DEFAULT_CALL_BYTES = 256 * 1024
DEFAULT_FILES = 100
DEFAULT_TOTAL_BYTES = 16 * 1024**2
MAX_CHUNK_BYTES = 1024 * 1024
MAX_READ_FILES = 100
MAX_EVENTS = 256
_CODES = frozenset({'ok', 'starting', 'started', 'completed', 'cancelled', 'timeout',
    'memory_limit', 'cpu_limit', 'process_limit', 'input_limit', 'output_limit',
    'worker_exit', 'plugin_exception', 'protocol_error', 'sandbox_unavailable',
    'binding_changed', 'permission_denied', 'unknown_outcome', 'host_error'})
_STATUSES = frozenset({'completed', 'failed', 'cancelled', 'unknown'})
_RESOURCE_KEYS = frozenset({'peak_memory_bytes', 'cpu_time_ms', 'process_count',
    'exit_code', 'wall_limit_ms', 'memory_limit_bytes', 'cpu_rate', 'active_process_limit'})
_PLUGIN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_CALL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_NAME = re.compile(r'^lxdiag-[0-9a-f]{64}\.jsonl$')
_LOCK = threading.RLock()


class DiagnosticsError(ValueError):
    """Fixed messages only: no worker exception/string content is accepted."""


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise DiagnosticsError('invalid bounded integer')
    return value


def _limits(maxcallbytes, maxfiles, totalbudget):
    _int(maxcallbytes, 1024, 1024 * 1024)
    _int(maxfiles, 1, 1000)
    _int(totalbudget, 1024, 256 * 1024**2)
    if totalbudget < maxcallbytes:
        raise DiagnosticsError('total budget below per-call budget')


def _root_path(root):
    raw = os.fspath(root)
    if not Path(raw).is_absolute() or raw.startswith(('\\\\', '//')) or '..' in Path(raw).parts:
        raise DiagnosticsError('explicit absolute local owned root required')
    path = Path(os.path.abspath(raw))
    if any(ord(c) < 32 for c in raw):
        raise DiagnosticsError('invalid root')
    return path


def _checked(path):
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise DiagnosticsError('linked root or log rejected')
    return path


def _ordinary(path):
    observed = _checked(path).lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise DiagnosticsError('special or hardlinked file rejected')
    return observed


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _encode(record):
    return (json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')


def _filename(plugin_id, call_id, digest):
    return 'lxdiag-' + hashlib.sha256((plugin_id + '\0' + call_id + '\0' + digest).encode('ascii')).hexdigest() + '.jsonl'


def _identifiers(plugin_id, call_id, digest):
    if not isinstance(plugin_id, str) or len(plugin_id) > 200 or not _PLUGIN.fullmatch(plugin_id):
        raise DiagnosticsError('invalid plugin id')
    if not isinstance(call_id, str) or not _CALL.fullmatch(call_id):
        raise DiagnosticsError('invalid call id')
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise DiagnosticsError('invalid digest')


@contextmanager
def _lease(root):
    # Lock metadata is not a call record; no records are replaced or removed.
    _checked(root)
    if not root.is_dir():
        raise DiagnosticsError('owned root must already exist')
    lock = root / '.diagnostics.lock'
    if lock.exists():
        _ordinary(lock)
    with lock.open('a+b') as stream:
        if _identity(os.fstat(stream.fileno()))[:2] != _identity(_ordinary(lock))[:2]:
            raise DiagnosticsError('lock identity changed')
        if stream.seek(0, 2) == 0:
            stream.write(b'0'); stream.flush(); os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DiagnosticsError('diagnostics root busy') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _inventory(root, maxfiles, totalbudget):
    """Unknown files count toward capacity, including nested files, never get deleted."""
    files = total = visited = 0
    for folder, dirs, names in os.walk(_checked(root), followlinks=False):
        for name in dirs:
            _checked(Path(folder) / name)
            visited += 1
            if visited > maxfiles * 4:
                raise DiagnosticsError('diagnostics directory capacity reached')
        for name in names:
            path = Path(folder) / name
            info = _ordinary(path)
            if path == root / '.diagnostics.lock':
                continue
            files += 1; total += info.st_size
            if files > maxfiles or total > totalbudget:
                raise DiagnosticsError('diagnostics capacity reached; explicit cleanup required')
    return files, total


def _read(path, maximum):
    before = _ordinary(path)
    if before.st_size > maximum:
        raise DiagnosticsError('log exceeds read budget')
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise DiagnosticsError('log changed while opening')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > maximum or _identity(after) != _identity(before) or _identity(_ordinary(path)) != _identity(before):
        raise DiagnosticsError('log changed while reading')
    return raw, _identity(before)


def _records(raw, name):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise DiagnosticsError('duplicate metadata field')
            value[key] = item
        return value
    if not raw or not raw.endswith(b'\n'):
        raise DiagnosticsError('incomplete metadata record')
    lines = raw.splitlines()
    if len(lines) > MAX_EVENTS:
        raise DiagnosticsError('too many metadata records')
    result = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(DiagnosticsError('nonfinite metadata')))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise DiagnosticsError('invalid metadata JSON') from exc
        if type(record) is not dict or record.get('schema') != FORMAT or type(record.get('sequence')) is not int or record.get('sequence') != index:
            raise DiagnosticsError('invalid metadata schema or sequence')
        common = {'schema', 'sequence', 'event'}
        event = record.get('event')
        if index == 0:
            if set(record) != common | {'plugin_id', 'call_id', 'digest', 'metadata_only'} or event != 'start' or record['metadata_only'] is not True:
                raise DiagnosticsError('invalid start metadata')
            _identifiers(record['plugin_id'], record['call_id'], record['digest'])
            if _filename(record['plugin_id'], record['call_id'], record['digest']) != name:
                raise DiagnosticsError('log identity mismatch')
        elif event == 'host_event':
            if set(record) != common | {'code', 'resources'} or type(record['code']) is not str or record['code'] not in _CODES:
                raise DiagnosticsError('invalid host metadata')
            _resources(record['resources'])
        elif event == 'finish' and index == len(lines) - 1:
            if set(record) != common | {'status', 'code', 'duration_ms', 'resources', 'stderr_bytes', 'stderr_lines', 'stderr_observed_bytes', 'stderr_truncated', 'stderr_final_line_incomplete'}:
                raise DiagnosticsError('invalid finish metadata')
            if type(record['status']) is not str or type(record['code']) is not str or record['status'] not in _STATUSES or record['code'] not in _CODES:
                raise DiagnosticsError('invalid result enum')
            _duration(record['duration_ms']); _resources(record['resources'])
            for key in ('stderr_bytes', 'stderr_lines', 'stderr_observed_bytes'):
                _int(record[key], 0, 2**63-1)
            if (record['stderr_observed_bytes'] > record['stderr_bytes'] or type(record['stderr_truncated']) is not bool
                    or type(record['stderr_final_line_incomplete']) is not bool):
                raise DiagnosticsError('invalid stderr counters')
        else:
            raise DiagnosticsError('invalid metadata event')
        result.append(record)
    return result


def _duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400000:
        raise DiagnosticsError('invalid duration')


def _resources(value):
    if type(value) is not dict or set(value) - _RESOURCE_KEYS:
        raise DiagnosticsError('unapproved resource metadata')
    for key, number in value.items():
        _int(number, -2147483648 if key == 'exit_code' else 0, 2**63-1)
    return dict(value)


class DiagnosticsLog:
    def __init__(self, root, pluginid, callid, digest, *, maxcallbytes=DEFAULT_CALL_BYTES,
                 maxfiles=DEFAULT_FILES, totalbudget=DEFAULT_TOTAL_BYTES):
        self.root = _root_path(root)  # Lexical only; no filesystem I/O.
        _identifiers(pluginid, callid, digest); _limits(maxcallbytes, maxfiles, totalbudget)
        self.plugin_id, self.call_id, self.digest = pluginid, callid, digest
        self.name = _filename(pluginid, callid, digest)
        self.path = self.root / self.name
        self.maxcallbytes, self.maxfiles, self.totalbudget = maxcallbytes, maxfiles, totalbudget
        self._started = self._finished = False
        self._expected = None
        self._sequence = 0
        self._stderr_bytes = self._stderr_lines = self._observed_bytes = 0
        self._last_observed_newline = True

    def start(self):
        record = {'schema':FORMAT, 'sequence':0, 'event':'start', 'plugin_id':self.plugin_id,
                  'call_id':self.call_id, 'digest':self.digest, 'metadata_only':True}
        with _LOCK, _lease(self.root):
            if self._started:
                raise DiagnosticsError('call already started')
            count, total = _inventory(self.root, self.maxfiles, self.totalbudget)
            raw = _encode(record)
            if count >= self.maxfiles or total + len(raw) > self.totalbudget:
                raise DiagnosticsError('diagnostics capacity reached; explicit cleanup required')
            if len(raw) > self.maxcallbytes:
                raise DiagnosticsError('call metadata capacity reached')
            with self.path.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                self._expected = (hashlib.sha256(raw).digest(), _identity(os.fstat(stream.fileno())))
            _ordinary(self.path)
            self._started = True; self._sequence = 1
        return {'file':self.name, 'metadata_only':True, 'stderr_body_saved':False}

    def append(self, chunk):
        """Consume stderr observation in memory only; never persist bytes or body hash."""
        if type(chunk) is not bytes or len(chunk) > MAX_CHUNK_BYTES:
            raise DiagnosticsError('bounded bytes observation required')
        with _LOCK:
            if not self._started or self._finished:
                raise DiagnosticsError('call not active')
            self._stderr_bytes += len(chunk)
            _int(self._stderr_bytes, 0, 2**63-1)
            available = max(0, self.maxcallbytes - self._observed_bytes)
            accepted = chunk[:available]
            self._observed_bytes += len(accepted)
            self._stderr_lines += accepted.count(b'\n')
            if accepted:
                self._last_observed_newline = accepted.endswith(b'\n')
        return {'observed_bytes':self._observed_bytes, 'body_saved':False,
                'truncated':self._stderr_bytes > self._observed_bytes}

    def _persist(self, value):
        with _LOCK, _lease(self.root):
            if not self._started or self._finished:
                raise DiagnosticsError('call not active')
            if self._sequence >= MAX_EVENTS:
                raise DiagnosticsError('call metadata event capacity reached')
            previous, identity = _read(self.path, self.maxcallbytes)
            if self._expected != (hashlib.sha256(previous).digest(), identity):
                raise DiagnosticsError('call metadata externally changed')
            record = {'schema':FORMAT, 'sequence':self._sequence, **value}
            raw = _encode(record)
            if len(previous) + len(raw) > self.maxcallbytes:
                raise DiagnosticsError('call metadata capacity reached')
            _, total = _inventory(self.root, self.maxfiles, self.totalbudget)
            if total + len(raw) > self.totalbudget:
                raise DiagnosticsError('diagnostics capacity reached; explicit cleanup required')
            with self.path.open('r+b') as stream:
                if _identity(os.fstat(stream.fileno())) != identity:
                    raise DiagnosticsError('log changed before append')
                stream.seek(0, 2); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                self._expected = (hashlib.sha256(previous + raw).digest(), _identity(os.fstat(stream.fileno())))
            self._sequence += 1
            return record

    def host_event(self, code, *, resources=None):
        if type(code) is not str or code not in _CODES:
            raise DiagnosticsError('unapproved host code')
        with _LOCK:
            # Reserve the final event slot so a bounded stream can still close.
            if self._sequence >= MAX_EVENTS - 1:
                raise DiagnosticsError('host event capacity reached; finish still allowed')
            return self._persist({'event':'host_event', 'code':code, 'resources':_resources({} if resources is None else resources)})

    def finish(self, status, code, duration, *, resources=None):
        if type(status) is not str or status not in _STATUSES or type(code) is not str or code not in _CODES:
            raise DiagnosticsError('unapproved result enum')
        _duration(duration)
        # Do not accept arbitrary worker exit text, tracebacks, paths or exception messages.
        with _LOCK:
            record = self._persist({'event':'finish', 'status':status, 'code':code,
                'duration_ms':duration, 'resources':_resources({} if resources is None else resources),
                'stderr_bytes':self._stderr_bytes, 'stderr_lines':self._stderr_lines,
                'stderr_observed_bytes':self._observed_bytes,
                'stderr_truncated':self._stderr_bytes > self._observed_bytes,
                'stderr_final_line_incomplete':self._observed_bytes > 0 and not self._last_observed_newline})
            self._finished = True
            return record

    def records(self):
        with _LOCK, _lease(self.root):
            raw, _ = _read(self.path, self.maxcallbytes)
            return _records(raw, self.name)


def list_records(root, *, limit=20, maxcallbytes=DEFAULT_CALL_BYTES,
                 maxfiles=DEFAULT_FILES, totalbudget=DEFAULT_TOTAL_BYTES):
    """Read bounded validated own metadata only; unknown files are never parsed/returned."""
    path = _root_path(root); _limits(maxcallbytes, maxfiles, totalbudget)
    _int(limit, 1, MAX_READ_FILES)
    with _LOCK, _lease(path):
        _inventory(path, maxfiles, totalbudget)
        names = sorted(item.name for item in path.iterdir() if _NAME.fullmatch(item.name))
        records = []
        for name in names[:limit]:
            raw, _ = _read(path / name, maxcallbytes)
            parsed = _records(raw, name)
            records.append({'file':name, 'records':parsed,
                            'complete':parsed[-1]['event'] == 'finish'})
        return {'items':records, 'returned':len(records), 'total':len(names),
                'metadata_only':True, 'stderr_body_saved':False,
                'cleanup_automatic':False}
