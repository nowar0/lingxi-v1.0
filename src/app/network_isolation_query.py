"""One-shot, read-only loopback-exemption enumeration helper.

NetworkIsolationGetAppContainerConfig has TWO arguments: DWORD* count and
SID_AND_ATTRIBUTES** entries (networkisolation.h). The ownership/free function
for that returned entries buffer is deliberately NOT guessed here. This module
must run only in a bounded short-lived helper process: one enumeration attempt
per process, then process exit reclaims its heap. Never embed it in the long-lived
host. Only ConvertSidToStringSidW's documented string allocation uses LocalFree.

Import/construction performs no native calls. This module creates no profile,
ACL, token, network connection, exemption or child process. Missing API, malformed
results, incomplete enumeration or helper failure means UNKNOWN, not 'no exemption'.
The parent must freeze/wire the fixed helper, bound its lifetime/output and attest
its executable; none of those production integration steps happen in this module.
"""
from __future__ import annotations

import ctypes as C
import json
import os
import re
import sys
import threading
from typing import BinaryIO

MAX_REQUEST_BYTES = 4096
MAX_OUTPUT_BYTES = 128 * 1024
MAX_ENTRIES = 4096
MAX_SID_BYTES = 68
APISET_DLL = 'api-ms-win-net-isolation-l1-1-0.dll'
DWORD = C.c_uint32
BOOL = C.c_int32
PSID = C.c_void_p
LPWSTR = C.c_wchar_p


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [('Sid', PSID), ('Attributes', DWORD)]


PSID_AND_ATTRIBUTES = C.POINTER(SID_AND_ATTRIBUTES)
_PROCESS_QUERY_LOCK = threading.Lock()
_PROCESS_QUERY_CLAIMED = False
_NONCE = re.compile(r'^[A-Za-z0-9_-]{16,128}$')


class QueryError(ValueError):
    """Fixed error codes only: never include native exceptions or other SID data."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_appcontainer_sid(value: object) -> str:
    if type(value) is not str or len(value) > 256:
        raise QueryError('invalid_request')
    parts = value.split('-')
    # Binary SIDs contain at most 15 subauthorities, including AppContainer's 2.
    if not 5 <= len(parts) <= 18 or parts[:4] != ['S', '1', '15', '2']:
        raise QueryError('invalid_request')
    for number in parts[4:]:
        if (not number.isascii() or not number.isdecimal() or len(number) > 10
                or str(int(number)) != number or int(number) > 0xffffffff):
            raise QueryError('invalid_request')
    return value


def _canonical_enumerated_sid(value: object) -> str:
    """Validate canonical general SIDs too; the API can contain non-target SIDs."""
    if type(value) is not str or not 5 <= len(value) <= 256:
        raise QueryError('invalid_sid')
    parts = value.split('-')
    if not 3 <= len(parts) <= 18 or parts[:2] != ['S', '1']:
        raise QueryError('invalid_sid')
    for index, number in enumerate(parts[2:]):
        upper = (1 << 48) - 1 if index == 0 else 0xffffffff
        if (not number.isascii() or not number.isdecimal() or len(number) > 15
                or str(int(number)) != number or int(number) > upper):
            raise QueryError('invalid_sid')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QueryError('invalid_request')
        result[key] = value
    return result


def parse_request(raw: bytes) -> dict:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        raise QueryError('invalid_request')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(QueryError('invalid_request')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise QueryError('invalid_request') from error
    if (type(value) is not dict or set(value) != {'v', 'nonce', 'appcontainer_sid'}
            or type(value['v']) is not int or value['v'] != 1
            or type(value['nonce']) is not str or not _NONCE.fullmatch(value['nonce'])):
        raise QueryError('invalid_request')
    canonical_appcontainer_sid(value['appcontainer_sid'])
    return value


class NativeQueryApi:
    """One native read-only query attempt per instance AND helper process.

    loader is an injectable fake DLL loader for tests; default WinDLL is used only
    after query() and only on Windows. No fallback DLL search or mutation API exists.
    """
    def __init__(self, *, loader=None):
        self._loader = loader
        self._attempted = False
        self._loaded = False

    def _bind(self):
        if self._loaded:
            return
        loader = self._loader
        if loader is None:
            if os.name != 'nt':
                raise QueryError('api_unavailable')
            loader = C.WinDLL
        try:
            self._network = loader(APISET_DLL, use_last_error=True)
            self._advapi = loader('advapi32.dll', use_last_error=True)
            self._kernel = loader('kernel32.dll', use_last_error=True)
            # SDK: DWORD WINAPI Function(DWORD*, SID_AND_ATTRIBUTES**). NO flags.
            self._query = self._network.NetworkIsolationGetAppContainerConfig
            self._query.argtypes = [C.POINTER(DWORD), C.POINTER(PSID_AND_ATTRIBUTES)]
            self._query.restype = DWORD
            self._valid = self._advapi.IsValidSid
            self._valid.argtypes = [PSID]
            self._valid.restype = BOOL
            self._length = self._advapi.GetLengthSid
            self._length.argtypes = [PSID]
            self._length.restype = DWORD
            self._string = self._advapi.ConvertSidToStringSidW
            self._string.argtypes = [PSID, C.POINTER(LPWSTR)]
            self._string.restype = BOOL
            self._free_string = self._kernel.LocalFree
            self._free_string.argtypes = [C.c_void_p]
            self._free_string.restype = C.c_void_p
        except Exception as error:
            raise QueryError('api_unavailable') from error
        self._loaded = True

    def query(self, target_sid: str) -> dict:
        global _PROCESS_QUERY_CLAIMED
        canonical_appcontainer_sid(target_sid)
        with _PROCESS_QUERY_LOCK:
            if self._attempted or _PROCESS_QUERY_CLAIMED:
                raise QueryError('query_already_attempted')
            self._attempted = True
            _PROCESS_QUERY_CLAIMED = True
        self._bind()
        count = DWORD()
        entries = PSID_AND_ATTRIBUTES()
        try:
            status = int(self._query(C.byref(count), C.byref(entries)))
        except Exception as error:
            raise QueryError('native_query_failed') from error
        if not 0 <= status <= 0xffffffff:
            raise QueryError('native_query_failed')
        if status:
            return {'target_match': None, 'api_status': status,
                    'enumeration_complete': False, 'count': None,
                    'error_code': 'native_query_failed'}
        if count.value > MAX_ENTRIES or (count.value and not entries):
            raise QueryError('invalid_enumeration')
        seen = set()
        matched = False
        # Validate EVERY entry even when target appears first; no early success.
        for index in range(count.value):
            pointer = entries[index].Sid
            if not pointer or not self._valid(pointer):
                raise QueryError('invalid_sid')
            size = int(self._length(pointer))
            if not 0 < size <= MAX_SID_BYTES:
                raise QueryError('invalid_sid')
            text = LPWSTR()
            converted = False
            try:
                converted = bool(self._string(pointer, C.byref(text)))
                if not converted or not text:
                    raise QueryError('invalid_sid')
                # Bounded read: do not walk arbitrary unbounded native text.
                characters = []
                raw_text = C.cast(text, C.POINTER(C.c_wchar))
                for offset in range(257):
                    char = raw_text[offset]
                    if char == '\x00':
                        break
                    characters.append(char)
                else:
                    raise QueryError('invalid_sid')
                sid = _canonical_enumerated_sid(''.join(characters))
                if sid in seen:
                    raise QueryError('duplicate_sid')
                seen.add(sid)
                matched = matched or sid == target_sid
            finally:
                # A non-null output may be allocated even on conversion failure.
                # This is ONLY the documented SID string, NOT the entries buffer.
                if text and self._free_string(C.cast(text, C.c_void_p)):
                    raise QueryError('sid_string_release_failed')
        return {'target_match': matched, 'api_status': 0,
                'enumeration_complete': True, 'count': count.value, 'error_code': None}


def _response(nonce=None, *, code='query_failed', status=None):
    return {'v': 1, 'nonce': nonce, 'target_match': None, 'api_status': status,
            'enumeration_complete': False, 'count': None, 'error_code': code}


def _read_request(stream: BinaryIO) -> bytes:
    chunks = []
    total = 0
    while True:
        block = stream.read(MAX_REQUEST_BYTES + 1 - total)
        if type(block) is not bytes:
            raise QueryError('invalid_request')
        if not block:
            return b''.join(chunks)
        total += len(block)
        if total > MAX_REQUEST_BYTES:
            raise QueryError('invalid_request')
        chunks.append(block)


def run(argv, input_stream: BinaryIO, output_stream: BinaryIO, *, api_factory=NativeQueryApi) -> int:
    """Fixed stdin JSON / stdout single JSON object contract. Parent enforces lifetime."""
    nonce = None
    result = _response(code='invalid_arguments')
    exit_code = 2
    try:
        if list(argv) != ['--query']:
            raise QueryError('invalid_arguments')
        request = parse_request(_read_request(input_stream))
        nonce = request['nonce']
        result = {'v': 1, 'nonce': nonce, **api_factory().query(request['appcontainer_sid'])}
        if (set(result) != {'v', 'nonce', 'target_match', 'api_status', 'enumeration_complete', 'count', 'error_code'}
                or result['enumeration_complete'] is not True or type(result['api_status']) is not int or result['api_status'] != 0
                or type(result['target_match']) is not bool or type(result['count']) is not int
                or not 0 <= result['count'] <= MAX_ENTRIES or result['error_code'] is not None):
            status = result.get('api_status')
            result = _response(nonce, code='native_query_failed',
                               status=status if type(status) is int and 0 <= status <= 0xffffffff else None)
        else:
            exit_code = 0
    except QueryError as error:
        result = _response(nonce, code=error.code)
    except Exception:
        # Raw exceptions/native details must never be written to helper stdout/stderr.
        result = _response(nonce)
    encoded = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps(_response(code='output_limit'), separators=(',', ':')).encode('ascii') + b'\n'
        exit_code = 2
    try:
        offset = 0
        while offset < len(encoded):
            written = output_stream.write(encoded[offset:])
            if type(written) is not int or not 0 < written <= len(encoded) - offset:
                return 2
            offset += written
        output_stream.flush()
    except Exception:
        return 2
    return exit_code


def main(argv=None) -> int:
    return run(sys.argv[1:] if argv is None else argv, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == '__main__':
    raise SystemExit(main())
