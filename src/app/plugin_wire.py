"""Stdlib-only bounded JSON/BE4 wire codec shared by worker and host.

No host authority, package runtime, AppContainer APIs, configuration or credentials
are imported here. The codec validates data; it is not transport or OS attestation.
"""
from __future__ import annotations

import json
import math
import re
import struct
from types import MappingProxyType
from typing import Any, BinaryIO

MAX_FRAME = 65536
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_STRING_BYTES = 16384
_SECRET = re.compile(r'(?i)(api[_-]?key|password|secret|access[_-]?token|credential|authorization|cookie|private[_-]?key)')
_LIKELY_KEY = re.compile(r'(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\b(?:bearer|api[_-]?key|access[_-]?token)\s*[:= ]\s*\S{8,})')
PERMISSIONS = MappingProxyType({'data.read':'plugin.data.read', 'data.write':'plugin.data.write',
    'file.read':'file.read', 'file.write':'file.write', 'http.request':'network.http',
    'model.generate':'model.generate'})
OPERATIONS = frozenset((*PERMISSIONS, 'config.read', 'state.read', 'cancel'))


class ProxyProtocolError(ValueError):
    """Fixed bounded error code, never request or adapter exception contents."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str):
    raise ProxyProtocolError(code)


def _json_shape(value: Any, *, safe=False):
    stack = [(value, 1)]
    count = 0
    while stack:
        current, depth = stack.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            _fail('json_complexity')
        if type(current) is dict:
            if len(current) > MAX_NODES // 2:
                _fail('json_complexity')
            for key, child in current.items():
                if type(key) is not str or len(key) > 128:
                    _fail('json_key')
                if safe and _SECRET.search(key):
                    _fail('secret_material')
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif type(current) is list:
            if len(current) > MAX_NODES:
                _fail('json_complexity')
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            try:
                size = len(current.encode('utf-8'))
            except UnicodeError:
                _fail('json_unicode')
            if size > MAX_STRING_BYTES or '\x00' in current:
                _fail('json_string')
            if safe and _LIKELY_KEY.search(current):
                _fail('secret_material')
        elif type(current) in (int, float):
            if type(current) is int and current.bit_length() > 64:
                _fail('json_number')
            if type(current) is float and not math.isfinite(current):
                _fail('json_number')
        elif current is not None and type(current) is not bool:
            _fail('json_type')


def encode_json(value: Any, *, safe=False) -> bytes:
    _json_shape(value, safe=safe)
    try:
        data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail('json_invalid')
    if not data or len(data) > MAX_FRAME:
        _fail('frame_size')
    return data


def decode_json(data: bytes) -> Any:
    if type(data) is not bytes or not 0 < len(data) <= MAX_FRAME:
        _fail('frame_size')
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                _fail('json_duplicate_key')
            obj[key] = value
        return obj
    def constant(_):
        _fail('json_number')
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except ProxyProtocolError:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError):
        _fail('json_invalid')
    _json_shape(result)
    return result


def encode_frame(value: Any) -> bytes:
    data = encode_json(value)
    return struct.pack('!I', len(data)) + data


class FrameDecoder:
    """Incremental bounded codec, not transport; errors permanently close it."""
    def __init__(self):
        self._buffer = bytearray()
        self._length = None
        self._closed = False

    def feed(self, chunk: bytes) -> list[Any]:
        if self._closed:
            _fail('frame_closed')
        if type(chunk) is not bytes or len(chunk) > MAX_FRAME + 4:
            self._closed = True
            _fail('frame_chunk_size')
        result = []
        try:
            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                needed = (4 if self._length is None else self._length) - len(self._buffer)
                take = min(needed, len(view) - offset)
                self._buffer.extend(view[offset:offset + take]); offset += take
                if len(self._buffer) < (4 if self._length is None else self._length):
                    continue
                if self._length is None:
                    self._length = struct.unpack('!I', self._buffer)[0]
                    self._buffer.clear()
                    if not 0 < self._length <= MAX_FRAME:
                        _fail('frame_size')
                else:
                    result.append(decode_json(bytes(self._buffer)))
                    if len(result) > MAX_NODES:
                        _fail('frame_batch_size')
                    self._buffer.clear(); self._length = None
            return result
        except ProxyProtocolError:
            self._closed = True; self._buffer.clear()
            raise

    def eof(self):
        self._closed = True
        if self._buffer or self._length is not None:
            self._buffer.clear()
            _fail('frame_truncated')


def read_frame(stream: BinaryIO) -> Any | None:
    """Clean EOF is accepted only before the first header byte."""
    def exact(size, *, initial=False):
        chunks = []
        remaining = size
        while remaining:
            part = stream.read(remaining)
            if type(part) is not bytes or len(part) > remaining:
                _fail('frame_stream')
            if not part:
                if initial and remaining == size:
                    return None
                _fail('frame_truncated')
            chunks.append(part); remaining -= len(part)
        return b''.join(chunks)
    header = exact(4, initial=True)
    if header is None:
        return None
    size = struct.unpack('!I', header)[0]
    if not 0 < size <= MAX_FRAME:
        _fail('frame_size')
    return decode_json(exact(size))


def write_frame(stream: BinaryIO, value: Any):
    data = encode_frame(value)
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if type(written) is not int or not 0 < written <= len(data) - offset:
            _fail('frame_stream')
        offset += written
    stream.flush()
