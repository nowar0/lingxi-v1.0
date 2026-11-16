"""Read-only loopback-exemption provider; import/construction performs no native I/O.

An explicit query lazily binds Firewallapi.dll's two-argument SDK API. Its returned
SIDs AND array belong to the process heap (the SDK FreeAppContainerConfig example):
HeapFree each distinct SID, then HeapFree the array. Only SID conversion strings
use LocalFree. No profile, ACL, exemption mutation, subprocess or fallback exists.

False means a complete, validated enumeration and successful cleanup, NOT a full
network-isolation attestation. Unknown/error poisons that provider for its lifetime.
Tests inject APIs/DLLs; this module alone is not OS acceptance evidence.
"""
from __future__ import annotations

import ctypes as C
import os
import struct
import threading

from app.plugin_windows_observer import ObserverError

DWORD = C.c_uint32
BOOL = C.c_int32
PSID = C.c_void_p
SIZE_T = C.c_size_t
MAX_ENTRIES = 4096
MAX_ARRAY_BYTES = 1024 * 1024
MAX_SID_BYTES = 68
MAX_SID_CHARS = 256
_POINTER_MAX = (1 << (8 * C.sizeof(PSID))) - 1


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [('Sid', PSID), ('Attributes', DWORD)]


PSID_AND_ATTRIBUTES = C.POINTER(SID_AND_ATTRIBUTES)


class LoopbackQueryError(ObserverError):
    """Fixed safe error code; observation_cleanup retains any incomplete owner."""


def _canonical_sid(value, *, appcontainer=False):
    if type(value) is not str or not 5 <= len(value) <= MAX_SID_CHARS:
        raise LoopbackQueryError('loopback_invalid_sid')
    parts = value.split('-')
    if (not 3 <= len(parts) <= 18 or parts[:2] != ['S', '1']
            or (appcontainer and (len(parts) < 5 or parts[:4] != ['S', '1', '15', '2']
                                   or value in ('S-1-15-2-1', 'S-1-15-2-2')))):
        raise LoopbackQueryError('loopback_invalid_sid')
    for index, number in enumerate(parts[2:]):
        upper = (1 << 48) - 1 if index == 0 else 0xffffffff
        if (not number.isascii() or not number.isdecimal() or len(number) > 15
                or str(int(number)) != number or int(number) > upper):
            raise LoopbackQueryError('loopback_invalid_sid')
    return value


def _sid_buffer(sid):
    parts = sid.split('-')
    subauthorities = [int(number) for number in parts[3:]]
    raw = bytes((1, len(subauthorities))) + int(parts[2]).to_bytes(6, 'big')
    raw += b''.join(struct.pack('<I', number) for number in subauthorities)
    return C.create_string_buffer(raw, len(raw))


def _pointer(pointer):
    return C.cast(pointer, PSID).value or 0


def _range_valid(address, size, alignment=1):
    return (type(address) is int and 0 < address <= _POINTER_MAX
            and address % alignment == 0 and 0 < size <= _POINTER_MAX - address + 1)


def _native_bool(value):
    if type(value) not in (bool, int):
        raise LoopbackQueryError('loopback_native_result_unknown')
    return bool(value)


class _AllocationOwner:
    """Retryable owner. Never drop a failed free or an unwalkable returned array.

    HeapSize checks the SDK process-heap allocation before Python dereferences it.
    If count/pointer/allocation size is inconsistent, walking/freeing unknown SID
    pointers would be unsafe: retain the array and its metadata for host review.
    """
    def __init__(self, api, heap):
        self.api = api
        self.heap = heap
        self.array = PSID_AND_ATTRIBUTES()
        self.count = DWORD()
        self._allocations = {}
        self._unwalked = False
        self._blocked = False

    @property
    def closed(self):
        return not self._allocations and not self._unwalked and not self._blocked

    def _adopt(self, address, kind):
        if not address:
            return
        before = self._allocations.get(address)
        if before is not None and before != kind:
            self._blocked = True
            raise LoopbackQueryError('loopback_allocation_alias')
        self._allocations[address] = kind

    def capture_array(self):
        address = _pointer(self.array)
        if address:
            self._adopt(address, 'array')
            self._unwalked = self.count.value != 0

    def discover(self):
        count = self.count.value
        address = _pointer(self.array)
        size = count * C.sizeof(SID_AND_ATTRIBUTES)
        if count > MAX_ENTRIES or size > MAX_ARRAY_BYTES:
            raise LoopbackQueryError('loopback_count_out_of_bounds')
        if bool(count) != bool(address):
            raise LoopbackQueryError('loopback_count_pointer_mismatch')
        if not count:
            return ()
        if (not _range_valid(address, size, C.alignment(SID_AND_ATTRIBUTES))
                or self.api.heap_size(self.heap, address) < size):
            raise LoopbackQueryError('loopback_array_allocation_invalid')
        # Register ALL children before validating individual SIDs. A late malformed
        # SID or early match must not leak the remainder or short-circuit validation.
        pointers = []
        malformed = False
        for index in range(count):
            pointer = self.array[index].Sid or 0
            if not _range_valid(pointer, 8, 4):
                malformed = True
                if pointer:
                    self._adopt(pointer, 'sid')
                    self._blocked = True  # Never free an unsafe/non-aligned address.
                continue
            if address <= pointer < address + size:
                malformed = True  # An interior alias is not a second allocation.
                continue
            if pointer in pointers:
                malformed = True
            self._adopt(pointer, 'sid')
            pointers.append(pointer)
        self._unwalked = False
        if malformed:
            raise LoopbackQueryError('loopback_sid_pointer_invalid_or_duplicate')
        return tuple(pointers)

    def close(self):
        # Retry discovery only, never requery or accept evidence after unknown.
        if self._unwalked:
            self.discover()
        if self._blocked:
            raise LoopbackQueryError('loopback_allocation_alias')
        failed = False
        # Insertion order is irrelevant: all SID allocations precede the array.
        for address, kind in sorted(tuple(self._allocations.items()),
                                    key=lambda item: item[1] == 'array'):
            if kind == 'array' and any(value == 'sid' for value in self._allocations.values()):
                failed = True
                continue
            try:
                if kind == 'local':
                    self.api.local_size(address)
                    result = self.api.kernel.LocalFree(PSID(address))
                    success = result is None or type(result) is int and result == 0
                else:
                    # Never feed an invalid/non-heap pointer to HeapFree, including
                    # cleanup of an error response or malformed SID entry.
                    alignment = C.alignment(SID_AND_ATTRIBUTES) if kind == 'array' else 4
                    if not _range_valid(address, 1, alignment):
                        raise LoopbackQueryError('loopback_allocation_pointer_invalid')
                    self.api.heap_size(self.heap, address)
                    success = _native_bool(self.api.kernel.HeapFree(self.heap, 0, PSID(address)))
                if not success:
                    failed = True
                    continue
            except BaseException:
                # In particular KeyboardInterrupt must not drop the remaining owner.
                failed = True
                continue
            del self._allocations[address]
        if failed or not self.closed:
            raise LoopbackQueryError('loopback_cleanup_failed')


class NativeLoopbackApi:
    """Lazy SDK wrapper. loader is a fake DLL loader seam, never an automatic fallback."""
    def __init__(self, *, loader=None):
        self._loader = loader
        self._loaded = False
        self._failed = False
        self.pending_cleanup = None
        self._lock = threading.RLock()

    def _bind(self):
        if self._loaded:
            return
        loader = self._loader
        if loader is None:
            if os.name != 'nt':
                raise LoopbackQueryError('loopback_api_unavailable')
            loader = C.WinDLL
        try:
            self.network = loader('Firewallapi.dll', use_last_error=True)
            self.advapi = loader('advapi32.dll', use_last_error=True)
            self.kernel = loader('kernel32.dll', use_last_error=True)
            def bind(dll, name, arguments, result):
                function = getattr(dll, name)
                function.argtypes, function.restype = arguments, result
            bind(self.network, 'NetworkIsolationGetAppContainerConfig',
                 [C.POINTER(DWORD), C.POINTER(PSID_AND_ATTRIBUTES)], DWORD)
            bind(self.advapi, 'IsValidSid', [PSID], BOOL)
            bind(self.advapi, 'GetLengthSid', [PSID], DWORD)
            bind(self.advapi, 'EqualSid', [PSID, PSID], BOOL)
            bind(self.advapi, 'ConvertSidToStringSidW', [PSID, C.POINTER(PSID)], BOOL)
            bind(self.kernel, 'GetProcessHeap', [], PSID)
            bind(self.kernel, 'HeapSize', [PSID, DWORD, PSID], SIZE_T)
            bind(self.kernel, 'HeapFree', [PSID, DWORD, PSID], BOOL)
            bind(self.kernel, 'LocalSize', [PSID], SIZE_T)
            bind(self.kernel, 'LocalFree', [PSID], PSID)
            self._loaded = True
        except Exception as error:
            raise LoopbackQueryError('loopback_api_unavailable') from error

    def heap_size(self, heap, address):
        size = self.kernel.HeapSize(heap, 0, PSID(address))
        if type(size) is not int or size == _POINTER_MAX or size <= 0:
            raise LoopbackQueryError('loopback_heap_allocation_invalid')
        return size

    def local_size(self, address):
        if not _range_valid(address, C.sizeof(C.c_wchar), C.alignment(C.c_wchar)):
            raise LoopbackQueryError('loopback_string_allocation_invalid')
        size = self.kernel.LocalSize(PSID(address))
        if type(size) is not int or size <= 0 or size == _POINTER_MAX:
            raise LoopbackQueryError('loopback_string_allocation_invalid')
        return size

    def _sid_text(self, pointer, owner):
        text = PSID()
        try:
            success = self.advapi.ConvertSidToStringSidW(PSID(pointer), C.byref(text))
        finally:
            # A fake/failed conversion may set its out-pointer before raising.
            owner._adopt(text.value, 'local')
        if not _native_bool(success) or not text.value:
            raise LoopbackQueryError('loopback_sid_conversion_failed')
        # SID strings are bounded by the binary SID format; do not perform an
        # unbounded wchar_p.value read, even for an unexpected conversion result.
        capacity = self.local_size(text.value) // C.sizeof(C.c_wchar)
        characters = C.cast(text, C.POINTER(C.c_wchar))
        value = ''
        for index in range(min(capacity, MAX_SID_CHARS + 1)):
            char = characters[index]
            if char == '\x00':
                return _canonical_sid(value)
            value += char
        raise LoopbackQueryError('loopback_sid_conversion_size')

    def has_loopback_exemption(self, sid):
        _canonical_sid(sid, appcontainer=True)
        with self._lock:
            if self._failed:
                raise LoopbackQueryError('loopback_provider_failed')
            owner = None
            cleanup_attempted = False
            try:
                self._bind()
                heap = self.kernel.GetProcessHeap()
                if not heap:
                    raise LoopbackQueryError('loopback_process_heap_unavailable')
                owner = _AllocationOwner(self, heap)
                try:
                    status = self.network.NetworkIsolationGetAppContainerConfig(
                        C.byref(owner.count), C.byref(owner.array))
                finally:
                    owner.capture_array()
                # Discover even on a returned error to retain/release any partial
                # SDK output. No error result can ever establish exemption absence.
                pointers = owner.discover()
                if type(status) is not int or status != 0:
                    raise LoopbackQueryError('loopback_query_failed')
                target = _sid_buffer(sid)  # Python-owned, NOT an SDK allocation.
                if not _native_bool(self.advapi.IsValidSid(target)):
                    raise LoopbackQueryError('loopback_invalid_sid')
                matched = False
                texts = set()
                for pointer in pointers:
                    allocated = self.heap_size(heap, pointer)
                    if allocated < 8:
                        raise LoopbackQueryError('loopback_sid_size_invalid')
                    header = C.string_at(pointer, 2)
                    expected_size = 8 + 4 * header[1]
                    if (header[0] != 1 or header[1] > 15 or expected_size > allocated
                            or not _native_bool(self.advapi.IsValidSid(PSID(pointer)))):
                        raise LoopbackQueryError('loopback_invalid_sid')
                    size = self.advapi.GetLengthSid(PSID(pointer))
                    if (type(size) is not int or size != expected_size
                            or not 8 <= size <= MAX_SID_BYTES):
                        raise LoopbackQueryError('loopback_sid_size_invalid')
                    text = self._sid_text(pointer, owner)
                    if text in texts:
                        raise LoopbackQueryError('loopback_duplicate_sid')
                    texts.add(text)
                    equal = _native_bool(self.advapi.EqualSid(PSID(pointer), target))
                    if equal != (text == sid):
                        raise LoopbackQueryError('loopback_sid_comparison_mismatch')
                    matched = matched or equal
                cleanup_attempted = True
                owner.close()
                return matched
            except BaseException as original:
                self._failed = True
                if owner is not None and not owner.closed:
                    if not cleanup_attempted:
                        try:
                            owner.close()
                        except BaseException:
                            pass
                    if not owner.closed:
                        self.pending_cleanup = owner
                        original.observation_cleanup = owner
                        original.add_note('Loopback query cleanup incomplete; explicit host retry required')
                if isinstance(original, (ObserverError, KeyboardInterrupt, SystemExit)):
                    raise
                error = LoopbackQueryError('loopback_query_failed')
                if self.pending_cleanup is not None:
                    error.observation_cleanup = self.pending_cleanup
                raise error from original


class LoopbackExemptionProvider:
    """Public host seam: exact SID -> bool, or an explicit fail-closed exception.

    Keep this instance on the observer so cleanup remains reachable even if its
    caller converts an exception into NetworkEvidence. Never cache a previous bool.
    """
    def __init__(self, *, native_api=None):
        self._api = native_api
        self._failed = False
        self.pending_cleanup = None
        self._lock = threading.RLock()

    def has_loopback_exemption(self, sid) -> bool:
        _canonical_sid(sid, appcontainer=True)
        with self._lock:
            if self._failed:
                raise LoopbackQueryError('loopback_provider_failed')
            try:
                if self._api is None:
                    self._api = NativeLoopbackApi()
                result = self._api.has_loopback_exemption(sid)
                if type(result) is not bool:
                    raise LoopbackQueryError('loopback_observation_unknown')
                return result
            except BaseException as original:
                self._failed = True
                self.pending_cleanup = getattr(original, 'observation_cleanup',
                    getattr(self._api, 'pending_cleanup', None))
                if isinstance(original, (ObserverError, KeyboardInterrupt, SystemExit)):
                    raise
                error = LoopbackQueryError('loopback_query_failed')
                if self.pending_cleanup is not None:
                    error.observation_cleanup = self.pending_cleanup
                raise error from original
