"""Read-only path checks under a duplicate of an actually observed child token.

No constructor/import native I/O. Never manufactures a lowbox token or grants ACLs.
Only a fresh dedicated thread impersonates; the host/main thread never does. These
observations require an already-created, still-owned suspended child. They cannot
satisfy the current pre-creation ProvisionLedger ancestor gate without an explicit
phase/contract integration. A Python receipt is not itself OS evidence.

A native local-file/filter call can still hang. The caller's finite wait then returns
unknown and retains the non-daemon thread/resource owner; it does NOT claim that the
thread was killed or privileges reverted. Real execution remains separately approved.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
import re
import threading
import time

from app.plugin_sandbox_policy import canonical_local_path

READ_DATA = 0x1
EXECUTE_OR_TRAVERSE = 0x20
READ_ATTRIBUTES = 0x80
READ_CONTROL = 0x20000
SYNCHRONIZE = 0x100000
READ_ONLY_MASK = READ_DATA | EXECUTE_OR_TRAVERSE | READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
MAX_TARGETS = 256
_SID = re.compile(r'^S-1-15-2-(?:[0-9]+-)*[0-9]+$')
_SHA = re.compile(r'^[0-9a-f]{64}$')


class TokenAccessError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class OwnedChildReceipt:
    process_handle: int = field(repr=False)
    pid: int
    creation_time: int
    sandbox_sid: str
    policy_fingerprint: str

    def __post_init__(self):
        if (type(self.process_handle) is not int or self.process_handle <= 0
                or type(self.pid) is not int or self.pid <= 0
                or type(self.creation_time) is not int or self.creation_time <= 0
                or not isinstance(self.sandbox_sid, str) or not _SID.fullmatch(self.sandbox_sid)
                or self.sandbox_sid in ('S-1-15-2-1', 'S-1-15-2-2')
                or not isinstance(self.policy_fingerprint, str) or not _SHA.fullmatch(self.policy_fingerprint)):
            raise TokenAccessError('unverified_child_receipt')


@dataclass(frozen=True)
class ProbeTarget:
    path: str
    access_mask: int
    kind: str
    volume: int
    file_id: int

    def __post_init__(self):
        # Drive roots occur in the full ancestor chain. Permit only their exact
        # canonical directory form for traverse-only observation, not file reads
        # or a generic relaxation of policy/grant path validation.
        drive_root = isinstance(self.path,str) and re.fullmatch(r'[A-Z]:\\',self.path) is not None
        if drive_root:
            if self.kind!='directory' or self.access_mask!=EXECUTE_OR_TRAVERSE:
                raise TokenAccessError('drive_root_traverse_only')
        else:
            canonical_local_path(self.path)
        if (type(self.access_mask) is not int or not self.access_mask
                or self.access_mask & ~READ_ONLY_MASK or self.kind not in ('file', 'directory')
                or type(self.volume) is not int or self.volume < 0
                or type(self.file_id) is not int or self.file_id <= 0
                or self.kind == 'directory' and self.access_mask & READ_DATA):
            raise TokenAccessError('readonly_exact_target_required')


@dataclass(frozen=True)
class TokenObservation:
    is_appcontainer: bool
    sandbox_sid: str | None
    capabilities: tuple
    integrity_sid: str
    restricted: bool
    restricted_sids: tuple
    groups: tuple
    privileges: tuple
    mandatory_policy: int
    authentication_id: tuple[int, int]
    token_type: int
    impersonation_level: int | None
    has_restrictions: bool
    user_sid: str

    def security_binding(self):
        return tuple(getattr(self, name) for name in self.__dataclass_fields__
                     if name not in ('token_type', 'impersonation_level'))


def _valid_token(value, child, *, duplicate=False):
    if (type(value) is not TokenObservation or value.is_appcontainer is not True
            or value.sandbox_sid != child.sandbox_sid or type(value.capabilities) is not tuple
            or value.capabilities or type(value.restricted) is not bool
            or type(value.has_restrictions) is not bool
            or not isinstance(value.user_sid, str) or not re.fullmatch(r'S-1-[0-9]+(?:-[0-9]+)+', value.user_sid)
            or not isinstance(value.integrity_sid, str) or not re.fullmatch(r'S-1-16-[0-9]+', value.integrity_sid)
            or int(value.integrity_sid.rsplit('-', 1)[1]) > 4096
            or type(value.mandatory_policy) is not int or not 0 <= value.mandatory_policy <= 3
            or type(value.authentication_id) is not tuple or len(value.authentication_id) != 2
            or any(type(x) is not int for x in value.authentication_id)
            or any(type(getattr(value, key)) is not tuple for key in ('restricted_sids', 'groups', 'privileges'))
            or value.token_type != (2 if duplicate else 1)
            or duplicate and value.impersonation_level != 2):
        raise TokenAccessError('actual_appcontainer_token_required')
    return value


@dataclass(frozen=True)
class AccessEvidence:
    pid: int
    creation_time: int
    sandbox_sid: str
    targets_fingerprint: str
    read_access_verified: bool = False
    parent_acl_modified: bool = False
    cleanup_verified: bool = False
    reason: str = 'unverified'
    source: str = 'unverified'


class ProbeOwner:
    """Retains uncertain handles/thread; no forcibly terminated Python thread."""
    def __init__(self):
        self.done = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.handles = set()
        self.cleanup_fault = False
        self.reverted = False
        self.reason = 'unverified'
        self.verified = False
        self.api = None

    def retry_handle_cleanup(self):
        if not self.done.is_set() or self.thread.is_alive():
            raise TokenAccessError('probe_thread_still_owned')
        for handle in tuple(self.handles):
            try:
                if self.api.close(handle) is not True:
                    raise TokenAccessError('close_failed')
                self.handles.remove(handle)
            except Exception:
                self.cleanup_fault = True
        return not self.handles


@dataclass(frozen=True)
class ProbeResult:
    evidence: AccessEvidence
    owner: ProbeOwner = field(repr=False, compare=False)


def targets_fingerprint(child, targets):
    if type(child) is not OwnedChildReceipt or type(targets) is not tuple or not 1 <= len(targets) <= MAX_TARGETS:
        raise TokenAccessError('exact_probe_plan_required')
    if any(type(target) is not ProbeTarget for target in targets) or len({x.path.casefold() for x in targets}) != len(targets):
        raise TokenAccessError('unique_exact_probe_targets_required')
    return _digest({'child': asdict(child), 'targets': [asdict(x) for x in targets]})


class TokenPathAccessVerifier:
    def __init__(self, *, api=None):
        self._api = api
        self._used = set()
        self._lock = threading.Lock()

    def verify(self, child, targets, *, user_confirmed, fingerprint, timeout=2.0):
        expected = targets_fingerprint(child, targets)
        if user_confirmed is not True or fingerprint != expected:
            raise TokenAccessError('exact_readonly_probe_approval_required')
        if type(timeout) not in (int, float) or not 0 < timeout <= 2:
            raise TokenAccessError('bounded_probe_wait_required')
        with self._lock:
            if expected in self._used or len(self._used) >= 4096:
                raise TokenAccessError('probe_approval_consumed_or_capacity')
            self._used.add(expected)
        owner = ProbeOwner()
        deadline = time.monotonic() + timeout
        def worker():
            impersonated = False
            api = None
            def check():
                if owner.stop.is_set() or time.monotonic() >= deadline:
                    raise TokenAccessError('probe_deadline_unknown')
            try:
                api = self._api if self._api is not None else NativeTokenAccessApi()
                owner.api = api
                check()
                if api.thread_has_token():
                    raise TokenAccessError('dedicated_thread_not_clean')
                identity = api.process_identity(child.process_handle)
                if identity != (child.pid, child.creation_time):
                    raise TokenAccessError('child_process_identity_changed')
                token = api.open_process_token(child.process_handle)
                if type(token) is not int or token <= 0 or token == child.process_handle:
                    raise TokenAccessError('token_handle_unverified')
                owner.handles.add(token)
                original = _valid_token(api.token_observation(token), child)
                duplicate = api.duplicate_impersonation_token(token)
                if type(duplicate) is not int or duplicate <= 0 or duplicate in owner.handles or duplicate == child.process_handle:
                    raise TokenAccessError('duplicate_handle_unverified')
                owner.handles.add(duplicate)
                copied = _valid_token(api.token_observation(duplicate), child, duplicate=True)
                if original.security_binding() != copied.security_binding():
                    raise TokenAccessError('duplicate_restrictions_changed')
                if api.process_identity(child.process_handle) != identity:
                    raise TokenAccessError('child_process_identity_changed')
                check()
                # A failed/exceptional setter is also followed by checked revert;
                # never assume a partially failed native operation changed nothing.
                impersonated = True
                if api.set_thread_token(duplicate) is not True:
                    raise TokenAccessError('impersonation_denied')
                # Querying the effective thread token prevents a fake successful
                # SetThreadToken return from being the sole evidence.
                if api.effective_thread_observation() != copied:
                    raise TokenAccessError('effective_thread_token_changed')
                for target in targets:
                    check()
                    handle = api.probe_open(target.path, target.access_mask, target.kind)
                    if type(handle) is not int or handle <= 0 or handle in owner.handles or handle == child.process_handle:
                        raise TokenAccessError('probe_handle_unverified')
                    owner.handles.add(handle)
                    actual = api.file_identity(handle)
                    if type(actual) is not dict:
                        raise TokenAccessError('held_path_identity_changed')
                    path = actual.get('final_path')
                    if isinstance(path, str) and path.startswith('\\\\?\\'):
                        path = path[4:]
                    if (type(actual) is not dict or path != target.path
                            or actual.get('volume') != target.volume or actual.get('file_id') != target.file_id
                            or actual.get('reparse') is not False or actual.get('kind') != target.kind
                            or target.kind == 'file' and actual.get('links') != 1):
                        raise TokenAccessError('held_path_identity_changed')
                    if api.close(handle) is not True:
                        raise TokenAccessError('probe_handle_close_failed')
                    owner.handles.remove(handle)
                    check()
                if api.effective_thread_observation() != copied or api.token_observation(token) != original or api.process_identity(child.process_handle) != identity:
                    raise TokenAccessError('token_or_process_changed_after_probe')
                check()
                owner.verified = True
                owner.reason = 'verified_readonly_access_at_observation'
            except Exception:
                owner.reason = 'readonly_access_denied_or_unverified'
                owner.verified = False
            finally:
                # On failed revert, do no further path, provider or host work.
                # Only close known kernel handles and let THIS thread terminate.
                if impersonated:
                    try:
                        owner.reverted = api.revert_to_self() is True
                    except Exception:
                        owner.reverted = False
                    if owner.reverted:
                        try:
                            owner.reverted = api.thread_has_token() is False
                        except Exception:
                            owner.reverted = False
                    if not owner.reverted:
                        owner.cleanup_fault = True
                        owner.verified = False
                        owner.reason = 'security_cleanup_fault_thread_exited'
                else:
                    owner.reverted = True
                if api is not None:
                    for handle in tuple(owner.handles):
                        try:
                            if api.close(handle) is not True:
                                raise TokenAccessError('close_failed')
                            owner.handles.remove(handle)
                        except Exception:
                            owner.cleanup_fault = True
                            owner.verified = False
                            owner.reason = 'handle_cleanup_unverified'
                owner.done.set()
        owner.thread = threading.Thread(target=worker, name='lingxi-readonly-token-access', daemon=False)
        try:
            owner.thread.start()
            owner.thread.join(timeout)
        except BaseException as error:
            # The caller can be interrupted while the dedicated thread still owns
            # impersonation/token handles. Preserve ownership before unwinding.
            owner.stop.set()
            error.probe_cleanup = owner
            raise
        if owner.thread.is_alive():
            owner.stop.set()
            reason = 'probe_thread_pending_unknown'
        else:
            reason = owner.reason
        cleanup = not owner.thread.is_alive() and owner.reverted and not owner.handles and not owner.cleanup_fault
        verified = owner.verified and cleanup and reason == 'verified_readonly_access_at_observation'
        evidence = AccessEvidence(child.pid, child.creation_time, child.sandbox_sid, expected,
                                  verified, False, cleanup, reason,
                                  'host_os_query' if verified else 'unverified')
        return ProbeResult(evidence, owner)


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [('sid', C.c_void_p), ('attributes', W.DWORD)]


class TOKEN_GROUPS(C.Structure):
    _fields_ = [('count', W.DWORD), ('first', SID_AND_ATTRIBUTES)]


class LUID(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]


class LUID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [('luid', LUID), ('attributes', W.DWORD)]


class TOKEN_PRIVILEGES(C.Structure):
    _fields_ = [('count', W.DWORD), ('first', LUID_AND_ATTRIBUTES)]


class TOKEN_STATISTICS(C.Structure):
    _fields_ = [('token_id', LUID), ('authentication_id', LUID), ('expiration', C.c_longlong),
                ('token_type', C.c_int), ('level', C.c_int), ('dynamic_charged', W.DWORD),
                ('dynamic_available', W.DWORD), ('group_count', W.DWORD),
                ('privilege_count', W.DWORD), ('modified_id', LUID)]


class NativeTokenAccessApi:
    """Lazy bindings; no privilege adjustment, ACL, owner or token creation APIs."""
    def __init__(self):
        if os.name != 'nt':
            raise TokenAccessError('windows_token_access_unsupported')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        def bind(dll, name, args, result):
            function = getattr(dll, name);function.argtypes = args;function.restype = result
        bind(self.k, 'GetProcessId', [W.HANDLE], W.DWORD)
        bind(self.k, 'GetProcessTimes', [W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], W.BOOL)
        bind(self.k, 'GetCurrentThread', [], W.HANDLE)
        bind(self.k, 'CloseHandle', [W.HANDLE], W.BOOL)
        bind(self.k, 'LocalFree', [C.c_void_p], C.c_void_p)
        bind(self.a, 'OpenProcessToken', [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
        bind(self.a, 'OpenThreadToken', [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL)
        bind(self.a, 'GetTokenInformation', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        bind(self.a, 'DuplicateTokenEx', [W.HANDLE, W.DWORD, C.c_void_p, C.c_int, C.c_int, C.POINTER(W.HANDLE)], W.BOOL)
        bind(self.a, 'SetThreadToken', [C.c_void_p, W.HANDLE], W.BOOL)
        bind(self.a, 'RevertToSelf', [], W.BOOL)
        bind(self.a, 'ConvertSidToStringSidW', [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL)
        bind(self.a, 'IsValidSid', [C.c_void_p], W.BOOL)
        bind(self.a, 'GetLengthSid', [C.c_void_p], W.DWORD)
        bind(self.k, 'CreateFileW', [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
        from app.plugin_windows_observer import BY_HANDLE_FILE_INFORMATION
        self._file_info_type = BY_HANDLE_FILE_INFORMATION
        bind(self.k, 'GetFileInformationByHandle', [W.HANDLE, C.POINTER(BY_HANDLE_FILE_INFORMATION)], W.BOOL)
        bind(self.k, 'GetFinalPathNameByHandleW', [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD], W.DWORD)

    def close(self, handle):
        if not self.k.CloseHandle(handle):
            raise TokenAccessError('close_failed')
        return True

    def process_identity(self, process):
        pid = self.k.GetProcessId(process)
        created, exited, kernel, user = (W.FILETIME() for _ in range(4))
        if not pid or not self.k.GetProcessTimes(process, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
            raise TokenAccessError('process_identity_query_failed')
        return pid, (created.dwHighDateTime << 32) | created.dwLowDateTime

    def open_process_token(self, process):
        token = W.HANDLE()
        if not self.a.OpenProcessToken(process, 0x8 | 0x2, C.byref(token)):
            raise TokenAccessError('child_token_query_duplicate_denied')
        return token.value

    def duplicate_impersonation_token(self, source):
        token = W.HANDLE()
        # TOKEN_QUERY | TOKEN_IMPERSONATE. No manual restricted/lowbox SID building.
        if not self.a.DuplicateTokenEx(source, 0x8 | 0x4, None, 2, 2, C.byref(token)):
            raise TokenAccessError('duplicate_lowbox_token_unsupported')
        return token.value

    def thread_has_token(self):
        token = W.HANDLE()
        if self.a.OpenThreadToken(self.k.GetCurrentThread(), 0x8, True, C.byref(token)):
            self.close(token.value)
            return True
        if C.get_last_error() != 1008:
            raise TokenAccessError('thread_token_state_unknown')
        return False

    def _info(self, token, kind):
        size = W.DWORD()
        self.a.GetTokenInformation(token, kind, None, 0, C.byref(size))
        if not 0 < size.value <= 65536:
            raise TokenAccessError('token_information_size')
        buffer = C.create_string_buffer(size.value)
        if not self.a.GetTokenInformation(token, kind, buffer, len(buffer), C.byref(size)):
            raise TokenAccessError('token_information_query_failed')
        return buffer

    def _sid(self, pointer):
        if not pointer or not self.a.IsValidSid(pointer) or not 8 <= self.a.GetLengthSid(pointer) <= 68:
            raise TokenAccessError('token_sid_invalid')
        value = W.LPWSTR()
        if not self.a.ConvertSidToStringSidW(pointer, C.byref(value)):
            raise TokenAccessError('token_sid_conversion_failed')
        try:
            if not value.value or len(value.value) > 256:
                raise TokenAccessError('token_sid_length')
            return value.value
        finally:
            if self.k.LocalFree(C.cast(value, C.c_void_p)):
                raise TokenAccessError('sid_string_cleanup_failed')

    def _groups(self, token, kind):
        buffer = self._info(token, kind)
        count = C.cast(buffer, C.POINTER(W.DWORD)).contents.value
        if count > 1024 or TOKEN_GROUPS.first.offset + count * C.sizeof(SID_AND_ATTRIBUTES) > len(buffer):
            raise TokenAccessError('token_groups_capacity')
        array = C.cast(C.addressof(buffer) + TOKEN_GROUPS.first.offset, C.POINTER(SID_AND_ATTRIBUTES))
        return tuple(sorted((self._sid(array[i].sid), array[i].attributes) for i in range(count)))

    def _integer(self, token, kind):
        buffer = self._info(token, kind)
        if len(buffer) < C.sizeof(W.DWORD):
            raise TokenAccessError('token_integer_size')
        return C.cast(buffer, C.POINTER(W.DWORD)).contents.value

    def token_observation(self, token):
        integer = lambda kind: self._integer(token, kind)
        app = integer(29)
        sid_buffer = self._info(token, 31)
        pointer = C.cast(sid_buffer, C.POINTER(C.c_void_p)).contents.value
        sid = self._sid(pointer) if pointer else None
        capabilities = self._groups(token, 30)
        label = self._info(token, 25)
        integrity = self._sid(C.cast(label, C.POINTER(SID_AND_ATTRIBUTES)).contents.sid)
        raw = self._info(token, 3);count = C.cast(raw, C.POINTER(W.DWORD)).contents.value
        if count > 1024 or TOKEN_PRIVILEGES.first.offset + count * C.sizeof(LUID_AND_ATTRIBUTES) > len(raw):
            raise TokenAccessError('token_privileges_capacity')
        values = C.cast(C.addressof(raw) + TOKEN_PRIVILEGES.first.offset, C.POINTER(LUID_AND_ATTRIBUTES))
        privileges = tuple(sorted((values[i].luid.low, values[i].luid.high, values[i].attributes) for i in range(count)))
        stats_buffer = self._info(token, 10)
        if len(stats_buffer) < C.sizeof(TOKEN_STATISTICS):
            raise TokenAccessError('token_statistics_size')
        stats = C.cast(stats_buffer, C.POINTER(TOKEN_STATISTICS)).contents
        token_type = integer(8)
        # SDK26100 winnt.h: TokenHasRestrictions=21, TokenIsRestricted=40.
        has_restrictions, restricted = integer(21), integer(40)
        user_buffer = self._info(token, 1)
        user_sid = self._sid(C.cast(user_buffer, C.POINTER(SID_AND_ATTRIBUTES)).contents.sid)
        if app not in (0, 1) or restricted not in (0, 1) or has_restrictions not in (0, 1):
            raise TokenAccessError('token_flags_invalid')
        return TokenObservation(app == 1, sid, capabilities, integrity, restricted == 1,
                                self._groups(token, 11), self._groups(token, 2), privileges,
                                integer(27), (stats.authentication_id.low, stats.authentication_id.high),
                                token_type, integer(9) if token_type == 2 else None,
                                has_restrictions == 1, user_sid)

    def set_thread_token(self, token):
        return bool(self.a.SetThreadToken(None, token))

    def revert_to_self(self):
        return bool(self.a.RevertToSelf())

    def effective_thread_observation(self):
        token = W.HANDLE()
        if not self.a.OpenThreadToken(self.k.GetCurrentThread(), 0x8, True, C.byref(token)):
            raise TokenAccessError('effective_thread_token_unknown')
        try:
            return self.token_observation(token.value)
        finally:
            self.close(token.value)

    def probe_open(self, path, access, kind):
        handle = self.k.CreateFileW(path, access, 1 | 2, None, 3, 0x00200000 | (0x02000000 if kind == 'directory' else 0), None)
        if not handle or handle == C.c_void_p(-1).value:
            raise TokenAccessError('readonly_path_access_denied')
        return handle

    def file_identity(self, handle):
        value = self._file_info_type()
        if not self.k.GetFileInformationByHandle(handle, C.byref(value)):
            raise TokenAccessError('probe_file_information_failed')
        length = self.k.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not 0 < length <= 32768:
            raise TokenAccessError('probe_final_path_failed')
        text = C.create_unicode_buffer(length + 1)
        actual = self.k.GetFinalPathNameByHandleW(handle, text, len(text), 0)
        if not actual or actual >= len(text):
            raise TokenAccessError('probe_final_path_failed')
        return {'final_path': text.value, 'volume': value.volume,
                'file_id': (value.index_high << 32) | value.index_low,
                'reparse': bool(value.attributes & 0x400),
                'kind': 'directory' if value.attributes & 0x10 else 'file', 'links': value.links}
