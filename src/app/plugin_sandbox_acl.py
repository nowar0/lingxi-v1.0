"""Exact-root AppContainer DACL grant adapter; import/planning perform NO native I/O.

Execution needs an explicit trusted handle-based path checker and one-use human
confirmation. It writes DACL only: never owner/group/MIC, inheritance protection,
parent/global ACLs, profiles, tokens or process state. Tests use injected fake APIs.
A successful root grant does not attest descendant access or OS sandbox isolation;
provisioning must separately inspect every descendant and the running token.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
import re
import threading
from typing import Callable

from app.plugin_sandbox_policy import canonical_local_path

MASKS = {'RX': 0x1200A9, 'RW': 0x13019F, 'X': 0x20}
SCOPES = {'this-directory-only': 0, 'this-directory-and-descendants': 3}
_SID = re.compile(r'^S-1-15-2-(?:[1-9][0-9]*)(?:-[0-9]+)*$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
PUBLIC_PACKAGE_SIDS = frozenset(('S-1-15-2-1', 'S-1-15-2-2'))
WRITE_BITS = (0x10000000 | 0x40000000 | 0x10000 | 0x40000 | 0x80000
              | 0x2 | 0x4 | 0x10 | 0x100 | 0x40)


class AclError(ValueError):
    """Safe failure code; callers should journal needs_review after a write attempt."""
    def __init__(self, code, *, write_attempted=False, native_operation=None, win32_status=None):
        super().__init__(code)
        self.code, self.write_attempted = code, write_attempted
        if native_operation in {'GetNamedSecurityInfoW','SetEntriesInAclW','SetNamedSecurityInfoW'} and type(win32_status) is int and 0<=win32_status<=0xffffffff:
            self.native_diagnostic={'operation':native_operation,'win32_status':win32_status,
                                    'dacl_write_entered':write_attempted}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False,
        default=lambda item: asdict(item) if is_dataclass(item) else (_ for _ in ()).throw(TypeError('not JSON'))).encode('utf-8')).hexdigest()


def _inside(path, root):
    return path.casefold().startswith(root.casefold() + '\\')


@dataclass(frozen=True)
class PathIdentity:
    path: str
    identity_fingerprint: str
    source: str = 'unverified'
    handle_based: bool = False
    ancestors_verified: bool = False
    has_reparse_point: bool = True
    ancestor_reparse_point: bool = True
    kind: str = 'unknown'
    hardlink_count: int | None = None


def _identity(checker, path):
    if not callable(checker):
        raise AclError('trusted_path_checker_required')
    try:
        value = checker(path)
    except Exception as error:
        raise AclError('path_observation_failed') from error
    if (type(value) is not PathIdentity or value.path != path
            or value.source != 'host_os_query' or value.handle_based is not True
            or value.ancestors_verified is not True or value.has_reparse_point is not False
            or value.ancestor_reparse_point is not False
            or value.kind not in ('file', 'directory')
            or not isinstance(value.identity_fingerprint, str)
            or not _SHA.fullmatch(value.identity_fingerprint)
            or (value.kind == 'file' and (type(value.hardlink_count) is not int
                                         or value.hardlink_count != 1))):
        raise AclError('unsafe_or_unverified_path')
    return value


def _sddl(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 65536
            or '\x00' in value or not re.search(r'(?:^|[OGS]:[^()]*)D:', value)):
        # Most callers provide full O:G:D:S SDDL; D: alone is also acceptable.
        if not isinstance(value, str) or not value.startswith('D:') or '\x00' in value or len(value) > 65536:
            raise AclError('invalid_before_sddl')
    if value.count('(') != value.count(')'):
        raise AclError('invalid_before_sddl')
    return value


@dataclass(frozen=True)
class GrantPlan:
    approved_run_root: str
    path: str
    sid: str
    rights: str
    scope: str
    access_mask: int
    inheritance: int
    before_sddl: str
    before_sddl_sha256: str
    path_identity: PathIdentity
    root_identity: PathIdentity
    fingerprint: str


def plan_grant(approved_run_root, path, sid, rights, scope, observed_before_sddl,
               *, check_path: Callable[[str], PathIdentity]):
    """Pure plan using injected read-only handle evidence, not a path stat fallback.

    Only the root's own traversal X or descendant RX/RW grants are representable.
    The provisioner must additionally bind runtime/code/scratch roles to its plan.
    """
    canonical_local_path(approved_run_root); canonical_local_path(path)
    if any(part.casefold() in {'appdata', 'credentials', 'vault'}
           for part in approved_run_root.split('\\')):
        raise AclError('protected_root')
    if (not isinstance(sid, str) or not _SID.fullmatch(sid)
            or sid in PUBLIC_PACKAGE_SIDS or len(sid) > 256):
        raise AclError('specific_appcontainer_sid_required')
    if rights not in MASKS or scope not in SCOPES:
        raise AclError('unsupported_grant')
    if path == approved_run_root:
        if rights != 'X' or scope != 'this-directory-only':
            raise AclError('root_traverse_only')
    elif not _inside(path, approved_run_root):
        raise AclError('outside_approved_run_root')
    if rights == 'X' and scope != 'this-directory-only':
        raise AclError('traverse_not_inheritable')
    if rights != 'X' and scope != 'this-directory-and-descendants':
        raise AclError('explicit_subtree_scope_required')
    before = _sddl(observed_before_sddl)
    root_identity = _identity(check_path, approved_run_root)
    identity = root_identity if path == approved_run_root else _identity(check_path, path)
    if root_identity.kind != 'directory' or identity.kind != 'directory':
        raise AclError('existing_directory_required')
    values = dict(approved_run_root=approved_run_root, path=path, sid=sid,
                  rights=rights, scope=scope, access_mask=MASKS[rights], inheritance=SCOPES[scope],
                  before_sddl=before, before_sddl_sha256=hashlib.sha256(before.encode()).hexdigest(),
                  path_identity=identity, root_identity=root_identity)
    return GrantPlan(**values, fingerprint=_digest(values))


def _snapshot(api, path):
    try:
        value = api.inspect(path)
    except Exception as error:
        raise AclError('security_descriptor_read_failed') from error
    required = {'sddl', 'owner', 'group', 'label_sddl', 'dacl_present', 'dacl_protected', 'aces'}
    if type(value) is not dict or set(value) != required:
        raise AclError('incomplete_security_descriptor')
    _sddl(value['sddl'])
    if (value['dacl_present'] is not True or type(value['dacl_protected']) is not bool
            or not all(isinstance(value[k], str) and len(value[k]) <= 65536
                       for k in ('owner', 'group', 'label_sddl'))
            or not value['owner'] or not value['group'] or type(value['aces']) is not list
            or len(value['aces']) > 4096):
        raise AclError('ambiguous_security_descriptor')
    aces = []
    for ace in value['aces']:
        if (type(ace) is not dict or set(ace) != {'type', 'flags', 'mask', 'sid'}
                or type(ace['type']) is not int or ace['type'] not in (0, 1)
                or type(ace['flags']) is not int or not 0 <= ace['flags'] <= 255
                or type(ace['mask']) is not int or not 0 <= ace['mask'] <= 0xFFFFFFFF
                or not isinstance(ace['sid'], str) or not re.fullmatch(r'S-\d+(?:-\d+)+', ace['sid'])):
            raise AclError('unsupported_or_ambiguous_ace')
        aces.append(dict(ace))
    return {**value, 'aces': aces}


def _target_safe(snapshot, plan):
    for ace in snapshot['aces']:
        if ace['sid'] in PUBLIC_PACKAGE_SIDS and ace['type'] == 0 and ace['mask'] & WRITE_BITS:
            raise AclError('public_package_write_access_not_strict')
        if ace['sid'] != plan.sid:
            continue
        # Existing denies are never removed; an overlapping deny cannot prove access.
        if ace['type'] == 1:
            if ace['mask'] & plan.access_mask:
                raise AclError('existing_target_deny')
        elif (ace['mask'] & ~plan.access_mask or ace['flags'] & ~plan.inheritance):
            raise AclError('existing_target_access_broader_than_plan')


def _ace_key(ace):
    return tuple(ace[k] for k in ('type', 'flags', 'mask', 'sid'))


def _verify_delta(before, after, plan):
    if any(before[key] != after[key] for key in ('owner', 'group', 'label_sddl', 'dacl_present', 'dacl_protected')):
        raise AclError('owner_group_label_or_inheritance_changed', write_attempted=True)
    # Preserve order, not just a multiset: deny/allow order can change semantics.
    non_target = lambda snapshot: tuple(_ace_key(a) for a in snapshot['aces'] if a['sid'] != plan.sid)
    if non_target(before) != non_target(after):
        raise AclError('other_principal_aces_changed', write_attempted=True)
    denies = lambda snapshot: tuple(_ace_key(a) for a in snapshot['aces'] if a['sid'] == plan.sid and a['type'] == 1)
    if denies(before) != denies(after):
        raise AclError('target_denies_changed', write_attempted=True)
    try:
        _target_safe(after, plan)
    except AclError as error:
        raise AclError(error.code, write_attempted=True) from error
    target = [a for a in after['aces'] if a['sid'] == plan.sid and a['type'] == 0]
    mask = 0
    for ace in target:
        if ace['flags'] == plan.inheritance:
            mask |= ace['mask']
    if mask != plan.access_mask:
        raise AclError('exact_access_not_verified', write_attempted=True)


class NativeAclAdapter:
    """Explicit execution only. Default native API is loaded lazily after approval.

    check_path is trusted host code returning handle identity/ancestor observations.
    It must hold or otherwise race-protect directory handles for production use;
    pre/post identity checks alone cannot defeat hostile same-user filesystem writes.
    """
    def __init__(self, *, check_path, api=None):
        if not callable(check_path):
            raise AclError('trusted_path_checker_required')
        self.check_path, self._api = check_path, api
        self._lock, self._used = threading.RLock(), set()

    @property
    def api(self):
        if self._api is None:
            self._api = NativeAclApi()
        return self._api

    def inspect(self, path):
        canonical_local_path(path)
        _identity(self.check_path, path)
        return _snapshot(self.api, path)

    def apply(self, plan, *, user_confirmed, fingerprint):
        if type(plan) is not GrantPlan or user_confirmed is not True or fingerprint != plan.fingerprint:
            raise AclError('exact_explicit_confirmation_required')
        # Rebuild the complete plan with fresh trusted identity before any native write.
        fresh = plan_grant(plan.approved_run_root, plan.path, plan.sid, plan.rights,
            plan.scope, plan.before_sddl, check_path=self.check_path)
        if fresh != plan:
            raise AclError('path_identity_or_plan_changed')
        with self._lock:
            if fingerprint in self._used:
                raise AclError('approval_already_attempted')
            if len(self._used) >= 4096:
                raise AclError('approval_capacity_exceeded')
            self._used.add(fingerprint)
            before = _snapshot(self.api, plan.path)
            if before['sddl'] != plan.before_sddl:
                raise AclError('before_descriptor_changed')
            _target_safe(before, plan)
            # Recheck independently approved root as well as target, not just lexical text.
            if (_identity(self.check_path, plan.path) != plan.path_identity
                    or _identity(self.check_path, plan.approved_run_root) != plan.root_identity):
                raise AclError('path_identity_changed_before_write')
            try:
                self.api.grant(plan.path, plan.sid, plan.access_mask, plan.inheritance)
            except Exception as error:
                raise AclError('dacl_write_failed_needs_review', write_attempted=True) from error
            try:
                after = _snapshot(self.api, plan.path)
                _verify_delta(before, after, plan)
                if (_identity(self.check_path, plan.path) != plan.path_identity
                        or _identity(self.check_path, plan.approved_run_root) != plan.root_identity):
                    raise AclError('path_identity_changed_after_write', write_attempted=True)
            except AclError as error:
                raise AclError(error.code, write_attempted=True) from error
            return {'source':'host_os_query', 'path':plan.path, 'sid':plan.sid,
                'rights':plan.rights, 'scope':plan.scope, 'fingerprint':plan.fingerprint,
                'before_sddl':before['sddl'], 'after_sddl':after['sddl'],
                'identity_fingerprint':plan.path_identity.identity_fingerprint,
                'dacl_only':True, 'owner_group_label_preserved':True,
                'other_principal_aces_preserved':True, 'recursive_verification_required':True,
                'os_sandbox_verified':False}


class NativeAclApi:
    """Real DACL-only Win32 writer, instantiated only during explicitly approved use."""
    OWNER_SECURITY_INFORMATION = 0x1
    GROUP_SECURITY_INFORMATION = 0x2
    DACL_SECURITY_INFORMATION = 0x4
    LABEL_SECURITY_INFORMATION = 0x10
    READ_INFORMATION = 0x17

    def __init__(self):
        if os.name != 'nt':
            raise AclError('windows_required')
        self.adv = C.WinDLL('advapi32', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self._bind()

    def _bind(self):
        pointer = C.c_void_p
        self.adv.GetNamedSecurityInfoW.argtypes = [W.LPWSTR,W.DWORD,W.DWORD,C.POINTER(pointer),
            C.POINTER(pointer),C.POINTER(pointer),C.POINTER(pointer),C.POINTER(pointer)]
        self.adv.GetNamedSecurityInfoW.restype = W.DWORD
        self.adv.SetNamedSecurityInfoW.argtypes = [W.LPWSTR,W.DWORD,W.DWORD,pointer,pointer,pointer,pointer]
        self.adv.SetNamedSecurityInfoW.restype = W.DWORD
        self.adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [pointer,W.DWORD,W.DWORD,
            C.POINTER(W.LPWSTR),C.POINTER(W.DWORD)]
        self.adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = W.BOOL
        self.adv.IsValidSid.argtypes = [pointer]
        self.adv.IsValidSid.restype = W.BOOL
        self.adv.GetLengthSid.argtypes = [pointer]
        self.adv.GetLengthSid.restype = W.DWORD
        self.adv.ConvertSidToStringSidW.argtypes = [pointer,C.POINTER(W.LPWSTR)]
        self.adv.ConvertSidToStringSidW.restype = W.BOOL
        self.adv.ConvertStringSidToSidW.argtypes = [W.LPCWSTR,C.POINTER(pointer)]
        self.adv.ConvertStringSidToSidW.restype = W.BOOL
        self.adv.GetSecurityDescriptorControl.argtypes = [pointer,C.POINTER(W.WORD),C.POINTER(W.DWORD)]
        self.adv.GetSecurityDescriptorControl.restype = W.BOOL
        self.adv.GetAclInformation.argtypes = [pointer,pointer,W.DWORD,C.c_int]
        self.adv.GetAclInformation.restype = W.BOOL
        self.adv.GetAce.argtypes = [pointer,W.DWORD,C.POINTER(pointer)]
        self.adv.GetAce.restype = W.BOOL
        self.adv.SetEntriesInAclW.argtypes = [W.ULONG,pointer,pointer,C.POINTER(pointer)]
        self.adv.SetEntriesInAclW.restype = W.DWORD
        self.kernel.LocalFree.argtypes = [pointer]
        self.kernel.LocalFree.restype = pointer

    def _sid(self, sid):
        text = W.LPWSTR()
        if not sid or not self.adv.ConvertSidToStringSidW(sid,C.byref(text)):
            raise AclError('sid_read_failed')
        try:return text.value
        finally:self.kernel.LocalFree(C.cast(text,C.c_void_p))

    def _sddl(self, descriptor, flags):
        text, length = W.LPWSTR(), W.DWORD()
        if not self.adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor,1,flags,C.byref(text),C.byref(length)):
            raise AclError('sddl_read_failed')
        try:return text.value or ''
        finally:self.kernel.LocalFree(C.cast(text,C.c_void_p))

    def _read(self, path):
        owner= C.c_void_p();group=C.c_void_p();dacl=C.c_void_p();sacl=C.c_void_p();descriptor=C.c_void_p()
        status=self.adv.GetNamedSecurityInfoW(path,1,self.READ_INFORMATION,C.byref(owner),C.byref(group),
            C.byref(dacl),C.byref(sacl),C.byref(descriptor))
        if status:
            if descriptor:self.kernel.LocalFree(descriptor)
            raise AclError('descriptor_read_failed',native_operation='GetNamedSecurityInfoW',win32_status=int(status))
        return owner,group,dacl,sacl,descriptor

    def inspect(self, path):
        owner,group,dacl,_,descriptor=self._read(path)
        try:
            control=W.WORD();revision=W.DWORD()
            if not self.adv.GetSecurityDescriptorControl(descriptor,C.byref(control),C.byref(revision)):
                raise AclError('descriptor_control_read_failed')
            if not dacl or not control.value & 0x4:
                raise AclError('null_or_absent_dacl')
            class ACL_SIZE_INFORMATION(C.Structure):
                _fields_=[('AceCount',W.DWORD),('AclBytesInUse',W.DWORD),('AclBytesFree',W.DWORD)]
            info=ACL_SIZE_INFORMATION()
            if not self.adv.GetAclInformation(dacl,C.byref(info),C.sizeof(info),2) or info.AceCount>4096:
                raise AclError('acl_information_failed')
            aces=[]
            for index in range(info.AceCount):
                ace=C.c_void_p()
                if not self.adv.GetAce(dacl,index,C.byref(ace)):
                    raise AclError('ace_read_failed')
                header=C.string_at(ace,4)
                typ,flags,size=header[0],header[1],int.from_bytes(header[2:4],'little')
                if typ not in (0,1) or size<16:
                    raise AclError('unsupported_ace_type')
                mask=int.from_bytes(C.string_at(ace.value+4,4),'little')
                sid_pointer=C.c_void_p(ace.value+8)
                if (not self.adv.IsValidSid(sid_pointer)
                        or self.adv.GetLengthSid(sid_pointer)>size-8):
                    raise AclError('invalid_ace_sid')
                aces.append({'type':typ,'flags':flags,'mask':mask,'sid':self._sid(sid_pointer)})
            return {'sddl':self._sddl(descriptor,self.READ_INFORMATION),
                    'owner':self._sid(owner),'group':self._sid(group),
                    'label_sddl':self._sddl(descriptor,self.LABEL_SECURITY_INFORMATION),
                    'dacl_present':True,'dacl_protected':bool(control.value & 0x1000),'aces':aces}
        finally:self.kernel.LocalFree(descriptor)

    def grant(self, path, sid, mask, inheritance):
        # Low-level implementation is internal to NativeAclAdapter's explicit gate.
        # No PROTECTED/UNPROTECTED_DACL flags and no owner/group/label pointers.
        canonical_local_path(path)
        if not isinstance(sid, str) or not _SID.fullmatch(sid) or sid in PUBLIC_PACKAGE_SIDS:
            raise AclError('specific_appcontainer_sid_required')
        if mask not in MASKS.values() or inheritance not in SCOPES.values():
            raise AclError('unsupported_native_acl_grant')
        sid_pointer=C.c_void_p();new_acl=C.c_void_p();descriptor=None
        if not self.adv.ConvertStringSidToSidW(sid,C.byref(sid_pointer)):
            raise AclError('sid_conversion_failed')
        try:
            _,_,old_acl,_,descriptor=self._read(path)
            if not old_acl:raise AclError('null_dacl_refused')
            class TRUSTEE_W(C.Structure):
                pass
            TRUSTEE_W._fields_=[('pMultipleTrustee',C.POINTER(TRUSTEE_W)),('MultipleTrusteeOperation',C.c_int),
                ('TrusteeForm',C.c_int),('TrusteeType',C.c_int),('ptstrName',C.c_void_p)]
            class EXPLICIT_ACCESS_W(C.Structure):
                _fields_=[('grfAccessPermissions',W.DWORD),('grfAccessMode',C.c_int),
                    ('grfInheritance',W.DWORD),('Trustee',TRUSTEE_W)]
            entry=EXPLICIT_ACCESS_W(mask,1,inheritance,TRUSTEE_W(None,0,0,5,sid_pointer))
            status=self.adv.SetEntriesInAclW(1,C.byref(entry),old_acl,C.byref(new_acl))
            if status or not new_acl:raise AclError('merge_acl_failed',native_operation='SetEntriesInAclW',win32_status=int(status))
            status=self.adv.SetNamedSecurityInfoW(path,1,self.DACL_SECURITY_INFORMATION,None,None,new_acl,None)
            if status:raise AclError('set_dacl_failed',write_attempted=True,native_operation='SetNamedSecurityInfoW',win32_status=int(status))
        finally:
            if new_acl:self.kernel.LocalFree(new_acl)
            if descriptor:self.kernel.LocalFree(descriptor)
            self.kernel.LocalFree(sid_pointer)
