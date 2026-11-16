"""Read-only Windows host observations. Import and construction perform no native I/O.

This is not ACL verification, provisioning, a launcher or a complete host verifier.
Loopback configuration uses the separate SDK-audited read-only provider; absence is
not full network enforcement proof, and unknown observation ALWAYS remains blocked.
Only an explicitly approved root can be inspected, apart from readonly ancestor handles.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes as W
import hashlib
import ntpath
import os
import re

from app.plugin_sandbox_policy import (PathObservation,
    canonical_local_path, inventory_fingerprint)

MAX_FILES = 50000
MAX_TOTAL_BYTES = 8 * 1024**3
REPARSE = 0x400
DIRECTORY = 0x10
_APP_SID = re.compile(r'^S-1-15-2-[0-9]+(?:-[0-9]+)*$')


def _app_sid(value):
    return isinstance(value, str) and len(value) <= 256 and _APP_SID.fullmatch(value) is not None


class ObserverError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileEvidence:
    path: str
    kind: str
    volume: int
    file_id: int
    attributes: int
    links: int
    size: int
    modified: int
    final_path: str
    sha256: str | None = None
    source: str = 'host_os_query'


@dataclass(frozen=True)
class IntegrityEvidence:
    verified: bool = False
    rid: int | None = None
    sid: str | None = None
    reason: str = 'unverified'
    source: str = 'host_os_query'


@dataclass(frozen=True)
class ProcessEvidence:
    verified: bool = False
    pid: int | None = None
    is_appcontainer: bool | None = None
    sid: str | None = None
    capabilities: tuple[str, ...] | None = None
    reason: str = 'unverified'
    source: str = 'host_os_query'


@dataclass(frozen=True)
class NetworkEvidence:
    verified: bool = False
    has_loopback_exemption: bool | None = None
    sid: str | None = None
    reason: str = 'loopback_observer_unsupported'
    source: str = 'host_os_query'


def _inside(path, root):
    return path.casefold() == root.casefold() or path.casefold().startswith(root.casefold()+'\\')


def _final(value):
    # Device-form caller inputs are rejected separately. Strip only the OS's local
    # DOS result prefix, never UNC/volume aliases, then compare to the approved path.
    if value.startswith('\\\\?\\') and len(value) > 6 and value[5:7] == ':\\':
        value = value[4:]
    if len(value) == 3 and value[0] in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' and value[1:] == ':\\':
        return value  # Readonly drive-root ancestor; never an approved inspection root.
    try:
        return canonical_local_path(value)
    except (TypeError, ValueError) as error:
        raise ObserverError('final_path_not_local_canonical') from error


def _ancestors(path):
    result=[]; parent=ntpath.dirname(path)
    while parent and parent != path:
        result.append(parent)
        path,parent=parent,ntpath.dirname(parent)
    return tuple(reversed(result))


class ObservationLease:
    """Owns no-delete-shared handles until explicit close/context exit.

    The host must revalidate immediately before resume/IO and retain the lease.
    Sharing writes is intentional; metadata+hash revalidation detects changes but
    is not a hostile-filesystem race proof or an ACL attestation.
    """
    def __init__(self, api):
        self.api=api; self._handles=[]; self._files=[]; self._directories={}
        self._missing={}; self.closed=False

    def _open(self, path, read=False):
        handle=self.api.open_readonly(path, read)
        if not handle:
            raise ObserverError('readonly_open_failed')
        self._handles.append((path,handle))
        return handle

    def _evidence(self, path, handle, *, read=False):
        info=self.api.file_info(handle)
        keys={'volume','file_id','attributes','links','size','modified','final_path'}
        if type(info) is not dict or set(info)!=keys or any(type(info[k]) is not int or info[k]<0 for k in keys-{'final_path'}):
            raise ObserverError('file_information_incomplete')
        final=_final(info['final_path'])
        if final.casefold()!=path.casefold():
            raise ObserverError('final_path_mismatch')
        if info['attributes'] & REPARSE:
            raise ObserverError('reparse_point')
        directory=bool(info['attributes'] & DIRECTORY)
        if not directory and info['links'] != 1:
            raise ObserverError('hardlink_or_unknown_links')
        digest=None
        if read:
            if directory or info['size']>MAX_TOTAL_BYTES:
                raise ObserverError('invalid_inventory_file')
            digest=self.api.hash_file(handle, info['size'])
            after=self.api.file_info(handle)
            if after!=info or type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                raise ObserverError('file_changed_during_read')
        return FileEvidence(path,'directory' if directory else 'file',
            info['volume'],info['file_id'],info['attributes'],info['links'],info['size'],
            info['modified'],final,digest)

    def checker(self, path):
        if self.closed:raise ObserverError('lease_closed')
        if path in self._missing:
            return PathObservation(path,'missing',True,True,False,False,scratch_fresh=True)
        for handle,evidence in self._files:
            if evidence.path==path:
                return PathObservation(path,evidence.kind,True,True,False,False,
                    evidence.links,evidence.sha256,evidence.size,
                    inventory_sha256=self._directories.get(path))
        raise ObserverError('path_not_observed')

    def revalidate(self):
        if self.closed:raise ObserverError('lease_closed')
        for handle,before in self._files:
            current=self._evidence(before.path,handle,read=before.kind=='file')
            if before.kind=='directory':
                # A legitimate sibling call may update an ancestor's mtime/size.
                # Keep exact held identity/type/attributes; inventory directories
                # still verify every name below, while files keep hash+metadata.
                stable=lambda item:(item.path,item.kind,item.volume,item.file_id,item.attributes,item.links,item.final_path,item.source)
                if stable(current)!=stable(before):raise ObserverError('held_object_changed')
            elif current!=before:raise ObserverError('held_object_changed')
        for directory,expected in self._directories.items():
            # Inventory itself was fully hashed below; this catches additions and
            # directory aliasing, not just edits to known files.
            observed=self.api.enumerate_tree(directory)
            if tuple(sorted(observed))!=self._names[directory]:
                raise ObserverError('inventory_names_changed')
        for path in self._missing:
            if self.api.path_exists(path):raise ObserverError('fresh_scratch_no_longer_missing')
        return True

    def close(self):
        failed=[]
        for path,handle in reversed(self._handles):
            try:
                if self.api.close(handle) is False:raise ObserverError('close_failed')
            except Exception:failed.append((path,handle))
        self._handles=list(reversed(failed));self.closed=not self._handles
        if failed:raise ObserverError('readonly_handle_cleanup_failed')

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class WindowsObserver:
    def __init__(self, approved_root, *, native_api=None, observation_approved=False):
        self.root=canonical_local_path(approved_root)
        if any(p.casefold() in {'appdata','credentials','vault'} for p in self.root.split('\\')):
            raise ObserverError('protected_root')
        self._api=native_api;self.approved=observation_approved is True

    @property
    def api(self):
        if not self.approved:raise ObserverError('explicit_observation_approval_required')
        if self._api is None:self._api=NativeObserverApi()
        return self._api

    def current_integrity(self):
        try:
            sid,rid=self.api.current_integrity()
            if type(rid) is not int or sid!=f'S-1-16-{rid}':raise ObserverError('integrity_information_invalid')
            return IntegrityEvidence(rid==8192,rid,sid,'medium_verified' if rid==8192 else 'normal_medium_required')
        except Exception as error:
            return IntegrityEvidence(reason=error.code if isinstance(error,ObserverError) else 'integrity_query_failed')

    def appcontainer(self, process_handle, *, expected_pid, expected_sid):
        try:
            if type(process_handle) is not int or process_handle<=0 or type(expected_pid) is not int or expected_pid<=0:
                raise ObserverError('invalid_owned_process_handle')
            if not _app_sid(expected_sid):
                raise ObserverError('invalid_expected_sid')
            value=self.api.process_identity(process_handle)
            if type(value) is not dict or set(value)!={'pid','is_appcontainer','sid','capabilities'}:
                raise ObserverError('process_information_incomplete')
            if (value['pid']!=expected_pid or value['is_appcontainer'] is not True or value['sid']!=expected_sid
                    or type(value['capabilities']) is not tuple or value['capabilities']):
                raise ObserverError('process_identity_or_capabilities_mismatch')
            return ProcessEvidence(True,value['pid'],True,value['sid'],(), 'appcontainer_identity_verified')
        except Exception as error:
            return ProcessEvidence(reason=error.code if isinstance(error,ObserverError) else 'process_identity_query_failed')

    def network_isolation(self, sid):
        """Observe loopback-exemption absence only, NOT full network enforcement.

        Caller must separately verify an actual AppContainer token with zero
        capabilities and probe the OS enforcement before claiming network isolation.
        """
        try:
            if not _app_sid(sid):raise ObserverError('invalid_expected_sid')
            exemption=self.api.has_loopback_exemption(sid)
            if type(exemption) is not bool:raise ObserverError('loopback_observation_unknown')
            return NetworkEvidence(not exemption,exemption,sid,'no_loopback_exemption_verified' if not exemption else 'loopback_exemption_present')
        except Exception as error:
            return NetworkEvidence(sid=sid,reason=error.code if isinstance(error,ObserverError) else 'loopback_query_failed')

    def inspect(self, *, inventories, missing_scratch=None):
        """Hold ancestors and complete inventory. No directory creation or ACL writes.

        inventories is {runtime_or_code_directory: tuple[InventoryEntry,...]}.
        Missing scratch is only a plan-time observation; after provisioning a new
        lease and separate ACL verifier are required for its created directory.
        """
        if type(inventories) is not dict or not inventories or len(inventories)>4:
            raise ObserverError('bounded_inventories_required')
        integrity=self.current_integrity()
        if not integrity.verified:
            raise ObserverError(integrity.reason)
        lease=ObservationLease(self.api);lease._names={}
        try:
            paths={self.root}
            for directory,entries in inventories.items():
                canonical_local_path(directory)
                if not _inside(directory,self.root) or directory.casefold()==self.root.casefold():raise ObserverError('inventory_outside_root')
                inventory_fingerprint(entries)
                paths.update((directory,*_ancestors(directory)))
            if missing_scratch is not None:
                canonical_local_path(missing_scratch)
                if not _inside(missing_scratch,self.root):raise ObserverError('scratch_outside_root')
                paths.update(_ancestors(missing_scratch))
            for path in sorted(paths,key=lambda p:(p.count('\\'),p)):
                # Parents outside root are observed readonly, never inventoried or granted.
                handle=lease._open(path)
                evidence=lease._evidence(path,handle)
                if evidence.kind!='directory':raise ObserverError('ancestor_not_directory')
                lease._files.append((handle,evidence))
            total=0;files=0
            for directory,entries in inventories.items():
                expected_names=[]
                for entry in entries:
                    expected_names.append(entry.relative_path)
                    pieces=entry.relative_path.split('/')
                    expected_names.extend('/'.join(pieces[:i])+'/' for i in range(1,len(pieces)))
                names=tuple(sorted(set(expected_names)))
                observed=self.api.enumerate_tree(directory)
                if type(observed) is not tuple or tuple(sorted(observed))!=names:
                    raise ObserverError('inventory_extra_missing_or_alias')
                if len(observed)>MAX_FILES*2:raise ObserverError('inventory_capacity')
                for name in sorted(x for x in names if x.endswith('/')):
                    path=directory+'\\'+name[:-1].replace('/','\\')
                    handle=lease._open(path);ev=lease._evidence(path,handle)
                    if ev.kind!='directory':raise ObserverError('inventory_directory_type')
                    lease._files.append((handle,ev))
                for entry in entries:
                    total+=entry.size;files+=1
                    if total>MAX_TOTAL_BYTES or files>MAX_FILES:raise ObserverError('inventory_capacity')
                    path=directory+'\\'+entry.relative_path.replace('/','\\')
                    handle=lease._open(path,True);ev=lease._evidence(path,handle,read=True)
                    if ev.kind!='file' or ev.sha256!=entry.sha256 or ev.size!=entry.size:raise ObserverError('inventory_content_mismatch')
                    lease._files.append((handle,ev))
                lease._directories[directory]=inventory_fingerprint(entries);lease._names[directory]=names
            if missing_scratch is not None:
                if self.api.path_exists(missing_scratch):raise ObserverError('scratch_already_exists')
                lease._missing[missing_scratch]=True
            lease.revalidate()
            return lease
        except BaseException as original:
            try:
                lease.close()
            except BaseException:
                # Retain ownership on failure, rather than losing a handle lease
                # in a local variable or masking the initial verification error.
                original.observation_cleanup = lease
                original.add_note('Readonly observer cleanup incomplete; explicit host retry required')
            raise


# Win32 ctypes declarations do not load DLLs or make calls until explicit use.
class BY_HANDLE_FILE_INFORMATION(C.Structure):
    _fields_=[('attributes',W.DWORD),('creation',W.FILETIME),('access',W.FILETIME),
        ('write',W.FILETIME),('volume',W.DWORD),('size_high',W.DWORD),('size_low',W.DWORD),
        ('links',W.DWORD),('index_high',W.DWORD),('index_low',W.DWORD)]

class SID_AND_ATTRIBUTES(C.Structure):
    _fields_=[('sid',C.c_void_p),('attributes',W.DWORD)]


class _NativeObservationCleanup:
    def __init__(self,api,handle):self.api=api;self.handle=handle;self.closed=False
    def close(self):
        if self.closed:return
        if self.api.close(self.handle) is not True:raise ObserverError('close_failed')
        self.closed=True;self.handle=None


class NativeObserverApi:
    """Lazy readonly API. No profile/ACL/network mutation APIs are bound."""
    def __init__(self):
        if os.name!='nt':raise ObserverError('windows_observer_unsupported')
        self.k=C.WinDLL('kernel32',use_last_error=True)
        self.a=C.WinDLL('advapi32',use_last_error=True)
        def bind(dll,name,args,result):
            fn=getattr(dll,name);fn.argtypes=args;fn.restype=result;return fn
        bind(self.k,'CreateFileW',[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE],W.HANDLE)
        bind(self.k,'CloseHandle',[W.HANDLE],W.BOOL)
        bind(self.k,'GetFileInformationByHandle',[W.HANDLE,C.POINTER(BY_HANDLE_FILE_INFORMATION)],W.BOOL)
        bind(self.k,'GetFinalPathNameByHandleW',[W.HANDLE,W.LPWSTR,W.DWORD,W.DWORD],W.DWORD)
        bind(self.k,'SetFilePointerEx',[W.HANDLE,C.c_longlong,C.POINTER(C.c_longlong),W.DWORD],W.BOOL)
        bind(self.k,'ReadFile',[W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p],W.BOOL)
        bind(self.k,'GetFileAttributesW',[W.LPCWSTR],W.DWORD)
        bind(self.k,'GetCurrentProcess',[],W.HANDLE)
        bind(self.k,'GetProcessId',[W.HANDLE],W.DWORD)
        bind(self.k,'LocalFree',[C.c_void_p],C.c_void_p)
        bind(self.a,'OpenProcessToken',[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)],W.BOOL)
        bind(self.a,'GetTokenInformation',[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)],W.BOOL)
        bind(self.a,'ConvertSidToStringSidW',[C.c_void_p,C.POINTER(W.LPWSTR)],W.BOOL)

    def open_readonly(self,path,read=False):
        # FILE_READ_ATTRIBUTES | READ_CONTROL; files additionally FILE_READ_DATA.
        access=0x80|0x20000|(0x1 if read else 0)
        handle=self.k.CreateFileW(path,access,0x1|0x2,None,3,0x02000000|0x00200000,None)
        if not handle or handle==C.c_void_p(-1).value:raise ObserverError('readonly_open_denied')
        return handle

    def close(self,handle):
        if not self.k.CloseHandle(handle):raise ObserverError('close_failed')
        return True

    def file_info(self,handle):
        info=BY_HANDLE_FILE_INFORMATION()
        if not self.k.GetFileInformationByHandle(handle,C.byref(info)):raise ObserverError('file_information_query_failed')
        size=self.k.GetFinalPathNameByHandleW(handle,None,0,0)
        if not size or size>32768:raise ObserverError('final_path_query_failed')
        buf=C.create_unicode_buffer(size+1)
        actual=self.k.GetFinalPathNameByHandleW(handle,buf,len(buf),0)
        if not actual or actual>=len(buf):raise ObserverError('final_path_query_failed')
        return {'volume':info.volume,'file_id':(info.index_high<<32)|info.index_low,
            'attributes':info.attributes,'links':info.links,'size':(info.size_high<<32)|info.size_low,
            'modified':(info.write.dwHighDateTime<<32)|info.write.dwLowDateTime,'final_path':buf.value}

    def hash_file(self,handle,size):
        if not self.k.SetFilePointerEx(handle,0,None,0):raise ObserverError('readonly_seek_failed')
        digest=hashlib.sha256();remaining=size
        while remaining:
            length=min(1024*1024,remaining);buf=C.create_string_buffer(length);count=W.DWORD()
            if not self.k.ReadFile(handle,buf,length,C.byref(count),None) or not 0<count.value<=length:
                raise ObserverError('readonly_file_incomplete')
            digest.update(buf.raw[:count.value]);remaining-=count.value
        extra=C.create_string_buffer(1);count=W.DWORD()
        if not self.k.ReadFile(handle,extra,1,C.byref(count),None) or count.value:raise ObserverError('readonly_size_changed')
        return digest.hexdigest()

    def path_exists(self,path):
        value=self.k.GetFileAttributesW(path)
        if value==0xFFFFFFFF:
            if C.get_last_error() in (2,3):return False
            raise ObserverError('existence_query_denied_or_unknown')
        return True

    def enumerate_tree(self,directory):
        # Hold every directory before its name is traversed. A path-only scandir
        # recursion would otherwise race a child directory replaced by a junction.
        names=[];stack=[('',directory)];lease=ObservationLease(self)
        try:
            while stack:
                prefix,current=stack.pop()
                handle=lease._open(current)
                ev=lease._evidence(current,handle)
                if ev.kind!='directory':raise ObserverError('inventory_directory_type')
                with os.scandir(current) as listing:
                    for item in listing:
                        relative=prefix+item.name
                        if item.is_symlink() or getattr(item.stat(follow_symlinks=False),'st_file_attributes',0)&REPARSE:
                            raise ObserverError('inventory_reparse_point')
                        if item.is_dir(follow_symlinks=False):
                            names.append(relative+'/');stack.append((relative+'/',item.path))
                        elif item.is_file(follow_symlinks=False):names.append(relative)
                        else:raise ObserverError('inventory_special_file')
                        if len(names)>MAX_FILES*2:raise ObserverError('inventory_capacity')
            lease.close()
            return tuple(names)
        except BaseException as original:
            try:lease.close()
            except BaseException:
                original.observation_cleanup=lease
                original.add_note('Readonly traversal cleanup incomplete; explicit host retry required')
            raise

    def _token(self,process):
        token=W.HANDLE()
        if not self.a.OpenProcessToken(process,0x8,C.byref(token)):raise ObserverError('token_query_denied')
        return token

    def _info(self,token,kind):
        needed=W.DWORD()
        self.a.GetTokenInformation(token,kind,None,0,C.byref(needed))
        if not 0<needed.value<=65536:raise ObserverError('token_information_size')
        buf=C.create_string_buffer(needed.value)
        if not self.a.GetTokenInformation(token,kind,buf,len(buf),C.byref(needed)):raise ObserverError('token_information_query_failed')
        return buf

    def _sid(self,pointer):
        text=W.LPWSTR()
        if not pointer or not self.a.ConvertSidToStringSidW(pointer,C.byref(text)):raise ObserverError('sid_query_failed')
        try:return text.value
        finally:self.k.LocalFree(C.cast(text,C.c_void_p))

    def current_integrity(self):
        token=self._token(self.k.GetCurrentProcess())
        owner=_NativeObservationCleanup(self,token)
        self._integrity_cleanup=owner
        try:
            buf=self._info(token,25);label=C.cast(buf,C.POINTER(SID_AND_ATTRIBUTES)).contents
            sid=self._sid(label.sid)
            try:rid=int(sid.rsplit('-',1)[1])
            except (ValueError,AttributeError) as error:raise ObserverError('integrity_sid_invalid') from error
            return sid,rid
        finally:
            try:owner.close()
            except BaseException as error:
                error.observation_cleanup=owner
                raise

    def process_identity(self,process):
        pid=self.k.GetProcessId(process)
        if not pid:raise ObserverError('process_id_query_failed')
        token=self._token(process)
        try:
            app=self._info(token,29)
            is_app=C.cast(app,C.POINTER(W.DWORD)).contents.value==1
            sid_buf=self._info(token,31);sid_pointer=C.cast(sid_buf,C.POINTER(C.c_void_p)).contents.value
            sid=self._sid(sid_pointer) if sid_pointer else None
            capabilities=self._info(token,30)
            count=C.cast(capabilities,C.POINTER(W.DWORD)).contents.value
            # Strict policy permits none; do not parse unknown capabilities as an empty list.
            if count:raise ObserverError('unexpected_token_capabilities')
            return {'pid':pid,'is_appcontainer':is_app,'sid':sid,'capabilities':()}
        finally:self.close(token)

    def has_loopback_exemption(self,sid):
        # Import lazily: the provider shares ObserverError, never native mutation.
        # Keep the provider for this observer's lifetime so an unknown result or
        # failed HeapFree retains its owner and cannot later become fake absence.
        from app.plugin_network_isolation import LoopbackExemptionProvider
        if getattr(self,'_loopback_provider',None) is None:
            self._loopback_provider=LoopbackExemptionProvider()
        return self._loopback_provider.has_loopback_exemption(sid)
