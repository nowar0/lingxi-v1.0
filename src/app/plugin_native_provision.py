"""Native provisioning bridge, never an automatic provisioner or an OS test.

Only host-held observation leases and an explicitly confirmed creation callback can
supply a prepared tree. ACL writes use NativeAclAdapter's real DACL-only path.
Profile existence/ownership needs an explicit documented read-only observer: SID
 derivation alone is NOT existence evidence. Import/construction make no native I/O.
Tests inject complete fake Win32 backends; no production object is tested by them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import ntpath
import os
import ctypes as C
from ctypes import wintypes as W
import re
import threading

from app.plugin_sandbox_policy import LaunchPlan, canonical_local_path, plan_fingerprint
from app.plugin_sandbox_acl import NativeAclAdapter, PathIdentity, MASKS, PUBLIC_PACKAGE_SIDS, WRITE_BITS, plan_grant
from app.plugin_windows_observer import WindowsObserver, ObservationLease, NativeObserverApi, _ancestors
from app.plugin_windows_launcher import NativeWindowsApi
from app.plugin_sandbox_provision import (_validate_plan, _validate_deferred_plan, _child_access_paths,
    NativeReadOnlyAclInspector, DeferredChildAccessRequest, provision_plan_binding,
    validate_deferred_child_access_request)
from app.plugin_token_access import ProbeTarget, MAX_TARGETS

MAX_OBJECTS = 50000
MAX_PREPARED_BYTES = 256 * 1024**2
MAX_SECURITY_BYTES = 16 * 1024**2
_NONCE = re.compile(r'^[0-9a-f]{32,128}$')
_RUN = re.compile(r'^sandbox-lab-[0-9a-f]{32}$')


class NativeProvisionError(ValueError):
    """Fixed host-safe error codes; never serializes an OS exception body."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _inside(path, root):
    return path.casefold() == root.casefold() or path.casefold().startswith(root.casefold()+'\\')


def _identity(evidence):
    # ACL changes and intentional scratch creation must not invalidate stable identity.
    return _hash({'path':evidence.path.casefold(),'volume':evidence.volume,'file_id':evidence.file_id,
                  'kind':evidence.kind,'links':evidence.links,'attributes':evidence.attributes})


def _plan(plan, parent):
    if type(plan) is not LaunchPlan or plan.fingerprint != plan_fingerprint(plan):
        raise NativeProvisionError('immutable_plan_required')
    canonical_local_path(parent)
    if sum(entry.size for entry in (*plan.runtime_files,*plan.code_files))>MAX_PREPARED_BYTES:
        raise NativeProvisionError('prepared_payload_byte_limit')
    if ntpath.dirname(plan.paths.root).casefold()!=parent.casefold() or not _RUN.fullmatch(ntpath.basename(plan.paths.root)):
        raise NativeProvisionError('exact_fresh_uuid_run_root_required')
    # Reuse the complete policy/root/minimum-ACL contract without inventing a profile.
    _validate_plan(plan,parent,'Lingxi.Provision.validation0001')


def _objects(lease, root):
    if type(lease) is not ObservationLease or lease.closed:
        raise NativeProvisionError('owned_live_observation_lease_required')
    return {e.path:(h,e) for h,e in lease._files if _inside(e.path,root)}


def _expected_names(plan):
    names=set()
    for directory,entries in ((plan.paths.runtime,plan.runtime_files),(plan.paths.code,plan.code_files),(plan.paths.scratch,())):
        paths=[directory]+[directory+'\\'+entry.relative_path.replace('/','\\') for entry in entries]
        for path in paths:
            relative=path[len(plan.paths.root)+1:].replace('\\','/')
            pieces=relative.split('/')
            names.update('/'.join(pieces[:i])+'/' for i in range(1,len(pieces)))
            names.add(relative+('/' if path==directory else ''))
    return tuple(sorted(names))


def _held_extra(lease, path):
    if not any(e.path==path for _,e in lease._files):
        handle=lease._open(path)
        evidence=lease._evidence(path,handle)
        if evidence.kind!='directory':raise NativeProvisionError('prepared_directory_required')
        lease._files.append((handle,evidence))


@dataclass(frozen=True)
class HostRootCreationReceipt:
    """Host-only result of an absent -> exclusive creation -> held identity check."""
    approved_parent: str
    root: str
    creation_nonce: str
    identity_fingerprint: str
    creation_fingerprint: str


def root_creation_plan(*, approved_parent, root, creation_nonce):
    canonical_local_path(approved_parent);canonical_local_path(root)
    if (ntpath.dirname(root).casefold()!=approved_parent.casefold()
            or not _RUN.fullmatch(ntpath.basename(root)) or not isinstance(creation_nonce,str)
            or not _NONCE.fullmatch(creation_nonce)):
        raise NativeProvisionError('exact_root_creation_plan_required')
    body={'approved_parent':approved_parent,'root':root,'creation_nonce':creation_nonce,
          'operation':'exclusive_new_run_root_only','parent_acl_modified':False}
    return {**body,'fingerprint':_hash(body)}


def create_run_root_once(plan, *, observer, root_creator, user_confirmed, fingerprint):
    """No default mkdir: invoke only the exact host controller callback after approval.

    The caller separately populates runtime/code afterwards. This is NOT permission
    to create/authorize the parent. No existing root is adopted, even when empty.
    """
    if (type(plan) is not dict or set(plan)!={'approved_parent','root','creation_nonce','operation','parent_acl_modified','fingerprint'}
            or root_creation_plan(approved_parent=plan['approved_parent'],root=plan['root'],creation_nonce=plan['creation_nonce'])!=plan
            or user_confirmed is not True or fingerprint!=plan['fingerprint']
            or type(observer) is not WindowsObserver or observer.root!=plan['root'] or not callable(root_creator)):
        raise NativeProvisionError('root_creation_confirmation_required')
    if not observer.current_integrity().verified:raise NativeProvisionError('normal_medium_observer_required')
    if observer.api.path_exists(plan['root']):raise NativeProvisionError('preexisting_run_root_not_adopted')
    root_creator(plan['root'],plan['creation_nonce'])
    lease=ObservationLease(observer.api)
    try:
        for path in (*_ancestors(plan['root']),plan['root']):
            handle=lease._open(path);evidence=lease._evidence(path,handle)
            if evidence.kind!='directory':raise NativeProvisionError('created_root_not_directory')
            lease._files.append((handle,evidence))
        root_evidence=_objects(lease,plan['root'])[plan['root']][1]
        if observer.api.enumerate_tree(plan['root'])!=():raise NativeProvisionError('created_root_not_empty')
        receipt=HostRootCreationReceipt(plan['approved_parent'],plan['root'],plan['creation_nonce'],
            _identity(root_evidence),plan['fingerprint'])
        return receipt
    finally:
        try:lease.close()
        except BaseException as error:
            error.observation_cleanup=lease
            raise


@dataclass(frozen=True)
class HostPreparedTree:
    approved_parent: str
    plan: LaunchPlan
    creation_nonce: str
    scratch_identity: str
    root_identity: str
    lease: ObservationLease
    creation_fingerprint: str


class MissingScratchWitness:
    """Host-owned single attempt. A Python instance is not independently OS evidence."""
    def __init__(self, parent, plan, nonce, observer, lease, root_receipt):
        self.parent,self.plan,self.nonce,self.observer,self.lease=parent,plan,nonce,observer,lease
        self.root_receipt=root_receipt
        self.fingerprint=_hash({'parent':parent,'plan':plan.fingerprint,'nonce':nonce,
            'root_identity':_identity(_objects(lease,plan.paths.root)[plan.paths.root][1]),
            'root_creation':root_receipt.creation_fingerprint,'scratch_missing':plan.paths.scratch})
        self._attempted=False;self._lock=threading.Lock()

    def prepare(self, scratch_creator, *, user_confirmed, fingerprint):
        """Call a trusted controller's create-ONLY callback, then observe the new object.

        No default mkdir exists. The callback must create exactly this missing path;
        collision/failure consumes this witness. No automatic cleanup or retry.
        """
        if user_confirmed is not True or fingerprint!=self.fingerprint or not callable(scratch_creator):
            raise NativeProvisionError('exact_scratch_creation_confirmation_required')
        with self._lock:
            if self._attempted:raise NativeProvisionError('scratch_creation_already_attempted')
            self._attempted=True
        self.lease.revalidate()
        before={p:_identity(e) for p,(_,e) in _objects(self.lease,self.plan.paths.root).items()}
        scratch_creator(self.plan.paths.scratch,self.nonce)
        fresh=None
        try:
            fresh=self.observer.inspect(inventories={self.plan.paths.runtime:self.plan.runtime_files,self.plan.paths.code:self.plan.code_files})
            for path in (*_ancestors(self.plan.paths.scratch),self.plan.paths.scratch):
                if _inside(path,self.plan.paths.root):_held_extra(fresh,path)
            values=_objects(fresh,self.plan.paths.root)
            if any(path not in values or _identity(values[path][1])!=identity for path,identity in before.items()):
                raise NativeProvisionError('prepared_tree_identity_changed')
            if self.observer.api.enumerate_tree(self.plan.paths.scratch)!=():
                raise NativeProvisionError('fresh_scratch_not_empty')
            names=self.observer.api.enumerate_tree(self.plan.paths.root)
            if type(names) is not tuple or tuple(sorted(names))!=_expected_names(self.plan) or len(names)>MAX_OBJECTS:
                raise NativeProvisionError('prepared_root_extra_or_missing_object')
            fresh.revalidate()
            return HostPreparedTree(self.parent,self.plan,self.nonce,_identity(values[self.plan.paths.scratch][1]),
                _identity(values[self.plan.paths.root][1]),fresh,self.fingerprint)
        except BaseException as error:
            if fresh is not None:
                try:fresh.close()
                except BaseException:error.observation_cleanup=fresh
            raise


def capture_missing_scratch(plan, *, approved_parent, creation_nonce, observer, observation_approved, root_creation_receipt):
    """Read-only plan-time witness. Never creates a run root, scratch, profile or ACL."""
    _plan(plan,approved_parent)
    if (observation_approved is not True or type(observer) is not WindowsObserver
            or observer.root!=plan.paths.root or not isinstance(creation_nonce,str) or not _NONCE.fullmatch(creation_nonce)):
        raise NativeProvisionError('explicit_host_observer_and_nonce_required')
    lease=observer.inspect(inventories={plan.paths.runtime:plan.runtime_files,plan.paths.code:plan.code_files},missing_scratch=plan.paths.scratch)
    receipt=root_creation_receipt
    if (type(receipt) is not HostRootCreationReceipt or receipt.approved_parent!=approved_parent
            or receipt.root!=plan.paths.root or receipt.creation_nonce!=creation_nonce
            or receipt.identity_fingerprint!=_identity(_objects(lease,plan.paths.root)[plan.paths.root][1])):
        lease.close()
        raise NativeProvisionError('new_root_creation_receipt_required')
    return MissingScratchWitness(approved_parent,plan,creation_nonce,observer,lease,receipt)


def _dacl(snapshot):
    full=snapshot['sddl']
    match=re.search(r'D:',full)
    if match is None:raise NativeProvisionError('full_descriptor_has_no_dacl')
    tail=full[match.start():]
    end=re.search(r'[OGS]:',tail[2:])
    return tail if end is None else tail[:end.start()+2]


def _without_sid(snapshot, sid):
    return tuple((a['type'],a['flags'],a['mask'],a['sid']) for a in snapshot['aces'] if a['sid']!=sid)


class NativeProfileFolderApi:
    """Lazy read-only SDK binding. Never creates, enumerates or adopts profiles.

    userenv.h (SDK 10.0.26100): GetAppContainerFolderPath returns a PWSTR
    owned by CoTaskMemFree, not LocalFree or FreeSid.
    """
    def __init__(self):
        if os.name != 'nt':raise NativeProvisionError('windows_profile_query_required')
        self.userenv=C.WinDLL('userenv.dll',use_last_error=True)
        self.ole=C.WinDLL('ole32.dll',use_last_error=True)
        self.query=self.userenv.GetAppContainerFolderPath
        self.query.argtypes=[W.LPCWSTR,C.POINTER(W.LPWSTR)];self.query.restype=C.c_long
        self.free=self.ole.CoTaskMemFree
        self.free.argtypes=[C.c_void_p];self.free.restype=None

    def folder_path(self,sid):
        if not isinstance(sid,str) or not re.fullmatch(r'S-1-15-2-(?:[0-9]+-)*[0-9]+',sid):
            raise NativeProvisionError('specific_profile_sid_required')
        output=W.LPWSTR()
        try:
            status=self.query(sid,C.byref(output))
            if status != 0 or not output:raise NativeProvisionError('profile_folder_query_failed')
            address=C.cast(output,C.c_void_p).value
            for index in range(30001):
                character=C.wstring_at(address+index*C.sizeof(C.c_wchar),1)
                if character=='\x00':break
            else:raise NativeProvisionError('profile_folder_path_invalid')
            value=C.wstring_at(address,index)
            if not value:raise NativeProvisionError('profile_folder_path_invalid')
            return value
        finally:
            if output:self.free(C.cast(output,C.c_void_p))


class OwnedProfileFolderObserver:
    """Observe one exact SID folder only AFTER a create-only receipt exists.

    The SID->path query and readonly no-delete-shared directory handles do not
    establish ownership: the adapter's successful creation receipt does. All
    ancestors are inspected only; no directory contents or registry are scanned.
    Explicit approval covers this Windows-managed path outside the run root.
    """
    def __init__(self, sid, *, observation_approved=False, folder_api=None, path_api=None):
        if not isinstance(sid,str) or not re.fullmatch(r'S-1-15-2-(?:[0-9]+-)*[0-9]+',sid):
            raise NativeProvisionError('specific_profile_sid_required')
        if type(observation_approved) is not bool:
            raise NativeProvisionError('explicit_profile_folder_observation_approval_required')
        self.sid,self.observation_approved=sid,observation_approved
        self._folder_api,self._path_api=folder_api,path_api

    def __call__(self,name):
        if self.observation_approved is not True:
            raise NativeProvisionError('profile_folder_observation_approval_required')
        if not isinstance(name,str) or not re.fullmatch(r'Lingxi\.[A-Za-z0-9_.-]{8,100}',name):
            raise NativeProvisionError('host_profile_name_required')
        if self._folder_api is None:self._folder_api=NativeProfileFolderApi()
        path=self._folder_api.folder_path(self.sid)
        try:canonical_local_path(path)
        except (TypeError,ValueError):raise NativeProvisionError('profile_folder_path_invalid') from None
        if self._path_api is None:self._path_api=NativeObserverApi()
        lease=ObservationLease(self._path_api)
        try:
            held=[]
            for directory in (*_ancestors(path),path):
                handle=lease._open(directory);evidence=lease._evidence(directory,handle)
                if evidence.kind!='directory':raise NativeProvisionError('profile_folder_not_directory')
                lease._files.append((handle,evidence));held.append(_identity(evidence))
            lease.revalidate()
            return {'source':'host_os_query','profile_name':name,'sid':self.sid,'exists':True,
                    'folder_path':path,'identity_fingerprint':_hash({'sid':self.sid,'path':path,'ancestors':held})}
        finally:
            try:lease.close()
            except BaseException as error:
                error.observation_cleanup=lease
                raise


class NativeProvisionAdapter:
    """Bridge for ProvisionLedger; create-only ownership plus exact folder identity.

    The default profile observer uses the documented readonly folder API, but needs
    separate explicit approval for that Windows-managed path. An injected observer
    is a trusted host capability and must return path/identity, not mere existence.
    By default AppContainer ancestor traversal needs a trusted access observer: a
    Medium host open cannot prove child SID traversal. The explicit deferred mode
    retains host identities/baseline ONLY and always reports child access pending;
    it requires the ledger's separate opt-in and a later real suspended-child probe.
    Neither provision ready nor readonly_verify grants launch permission. No parent
    ACL is ever added here. Native profile creation occurs only after exact commit.
    Security descriptors are retained in memory for all paths, not truncated into
    D:-only proof; only the ledger-facing view uses the core's D:-only schema.
    """
    def __init__(self, prepared, *, creation_nonce, observation_approved,
                 acl_api=None, native_profile_api=None, profile_observer=None, readonly_acl_inspector=None,
                 ancestor_access_observer=None, profile_folder_observation_approved=False,
                 defer_child_access=False):
        if (type(prepared) is not HostPreparedTree or observation_approved is not True
                or creation_nonce!=prepared.creation_nonce or not _NONCE.fullmatch(creation_nonce)):
            raise NativeProvisionError('exact_host_prepared_tree_required')
        _plan(prepared.plan,prepared.approved_parent)
        if type(defer_child_access) is not bool:
            raise NativeProvisionError('explicit_boolean_deferred_child_access_opt_in_required')
        if defer_child_access:
            _validate_deferred_plan(prepared.plan)
            if ancestor_access_observer is not None:
                raise NativeProvisionError('deferred_and_preverified_access_modes_are_exclusive')
        self.defer_child_access=defer_child_access
        if profile_observer is not None and not callable(profile_observer):
            raise NativeProvisionError('profile_observer_must_be_host_callable')
        self.prepared=prepared;self.plan=prepared.plan;self.lease=prepared.lease
        self._acl=NativeAclAdapter(check_path=self.check_path,api=acl_api)
        self._readonly_acl=(readonly_acl_inspector if readonly_acl_inspector is not None else
                            NativeReadOnlyAclInspector(approved_root=self.plan.paths.root) if acl_api is None else None)
        if ancestor_access_observer is not None and not callable(ancestor_access_observer):
            raise NativeProvisionError('ancestor_access_observer_must_be_host_callable')
        self._ancestor_access_observer=ancestor_access_observer
        if type(profile_folder_observation_approved) is not bool:
            raise NativeProvisionError('explicit_profile_folder_observation_approval_required')
        self._native_profile=native_profile_api
        self._profile_observer=profile_observer if profile_observer is not None else OwnedProfileFolderObserver(
            self.plan.appcontainer_sid,observation_approved=profile_folder_observation_approved)
        self._profile_folder_observation_approved=profile_folder_observation_approved or profile_observer is not None
        self._baseline=None;self._created={};self._attempted_profiles=set();self._applied={}
        self._write_authority=None
        self._lock=threading.RLock()

    def authorize_writes(self, *, intent, fingerprint, user_confirmed):
        """Explicit host gate matching the exact ProvisionLedger preview.

        This does not consume/commit the ledger; it only prevents direct adapter
        methods from silently treating read-only observation as write permission.
        The ledger remains responsible for one-use token/TTL/durable stage CAS.
        """
        if (user_confirmed is not True or type(intent) is not dict or _hash(intent)!=fingerprint
                or intent.get('root')!=self.plan.paths.root or intent.get('sid')!=self.plan.appcontainer_sid
                or intent.get('policy_fingerprint')!=self.plan.fingerprint
                or intent.get('approved_parent')!=self.prepared.approved_parent):
            raise NativeProvisionError('exact_provision_intent_confirmation_required')
        expected=[{'path':g.path,'sid':g.sid,'rights':g.rights,'scope':g.scope} for g in self.plan.acl_grants]
        if intent.get('grants')!=expected or self._write_authority is not None:
            raise NativeProvisionError('provision_write_authority_already_set_or_invalid')
        if self.defer_child_access:
            if (intent.get('child_access_mode')!='deferred_owned_suspended_child'
                    or intent.get('provision_binding')!=provision_plan_binding(self.plan)
                    or intent.get('root_observation')!=self.observe(self.plan)):
                raise NativeProvisionError('exact_pending_child_access_intent_required')
        elif 'child_access_mode' in intent or 'provision_binding' in intent:
            raise NativeProvisionError('deferred_child_access_not_enabled')
        self._write_authority={'profile_name':intent.get('profile_name'),'fingerprint':fingerprint}

    def check_path(self,path):
        if not _inside(path,self.plan.paths.root):raise NativeProvisionError('outside_prepared_run_root')
        self.lease.revalidate()
        value=_objects(self.lease,self.plan.paths.root).get(path)
        if value is None:raise NativeProvisionError('path_not_held_by_preparation')
        evidence=value[1]
        return PathIdentity(path,_identity(evidence),'host_os_query',True,True,False,False,
                            evidence.kind,evidence.links if evidence.kind=='file' else None)

    def _validate_tree(self):
        self.lease.revalidate()
        values=_objects(self.lease,self.plan.paths.root)
        if (_identity(values[self.plan.paths.root][1])!=self.prepared.root_identity
                or _identity(values[self.plan.paths.scratch][1])!=self.prepared.scratch_identity):
            raise NativeProvisionError('prepared_identity_changed')
        names=self.lease.api.enumerate_tree(self.plan.paths.root)
        if type(names) is not tuple or tuple(sorted(names))!=_expected_names(self.plan) or len(names)>MAX_OBJECTS:
            raise NativeProvisionError('prepared_root_extra_or_missing_object')
        if self.lease.api.enumerate_tree(self.plan.paths.scratch)!=():
            raise NativeProvisionError('scratch_changed_before_execution')
        return values

    def _snapshots(self):
        values=self._validate_tree()
        result={};total=0
        for path in sorted(values):
            snapshot=self._acl.inspect(path)
            total+=len(json.dumps(snapshot,ensure_ascii=False,allow_nan=False).encode('utf-8'))
            if total>MAX_SECURITY_BYTES:raise NativeProvisionError('security_snapshot_capacity')
            result[path]=snapshot
        return result

    def _match_plan(self,plan):
        if not self.defer_child_access:
            if plan!=self.plan:raise NativeProvisionError('prepared_plan_mismatch')
        else:
            _plan(plan,self.prepared.approved_parent)
            _validate_deferred_plan(plan)
            if provision_plan_binding(plan)!=provision_plan_binding(self.plan):
                raise NativeProvisionError('prepared_semantic_plan_binding_mismatch')

    def child_access_request(self,plan):
        """Expose held identities ONLY, not an access result or a child-token factory.

        Call observe first to capture the pre-profile/pre-ACL security baseline.
        The trusted launcher must keep this lease open through its actual suspended
        child probe and pre-resume revalidation. This method never runs that probe.
        """
        with self._lock:
            if not self.defer_child_access:raise NativeProvisionError('deferred_child_access_not_enabled')
            self._match_plan(plan)
            if self._baseline is None:raise NativeProvisionError('preprovision_acl_baseline_required')
            self._validate_tree()
            held={e.path:e for _,e in self.lease._files}
            targets=[]
            for path,kind,mask in _child_access_paths(plan):
                evidence=held.get(path)
                if evidence is None or evidence.kind!=kind:
                    raise NativeProvisionError('complete_held_child_access_targets_required')
                targets.append(ProbeTarget(path,mask,kind,evidence.volume,evidence.file_id))
            if not 1<=len(targets)<=MAX_TARGETS:
                raise NativeProvisionError('child_access_target_capacity')
            ancestors=[asdict(item) for item in targets if not _inside(item.path,plan.paths.root)]
            request=DeferredChildAccessRequest(self.plan.fingerprint,provision_plan_binding(plan),
                plan.paths.root,plan.appcontainer_sid,_hash(ancestors),_hash(self._baseline),tuple(targets))
            validate_deferred_child_access_request(request.snapshot(),plan)
            return request

    def observe(self,plan):
        with self._lock:
            self._match_plan(plan)
            if not self.defer_child_access:
                # Compatibility is explicit trusted pre-verified observation ONLY.
                # A host Medium open (or a pending request) must never set this True.
                if self._ancestor_access_observer is None:
                    raise NativeProvisionError('appcontainer_ancestor_traversal_observer_unsupported')
                ancestor_proof=self._ancestor_access_observer(plan.paths.root,plan.appcontainer_sid)
                expected_ancestors=tuple(p for p in _ancestors(plan.paths.root) if not _inside(p,plan.paths.root))
                fields={'source','root','sid','paths','access_mask','traversal_verified','parent_acl_modified','evidence_fingerprint'}
                if (type(ancestor_proof) is not dict or set(ancestor_proof)!=fields
                        or ancestor_proof['source']!='host_os_query' or ancestor_proof['root']!=plan.paths.root
                        or ancestor_proof['sid']!=plan.appcontainer_sid or ancestor_proof['paths']!=expected_ancestors
                        or type(ancestor_proof['access_mask']) is not int or ancestor_proof['access_mask']!=0x20
                        or ancestor_proof['traversal_verified'] is not True or ancestor_proof['parent_acl_modified'] is not False
                        or not isinstance(ancestor_proof['evidence_fingerprint'],str)
                        or not re.fullmatch(r'[0-9a-f]{64}',ancestor_proof['evidence_fingerprint'])):
                    raise NativeProvisionError('appcontainer_ancestor_traversal_unverified')
            values=self._validate_tree()
            if self._baseline is None:
                baseline=self._snapshots()
                for snapshot in baseline.values():
                    if any(a['sid']==self.plan.appcontainer_sid for a in snapshot['aces']):
                        raise NativeProvisionError('fresh_sid_already_present_in_descendant')
                    if any(a['type']==0 and a['sid'] in PUBLIC_PACKAGE_SIDS and a['mask'] & WRITE_BITS for a in snapshot['aces']):
                        raise NativeProvisionError('public_package_write_access')
                self._baseline=baseline
            if self.defer_child_access:
                pending=self.child_access_request(plan).snapshot()
                return {'source':'host_os_query','root':plan.paths.root,'fresh_owned_root':True,
                        'inventory_verified':True,'ancestor_traversal_verified':False,'child_access':pending,
                        'identity_fingerprint':_hash({'plan':provision_plan_binding(plan),
                            'nonce':self.prepared.creation_nonce,'creation':self.prepared.creation_fingerprint,
                            'pending_child_access':pending['fingerprint'],
                            'objects':{p:_identity(e) for p,(_,e) in values.items()}})}
            return {'source':'host_os_query','root':plan.paths.root,'fresh_owned_root':True,
                    'inventory_verified':True,'ancestor_traversal_verified':True,
                    'identity_fingerprint':_hash({'plan':plan.fingerprint,'nonce':self.prepared.creation_nonce,
                        'creation':self.prepared.creation_fingerprint,'ancestor_access':ancestor_proof['evidence_fingerprint'],'objects':{p:_identity(e) for p,(_,e) in values.items()}})}

    def _profile(self,name):
        if self._profile_observer is None:raise NativeProvisionError('profile_existence_observer_unsupported')
        value=self._profile_observer(name)
        fields={'source','profile_name','sid','exists','folder_path','identity_fingerprint'}
        if type(value) is not dict or set(value)!=fields or value['source']!='host_os_query' or value['profile_name']!=name or type(value['exists']) is not bool:
            raise NativeProvisionError('profile_observation_unverified')
        if value['exists'] and value['sid']!=self.plan.appcontainer_sid:
            raise NativeProvisionError('profile_sid_mismatch')
        if not value['exists']:
            raise NativeProvisionError('owned_profile_disappeared')
        try:canonical_local_path(value['folder_path'])
        except (TypeError,ValueError):raise NativeProvisionError('profile_folder_path_invalid') from None
        if not isinstance(value['identity_fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['identity_fingerprint']):
            raise NativeProvisionError('profile_folder_identity_unverified')
        return value

    def create_profile(self,name,sid):
        with self._lock:
            if self._write_authority is None or self._write_authority['profile_name']!=name:
                raise NativeProvisionError('provision_write_authority_required')
            if sid!=self.plan.appcontainer_sid or not re.fullmatch(r'Lingxi\.[A-Za-z0-9_.-]{8,100}',name):
                raise NativeProvisionError('profile_intent_mismatch')
            if name in self._attempted_profiles:raise NativeProvisionError('profile_attempt_consumed')
            if not self._profile_folder_observation_approved or self._profile_observer is None:
                raise NativeProvisionError('profile_folder_observation_approval_required')
            self._attempted_profiles.add(name)
            self._validate_tree()
            if self._native_profile is None:self._native_profile=NativeWindowsApi()
            # CreateAppContainerProfile is the authoritative collision gate. A
            # pre-query can neither prove ownership nor safely close its race.
            actual=self._native_profile.create_profile(name,name,sid)
            # Record every reported successful creation before further checks. A
            # mismatched result is retained for review, never adopted for ACL use.
            self._created[name]={'sid':actual,'folder_observation':None,'created_new':True}
            if actual!=sid:raise NativeProvisionError('profile_creation_sid_mismatch')
            # Ownership is retained BEFORE post-create observation. Failure leaves
            # this creation reachable, consumed, and in needs_review.
            observed=self._profile(name)
            self._created[name]['folder_observation']=dict(observed)
            return {'profile_name':name,'sid':sid,'created_new':True}

    def inspect_profile(self,name):
        if name not in self._created:raise NativeProvisionError('profile_not_owned_by_this_creation')
        owned=self._created[name]
        if owned['folder_observation'] is None:
            raise NativeProvisionError('created_profile_observation_incomplete_needs_review')
        value=self._profile(name)
        if value!=owned['folder_observation']:
            raise NativeProvisionError('owned_profile_folder_identity_changed')
        return {key:value[key] for key in ('source','profile_name','sid')}

    def _grant(self,path,sid):
        if sid!=self.plan.appcontainer_sid:raise NativeProvisionError('foreign_sid')
        for grant in self.plan.acl_grants:
            if grant.path==path:return grant
        raise NativeProvisionError('path_not_in_exact_grant_plan')

    def inspect_acl(self,path,sid):
        grant=self._grant(path,sid);identity=self.check_path(path);snapshot=self._acl.inspect(path)
        target=[{'type':a['type'],'flags':a['flags'],'mask':a['mask']} for a in snapshot['aces'] if a['sid']==sid]
        broader=any(a['type']==1 or a['mask'] & ~MASKS[grant.rights] or a['flags'] & ~(3 if grant.scope=='this-directory-and-descendants' else 0)
                    for a in snapshot['aces'] if a['sid']==sid)
        broader=bool(broader or any(a['type']==0 and a['sid'] in PUBLIC_PACKAGE_SIDS and a['mask'] & WRITE_BITS for a in snapshot['aces']))
        dacl=_dacl(snapshot)
        if self._readonly_acl is not None:
            independent=self._readonly_acl.inspect_acl(path,sid)
            if (type(independent) is not dict or independent.get('source')!='host_os_query'
                    or independent.get('path')!=path or independent.get('sid')!=sid
                    or independent.get('sddl')!=dacl or independent.get('target_aces')!=target
                    or independent.get('broader_access') is not False):
                raise NativeProvisionError('independent_dacl_observation_mismatch')
        return {'source':'host_os_query','path':path,'sid':sid,'sddl':dacl,
                'identity_fingerprint':identity.identity_fingerprint,'target_aces':target,'broader_access':broader}

    def apply_acl(self,grant,*,expected_before_fingerprint):
        with self._lock:
            if (grant not in self.plan.acl_grants or self._baseline is None or not self._created
                    or any(item['folder_observation'] is None for item in self._created.values())):
                raise NativeProvisionError('profile_and_baseline_required_before_acl')
            for name in self._created:self.inspect_profile(name)
            before=self.inspect_acl(grant.path,grant.sid)
            if _hash(before)!=expected_before_fingerprint:raise NativeProvisionError('acl_before_changed')
            if grant.path in self._applied:raise NativeProvisionError('acl_already_attempted')
            self._applied[grant.path]=False
            snapshot=self._acl.inspect(grant.path)
            native_plan=plan_grant(self.plan.paths.root,grant.path,grant.sid,grant.rights,grant.scope,
                                   snapshot['sddl'],check_path=self.check_path)
            result=self._acl.apply(native_plan,user_confirmed=True,fingerprint=native_plan.fingerprint)
            self._verify_all(grant)
            self._applied[grant.path]=True
            return result

    def _verify_all(self,grant):
        current=self._snapshots()
        if self._baseline is None or set(current)!=set(self._baseline):raise NativeProvisionError('acl_baseline_missing_or_tree_changed')
        sid=grant.sid
        for path,after in current.items():
            before=self._baseline[path]
            if any(before[k]!=after[k] for k in ('owner','group','label_sddl','dacl_present','dacl_protected')) or _without_sid(before,sid)!=_without_sid(after,sid):
                raise NativeProvisionError('non_target_security_changed')
            if any(a['sid']==sid for a in before['aces']):raise NativeProvisionError('baseline_target_sid_preexisting')
            if any(a['sid'] in PUBLIC_PACKAGE_SIDS and a['type']==0 and a['mask'] & WRITE_BITS for a in after['aces']):
                raise NativeProvisionError('public_package_write_access')
            targets=[a for a in after['aces'] if a['sid']==sid]
            applicable=next((g for g in self.plan.acl_grants if g.path==path),None)
            applied_paths={p for p,done in self._applied.items() if done}|{grant.path}
            expected=[]
            if applicable and applicable.path in applied_paths:
                expected=[{'type':0,'flags':3 if applicable.scope=='this-directory-and-descendants' else 0,'mask':MASKS[applicable.rights],'sid':sid}]
            else:
                ancestor=next((g for g in self.plan.acl_grants if g.path in applied_paths and g.scope=='this-directory-and-descendants' and path.casefold().startswith(g.path.casefold()+'\\')),None)
                if ancestor:
                    flags=19 if self.check_path(path).kind=='directory' else 16
                    expected=[{'type':0,'flags':flags,'mask':MASKS[ancestor.rights],'sid':sid}]
            if targets!=expected:raise NativeProvisionError('descendant_acl_not_exactly_verified')
        return current

    def verify_tree_acl(self,grant):
        with self._lock:
            if grant not in self.plan.acl_grants or self._applied.get(grant.path) is not True:
                raise NativeProvisionError('grant_not_completed')
            self._verify_all(grant)
            return {'source':'host_os_query','path':grant.path,'sid':grant.sid,
                    'all_descendants_verified':True,'non_target_entries_preserved':True,'broader_access':False}
