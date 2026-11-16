"""Explicit sandbox profile/ACL provisioning journal; import and preview never write.

A trusted, explicitly supplied native adapter is required for commit. This module
DOES NOT supply a production ACL writer: NativeReadOnlyAclInspector only inspects
DACLs, lazily. Missing profile, fresh-root or ACL implementation fails closed.
Tests use fake adapters, never real profile/ACL/token/process APIs. Before real use,
the user must approve the exact fresh run root, profile SID and per-path grants.
Before/after SDDL in the safety journal is an authorization record, NOT a source,
project or user-data backup. Failed/ambiguous operations remain needs_review; no
profile deletion, unknown adoption, recursive permission reset or auto-retry.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import copy
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from app.plugin_sandbox_policy import (AclGrant, LaunchPlan, canonical_local_path,
    inventory_fingerprint, plan_fingerprint, SandboxPolicy, semantic_plan_projection,
    validate_launch_plan)
from app.plugin_token_access import (OwnedChildReceipt, ProbeTarget, EXECUTE_OR_TRAVERSE,
    READ_ONLY_MASK, MAX_TARGETS, targets_fingerprint)
from app.plugin_windows_launcher import plan_provision

FORMAT = 'lingxi.sandbox-provision-ledger.v1'
MAX_BYTES = 2 * 1024**2
MAX_RECORDS = 64
MAX_PENDING = 32
# User explicitly allowed deleting files only within the per-call scratch subtree.
MASKS = {'RX': 0x1200A9, 'RW': 0x13019F, 'X': 0x20}
_NAME = re.compile(r'^Lingxi\.[A-Za-z0-9_.-]{8,100}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')


class ProvisionError(ValueError):
    pass


def _safe_provision_failure(error):
    """Bounded fixed metadata only; no paths, arbitrary exception text or secrets."""
    result=[];seen=set()
    while error is not None and id(error) not in seen and len(result)<6:
        seen.add(id(error));item={'type':type(error).__name__}
        code=getattr(error,'code',None)
        if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,99}',code):item['code']=code
        if type(getattr(error,'write_attempted',None)) is bool:item['write_attempted']=error.write_attempted
        native=getattr(error,'native_diagnostic',None)
        if type(native) is dict and set(native)=={'operation','win32_status','dacl_write_entered'}:
            if (native['operation'] in {'GetNamedSecurityInfoW','SetEntriesInAclW','SetNamedSecurityInfoW'}
                    and type(native['win32_status']) is int and 0<=native['win32_status']<=0xffffffff
                    and type(native['dacl_write_entered']) is bool):item['native']=dict(native)
        result.append(item);error=error.__cause__
    return result


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


CHILD_ACCESS_FORMAT = 'lingxi.deferred-child-access.v1'


def provision_plan_binding(plan):
    """Pure semantic binding; physical IPC numbers are NOT provisioning authority."""
    return _sha(semantic_plan_projection(plan))


def _child_access_paths(plan):
    # Every runtime/code payload and its complete ancestor chain, including the
    # drive root. Directory opens request traverse only; no parent ACL is granted.
    files = {directory + '\\' + entry.relative_path.replace('/', '\\')
             for directory, entries in ((plan.paths.runtime, plan.runtime_files),
                                        (plan.paths.code, plan.code_files)) for entry in entries}
    directories = {plan.paths.root, plan.paths.runtime, plan.paths.code}
    for path in (*files, *tuple(directories)):
        parent = ntpath.dirname(path)
        while parent and parent != path:
            directories.add(parent)
            path, parent = parent, ntpath.dirname(parent)
    return tuple((path, 'file' if path in files else 'directory',
                  READ_ONLY_MASK if path in files else EXECUTE_OR_TRAVERSE)
                 for path in sorted(files | directories))


@dataclass(frozen=True)
class DeferredChildAccessRequest:
    """Read-only probe INTENT, never an OS access attestation or resume permission.

    A frozen Python object, serialized snapshot, hash, or successful structural
    validation can all be synthesized. The launcher must retain its own suspended
    child, query its actual identity/token, call the actual TokenPathAccessVerifier,
    bind its result/cleanup to these exact targets and that child, and revalidate
    held paths immediately pre-resume. This module never issues child-access True.
    """
    origin_plan_fingerprint: str
    semantic_binding: str
    root: str
    sid: str
    ancestor_host_identity_fingerprint: str
    acl_baseline_fingerprint: str
    targets: tuple[ProbeTarget, ...]

    def snapshot(self):
        body = {'schema': CHILD_ACCESS_FORMAT, 'phase': 'pending_owned_suspended_child',
                'source': 'held_host_identity_only',
                'origin_plan_fingerprint': self.origin_plan_fingerprint,
                'semantic_binding': self.semantic_binding, 'root': self.root, 'sid': self.sid,
                'ancestor_host_identity_fingerprint': self.ancestor_host_identity_fingerprint,
                'acl_baseline_fingerprint': self.acl_baseline_fingerprint,
                'targets': [asdict(target) for target in self.targets],
                'ancestor_traversal_verified': False, 'child_access_verified': False,
                'launch_authorized': False}
        return {**body, 'fingerprint': _sha(body)}

    def probe_fingerprint(self, plan, child):
        """Bind an intent to the host's receipt; still NOT proof the child exists."""
        validate_deferred_child_access_request(self.snapshot(), plan)
        validate_launch_plan(plan, require_physical=True)
        if (type(child) is not OwnedChildReceipt or child.sandbox_sid != self.sid
                or child.policy_fingerprint != plan.fingerprint):
            raise ProvisionError('exact owned physical child binding required')
        return targets_fingerprint(child, self.targets)


def validate_deferred_child_access_request(snapshot, plan):
    """Pure exact shape/binding validation ONLY; arbitrary dicts cannot grant access.

    Returns typed targets for a later trusted host probe. It deliberately has no
    API accepting a caller-supplied result to upgrade pending to verified/allowed.
    """
    _validate_deferred_plan(plan)
    fields = {'schema', 'phase', 'source', 'origin_plan_fingerprint', 'semantic_binding',
              'root', 'sid', 'ancestor_host_identity_fingerprint', 'acl_baseline_fingerprint',
              'targets', 'ancestor_traversal_verified', 'child_access_verified',
              'launch_authorized', 'fingerprint'}
    if (type(snapshot) is not dict or set(snapshot) != fields
            or snapshot['schema'] != CHILD_ACCESS_FORMAT
            or snapshot['phase'] != 'pending_owned_suspended_child'
            or snapshot['source'] != 'held_host_identity_only'
            or snapshot['root'] != plan.paths.root or snapshot['sid'] != plan.appcontainer_sid
            or snapshot['semantic_binding'] != provision_plan_binding(plan)
            or any(snapshot[key] is not False for key in
                   ('ancestor_traversal_verified', 'child_access_verified', 'launch_authorized'))
            or any(not isinstance(snapshot[key], str) or not _SHA.fullmatch(snapshot[key])
                   for key in ('origin_plan_fingerprint', 'ancestor_host_identity_fingerprint',
                               'acl_baseline_fingerprint', 'fingerprint'))
            or _sha({key: value for key, value in snapshot.items() if key != 'fingerprint'}) != snapshot['fingerprint']
            or type(snapshot['targets']) is not list or not 1 <= len(snapshot['targets']) <= MAX_TARGETS):
        raise ProvisionError('exact pending child access snapshot required; no access authority')
    try:
        if any(type(item) is not dict or set(item) != {'path', 'access_mask', 'kind', 'volume', 'file_id'}
               for item in snapshot['targets']):
            raise ValueError('target fields')
        targets = tuple(ProbeTarget(**item) for item in snapshot['targets'])
        if tuple((item.path, item.kind, item.access_mask) for item in targets) != _child_access_paths(plan):
            raise ValueError('incomplete or changed targets')
        ancestors = [asdict(item) for item in targets
                     if item.path != plan.paths.root and not _inside(item.path, plan.paths.root)]
        if _sha(ancestors) != snapshot['ancestor_host_identity_fingerprint']:
            raise ValueError('ancestor identity binding')
    except (TypeError, ValueError) as error:
        raise ProvisionError('complete exact held-identity probe targets required') from error
    return DeferredChildAccessRequest(snapshot['origin_plan_fingerprint'], snapshot['semantic_binding'],
        snapshot['root'], snapshot['sid'], snapshot['ancestor_host_identity_fingerprint'],
        snapshot['acl_baseline_fingerprint'], targets)


def _checked(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path).startswith(('\\\\', '//')):
        raise ProvisionError('ledger requires an explicit local absolute path')
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if (part.is_symlink() or part.is_junction()
                or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise ProvisionError('ledger path contains link/reparse point')
    return path


def _ordinary(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ProvisionError('ledger/lock must be ordinary single-link file')
    return value


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProvisionError('duplicate ledger JSON field')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProvisionError('nonfinite ledger')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProvisionError('invalid ledger JSON') from error


def _inside(path, root):
    return path.casefold().startswith(root.casefold() + '\\')


def _validate_plan(plan, approved_parent, profile_name):
    canonical_local_path(approved_parent)
    if type(plan) is not LaunchPlan or plan.fingerprint != plan_fingerprint(plan):
        raise ProvisionError('exact immutable policy plan required')
    SandboxPolicy(**asdict(plan.policy))
    if (plan.actual_verified or plan.os_actions_performed or plan.fallback_allowed
            or plan.capabilities or plan.network_capabilities):
        raise ProvisionError('unexecuted zero-capability fail-closed plan required')
    root = canonical_local_path(plan.paths.root)
    if ntpath.dirname(root).casefold() != approved_parent.casefold():
        raise ProvisionError('fresh run root must be a direct child of approved parent')
    if not re.fullmatch(r'sandbox-lab-[A-Za-z0-9_-]{8,80}', ntpath.basename(root)):
        raise ProvisionError('unique host-owned sandbox-lab run root required')
    if any(part.casefold() in {'appdata', 'credentials', 'vault'} for part in root.split('\\')):
        raise ProvisionError('protected data root forbidden')
    runtime, code, scratch = (plan.paths.runtime, plan.paths.code, plan.paths.scratch)
    for path in (runtime, code, scratch):
        canonical_local_path(path)
        if not _inside(path, root):
            raise ProvisionError('grant path outside fresh run root')
    if code != root + '\\code\\' + plan.binding.plugin_id + '\\' + plan.binding.digest:
        raise ProvisionError('code path must bind plugin digest')
    if scratch != root + '\\scratch\\' + plan.binding.call_id:
        raise ProvisionError('scratch path must bind unique call')
    if any(a.casefold() == b.casefold() or _inside(a, b) or _inside(b, a)
           for a, b in ((runtime, code), (runtime, scratch), (code, scratch))):
        raise ProvisionError('runtime/code/scratch overlap')
    if plan.runtime_manifest_sha256 != inventory_fingerprint(plan.runtime_files):
        raise ProvisionError('runtime inventory mismatch')
    inventory_fingerprint(plan.code_files)
    parents = set()
    for path in (runtime, code, scratch):
        parent = ntpath.dirname(path)
        while parent == root or _inside(parent, root):
            parents.add(parent)
            if parent == root:
                break
            parent = ntpath.dirname(parent)
    targets = {p.casefold() for p in (runtime, code, scratch)}
    expected = [AclGrant(runtime, plan.appcontainer_sid, 'RX', 'this-directory-and-descendants'),
                AclGrant(code, plan.appcontainer_sid, 'RX', 'this-directory-and-descendants'),
                AclGrant(scratch, plan.appcontainer_sid, 'RW', 'this-directory-and-descendants')]
    expected += [AclGrant(path, plan.appcontainer_sid, 'X', 'this-directory-only')
                 for path in sorted(parents) if path.casefold() not in targets]
    if plan.acl_grants != tuple(expected):
        raise ProvisionError('ACL must contain only exact minimal run-root grants, never approved parent')
    if not _NAME.fullmatch(profile_name):
        raise ProvisionError('host-owned profile name required')
    plan_provision(profile_name, plan.appcontainer_sid, plan.acl_grants)
    return root


def _validate_deferred_plan(plan):
    validate_launch_plan(plan, require_physical=False)
    if (plan.policy.mode not in ('normal','stdio_mcp')
            or not re.fullmatch(r'sandbox-lab-[0-9a-f]{32}', ntpath.basename(plan.paths.root))):
        raise ProvisionError('deferred child access requires strict fresh UUID root plan')


def _observation(adapter, plan, *, defer_child_access=False):
    try:
        observed = adapter.observe(plan)
    except Exception as error:
        raise ProvisionError('trusted fresh-root and inventory observer unavailable') from error
    fields = {'source', 'root', 'fresh_owned_root', 'inventory_verified',
              'ancestor_traversal_verified', 'identity_fingerprint'}
    if defer_child_access:
        _validate_deferred_plan(plan)
        fields.add('child_access')
    if (type(observed) is not dict or set(observed) != fields
            or observed['source'] != 'host_os_query' or observed['root'] != plan.paths.root
            or any(observed[key] is not True for key in ('fresh_owned_root', 'inventory_verified'))
            or observed['ancestor_traversal_verified'] is not (False if defer_child_access else True)
            or not isinstance(observed['identity_fingerprint'], str)
            or not _SHA.fullmatch(observed['identity_fingerprint'])):
        raise ProvisionError('unverified root/inventory/ancestor traversal; no outside-root grants allowed')
    if defer_child_access:
        validate_deferred_child_access_request(observed['child_access'], plan)
    return copy.deepcopy(observed)


def _acl(adapter, grant):
    try:
        observed = adapter.inspect_acl(grant.path, grant.sid)
    except Exception as error:
        raise ProvisionError('trusted exact-path ACL inspection unavailable') from error
    fields = {'source', 'path', 'sid', 'sddl', 'identity_fingerprint', 'target_aces', 'broader_access'}
    if (type(observed) is not dict or set(observed) != fields
            or observed['source'] != 'host_os_query' or observed['path'] != grant.path
            or observed['sid'] != grant.sid or observed['broader_access'] is not False
            or not isinstance(observed['identity_fingerprint'], str)
            or not _SHA.fullmatch(observed['identity_fingerprint'])
            or not isinstance(observed['sddl'], str) or not observed['sddl'].startswith('D:')
            or '\x00' in observed['sddl'] or len(observed['sddl']) > 65536
            or type(observed['target_aces']) is not list):
        raise ProvisionError('unsafe or ambiguous target ACL observation')
    for ace in observed['target_aces']:
        if (type(ace) is not dict or set(ace) != {'type', 'flags', 'mask'}
                or any(type(ace[key]) is not int for key in ace)):
            raise ProvisionError('invalid target ACE observation')
    return copy.deepcopy(observed)


def _without_target(sddl, sid):
    # Canonical Win32 SDDL has no nested ACE parentheses. An AppContainer SID has
    # no short SDDL alias; an ambiguous string is rejected, never rewritten.
    return re.sub(r'\([^()]*;;;' + re.escape(sid) + r'\)', '', sddl)


def _same_non_target_dacl(before, after, sid):
    """Keep every non-target ACE byte/order and every protection/request flag.

    SetNamedSecurityInfoW may set SE_DACL_AUTO_INHERITED (SDDL AI) while
    propagating the newly approved inheritable ACE. This completion metadata is
    not a grant. Only absent->AI is admitted; P/AR and all ACEs remain exact.
    No descriptor is rewritten or weakened to make the comparison pass.
    """
    def split(value):
        stripped=_without_target(value,sid)
        match=re.fullmatch(r'D:((?:AI|AR|P)*)(.*)',stripped)
        if match is None:raise ProvisionError('invalid canonical DACL projection')
        flags=re.findall(r'AI|AR|P',match[1])
        if len(flags)!=len(set(flags)):raise ProvisionError('duplicate DACL flags')
        return set(flags),match[2]
    old_flags,old_aces=split(before);new_flags,new_aces=split(after)
    return old_aces==new_aces and (new_flags==old_flags or new_flags==old_flags|{'AI'})


def _verify_tree(adapter, grant):
    """A directory's inheritable ACE alone does not prove all descendants effective."""
    try:
        proof = adapter.verify_tree_acl(grant)
    except Exception as error:
        raise ProvisionError('complete descendant ACL verification unavailable') from error
    expected = {'source': 'host_os_query', 'path': grant.path, 'sid': grant.sid,
                'all_descendants_verified': True, 'non_target_entries_preserved': True,
                'broader_access': False}
    if type(proof) is not dict or proof != expected:
        raise ProvisionError('descendant permissions are unverified or broader than approved')
    return proof


def _expected_aces(grant):
    return [{'type': 0, 'flags': 3 if grant.scope == 'this-directory-and-descendants' else 0,
             'mask': MASKS[grant.rights]}]


class ProvisionLedger:
    """Bounded provision-only journal; ready/readonly NEVER authorizes launch.

    Legacy mode still requires pre-verified traversal. The separate explicit
    defer_child_access=True contract accepts only normal fresh UUID-root pending
    host-identity observations; it cannot attest a child token or complete access.
    No implicit native adapter, process launch, token creation or user-data path.
    """
    def __init__(self, ledger_path, *, approved_parent, native_adapter, clock=time.monotonic, ttl=300,
                 defer_child_access=False):
        canonical_local_path(approved_parent)
        methods = ('observe', 'create_profile', 'inspect_acl', 'apply_acl', 'inspect_profile', 'verify_tree_acl')
        if (native_adapter is None or not all(callable(getattr(native_adapter, name, None)) for name in methods)
                or type(ttl) not in (int, float) or not 0 < ttl <= 600):
            raise ProvisionError('complete explicit native adapter and bounded TTL required')
        if type(defer_child_access) is not bool:
            raise ProvisionError('explicit boolean deferred child access opt-in required')
        self.defer_child_access = defer_child_access
        self.path = Path(ledger_path)
        self.approved_parent, self.adapter, self.clock, self.ttl = approved_parent, native_adapter, clock, ttl
        self._lock = threading.RLock()
        self._pending = {}

    def _read(self):
        path = _checked(self.path)
        if not path.exists():
            return {'schema': FORMAT, 'revision': 0, 'records': []}, {'exists': False}
        before = _ordinary(path)
        if before.st_size > MAX_BYTES:
            raise ProvisionError('provision ledger capacity exceeded')
        with path.open('rb') as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ProvisionError('ledger replaced while opening')
            raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > MAX_BYTES or _identity(before) != _identity(after)
                or _identity(before) != _identity(_ordinary(path))):
            raise ProvisionError('ledger changed while reading')
        value = _parse(raw)
        if (type(value) is not dict or set(value) != {'schema', 'revision', 'records'}
                or value['schema'] != FORMAT or type(value['revision']) is not int
                or not 0 <= value['revision'] <= 2**53-1 or type(value['records']) is not list
                or len(value['records']) > MAX_RECORDS):
            raise ProvisionError('unknown provision ledger format')
        ids = set()
        for record in value['records']:
            if (type(record) is not dict or set(record) != {'id', 'intent', 'phase', 'events'}
                    or record['id'] in ids or type(record['events']) is not list or not record['events']):
                raise ProvisionError('invalid provision record')
            ids.add(record['id'])
            previous = ''
            for index, event in enumerate(record['events']):
                if (type(event) is not dict or set(event) != {'seq', 'phase', 'detail', 'previous', 'hash'}
                        or event['seq'] != index or event['previous'] != previous
                        or _sha({key: event[key] for key in event if key != 'hash'}) != event['hash']):
                    raise ProvisionError('provision journal chain mismatch')
                previous = event['hash']
            if record['phase'] != record['events'][-1]['phase']:
                raise ProvisionError('provision journal phase mismatch')
        return value, {'exists': True, 'identity': _identity(before), 'sha256': hashlib.sha256(raw).hexdigest()}

    def snapshot(self):
        with self._lock:
            document, observation = self._read()
            return {**copy.deepcopy(document), 'fingerprint': _sha({'document': document, 'observation': observation})}

    @contextmanager
    def _process_lock(self):
        path = _checked(self.path)
        if not _checked(path.parent).is_dir():
            raise ProvisionError('explicit ledger parent must already exist')
        lock = _checked(path.with_name(path.name + '.lock'))
        if lock.exists():
            _ordinary(lock)
        with lock.open('a+b') as stream:
            if _identity(os.fstat(stream.fileno()))[:2] != _identity(_ordinary(lock))[:2]:
                raise ProvisionError('lock file replacement')
            if stream.seek(0, 2) == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ProvisionError('another provision transaction holds the ledger') from error
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _publish(self, document, expected):
        if self._read()[1] != expected:
            raise ProvisionError('ledger CAS changed; stop and review external edit')
        path = _checked(self.path)
        value = copy.deepcopy(document); value['revision'] += 1
        raw = (_canonical(value) + '\n').encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ProvisionError('ledger capacity exceeded')
        temporary = _checked(path.with_name('.' + path.name + '.' + secrets.token_hex(12) + '.tmp'))
        try:
            with temporary.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if self._read()[1] != expected:
                raise ProvisionError('ledger changed during publish')
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                _ordinary(temporary); temporary.unlink()
        fresh, observed = self._read()
        return fresh, observed

    def _validate(self, plan, profile_name):
        root = _validate_plan(plan, self.approved_parent, profile_name)
        if self.defer_child_access:
            _validate_deferred_plan(plan)
        ledger = str(_checked(self.path))
        if ledger.casefold() == root.casefold() or _inside(ledger, root):
            raise ProvisionError('authorization ledger must stay outside every sandbox run root')
        return root

    def preview(self, plan, *, profile_name, expected_revision):
        """Read-only. All prospective paths/SID/rights are visible in the returned intent."""
        with self._lock:
            self._validate(plan, profile_name)
            document, observed = self._read()
            if type(expected_revision) is not int or expected_revision != document['revision']:
                raise ProvisionError('stale provision ledger revision')
            now = self.clock()
            self._pending = {key: value for key, value in self._pending.items() if value['expires'] > now}
            if len(self._pending) >= MAX_PENDING or len(document['records']) >= MAX_RECORDS:
                raise ProvisionError('provision intent/ledger capacity exceeded')
            for record in document['records']:
                old = record['intent']
                if (old['root'].casefold() == plan.paths.root.casefold()
                        or old['profile_name'] == profile_name or old['sid'] == plan.appcontainer_sid):
                    raise ProvisionError('root/profile/SID already journaled; no adoption or replay')
            baseline = _observation(self.adapter, plan, defer_child_access=self.defer_child_access)
            if (self.defer_child_access
                    and baseline['child_access']['origin_plan_fingerprint'] != plan.fingerprint):
                raise ProvisionError('pending child access origin must match exact provision plan')
            acls = [_acl(self.adapter, grant) for grant in plan.acl_grants]
            if any(item['target_aces'] for item in acls):
                raise ProvisionError('fresh profile SID already has ACL entries; manual review required')
            intent = {'root': plan.paths.root, 'approved_parent': self.approved_parent,
                      'profile_name': profile_name, 'sid': plan.appcontainer_sid,
                      'policy_fingerprint': plan.fingerprint,
                      'runtime_manifest_sha256': plan.runtime_manifest_sha256,
                      'code_manifest_sha256': inventory_fingerprint(plan.code_files),
                      'grants': [asdict(grant) for grant in plan.acl_grants],
                      'root_observation': baseline, 'acl_before': acls,
                      'expected_revision': expected_revision,
                      'notice': 'exact new root only; no parent ACL, MIC, owner, profile deletion or fallback'}
            if self.defer_child_access:
                intent['child_access_mode'] = 'deferred_owned_suspended_child'
                intent['provision_binding'] = provision_plan_binding(plan)
            token = secrets.token_urlsafe(32); fingerprint = _sha(intent)
            self._pending[hashlib.sha256(token.encode()).hexdigest()] = {
                'plan': plan, 'intent': intent, 'observed': observed, 'expires': now + self.ttl,
                'fingerprint': fingerprint}
            return {'token': token, 'fingerprint': fingerprint, 'snapshot': copy.deepcopy(intent),
                    'expires_in': self.ttl, 'approval_required': True, 'os_actions_performed': False}

    def commit(self, token, fingerprint, *, user_confirmed):
        with self._lock:
            token_key = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else ''
            approval = self._pending.pop(token_key, None)
            if (not approval or user_confirmed is not True or fingerprint != approval['fingerprint']
                    or self.clock() >= approval['expires']):
                raise ProvisionError('exact unexpired one-attempt human approval required')
            plan, intent = approval['plan'], approval['intent']
            self._validate(plan, intent['profile_name'])
            with self._process_lock():
                document, observed = self._read()
                if observed != approval['observed'] or document['revision'] != intent['expected_revision']:
                    raise ProvisionError('stale ledger; no OS action attempted')
                if (_observation(self.adapter, plan, defer_child_access=self.defer_child_access) != intent['root_observation']
                        or [_acl(self.adapter, grant) for grant in plan.acl_grants] != intent['acl_before']):
                    raise ProvisionError('root, inventory or ACL changed after preview; no OS action attempted')
                record = {'id': 'provision-' + secrets.token_hex(16), 'intent': intent,
                          'phase': '', 'events': []}
                document['records'].append(record)
                def event(phase, detail):
                    nonlocal document, observed, record
                    item = {'seq': len(record['events']), 'phase': phase, 'detail': detail,
                            'previous': record['events'][-1]['hash'] if record['events'] else ''}
                    item['hash'] = _sha(item)
                    record['events'].append(item); record['phase'] = phase
                    document, observed = self._publish(document, observed)
                    record = next(value for value in document['records'] if value['id'] == record['id'])
                event('createprofile_intent', {'profile_name': intent['profile_name'], 'sid': intent['sid']})
                try:
                    # Adapter must create-only and reject existing profile, never auto-adopt.
                    created = self.adapter.create_profile(intent['profile_name'], intent['sid'])
                    if created != {'profile_name': intent['profile_name'], 'sid': intent['sid'], 'created_new': True}:
                        raise ProvisionError('profile creation not exact/create-only')
                    event('profile_created', created)
                    for index, grant in enumerate(plan.acl_grants):
                        before = _acl(self.adapter, grant)
                        if before != intent['acl_before'][index]:
                            raise ProvisionError('ACL changed before application')
                        event('acl_intent', {'grant': asdict(grant), 'before': before})
                        # There is intentionally no default native writer. It must apply
                        # this SID's minimum ACE while preserving all other entries.
                        self.adapter.apply_acl(grant, expected_before_fingerprint=_sha(before))
                        after = _acl(self.adapter, grant)
                        if (after['identity_fingerprint'] != before['identity_fingerprint']
                                or after['target_aces'] != _expected_aces(grant)
                                or not _same_non_target_dacl(before['sddl'], after['sddl'], grant.sid)):
                            raise ProvisionError('ACL change broader than approved or object changed')
                        tree_proof = _verify_tree(self.adapter, grant)
                        event('acl_verified', {'grant': asdict(grant), 'before': before, 'after': after, 'tree': tree_proof})
                    if _observation(self.adapter, plan, defer_child_access=self.defer_child_access) != intent['root_observation']:
                        raise ProvisionError('root or inventories changed while provisioning')
                    ready_detail = {'sid': intent['sid'], 'launch_authorized': False}
                    if self.defer_child_access:
                        ready_detail.update(child_access_phase='pending_owned_suspended_child',
                                            ancestor_traversal_verified=False, child_access_verified=False)
                    event('ready', ready_detail)
                except BaseException as error:
                    # If journal itself was externally changed, do NOT overwrite it to
                    # record recovery. Surface uncertainty and retain all OS objects.
                    try:
                        event('needs_review', {'reason': 'provision_incomplete_or_ambiguous',
                            'profile_removed': False, 'acl_rollback_attempted': False,
                            'failed_after_phase':record['phase'],'diagnostics':_safe_provision_failure(error)})
                    except Exception as journal_error:
                        raise ProvisionError('provision outcome uncertain and ledger changed; manual review required') from journal_error
                    raise ProvisionError('provision incomplete; retained profile/ACLs need explicit review, no automatic retry') from error
                result = {'id': record['id'], 'phase': record['phase'], 'sid': intent['sid'],
                          'root': intent['root'], 'launch_authorized': False, 'os_actions_performed': True}
                if self.defer_child_access:
                    result.update(child_access=copy.deepcopy(intent['root_observation']['child_access']),
                                  ancestor_traversal_verified=False, child_access_verified=False)
                return result

    def readonly_verify(self, record_id, plan, *, profile_name):
        """Inspect only: matching journal is necessary, not sufficient for launch."""
        with self._lock:
            self._validate(plan, profile_name)
            document, _ = self._read()
            record = next((item for item in document['records'] if item['id'] == record_id), None)
            deferred_record = bool(record and record['intent'].get('child_access_mode') == 'deferred_owned_suspended_child')
            if deferred_record:
                binding_matches = (self.defer_child_access
                    and record['intent'].get('provision_binding') == provision_plan_binding(plan)
                    and record['intent']['root_observation'].get('child_access', {}).get('origin_plan_fingerprint')
                        == record['intent']['policy_fingerprint'])
            else:
                # Never reinterpret old ready records as deferred access evidence.
                binding_matches = bool(record and not self.defer_child_access
                    and 'child_access_mode' not in record['intent']
                    and record['intent']['policy_fingerprint'] == plan.fingerprint)
            if (not record or record['phase'] != 'ready' or not binding_matches
                    or record['intent']['profile_name'] != profile_name
                    or record['intent']['sid'] != plan.appcontainer_sid
                    or record['intent']['root'] != plan.paths.root):
                raise ProvisionError('ready owned ledger/profile/root match required')
            try:
                profile = self.adapter.inspect_profile(profile_name)
            except Exception as error:
                raise ProvisionError('profile ownership observation unavailable') from error
            if profile != {'source': 'host_os_query', 'profile_name': profile_name, 'sid': plan.appcontainer_sid}:
                raise ProvisionError('profile SID mismatch')
            if _observation(self.adapter, plan, defer_child_access=self.defer_child_access) != record['intent']['root_observation']:
                raise ProvisionError('root/inventory verification changed')
            verified = [event for event in record['events'] if event['phase'] == 'acl_verified']
            if len(verified) != len(plan.acl_grants):
                raise ProvisionError('incomplete ACL journal')
            for grant, event in zip(plan.acl_grants, verified):
                if (event['detail']['grant'] != asdict(grant) or _acl(self.adapter, grant) != event['detail']['after']
                        or _verify_tree(self.adapter, grant) != event['detail']['tree']):
                    raise ProvisionError('ACL no longer matches exact approved result')
            result = {'record_id': record_id, 'root': plan.paths.root, 'sid': plan.appcontainer_sid,
                      'profile_ledger_verified': True, 'acl_verified': True, 'launch_authorized': False,
                      'os_actions_performed': False}
            if deferred_record:
                result.update(child_access=copy.deepcopy(record['intent']['root_observation']['child_access']),
                              ancestor_traversal_verified=False, child_access_verified=False)
            return result


class NativeReadOnlyAclInspector:
    """Lazy Win32 DACL observer only; intentionally no profile/ACL write methods.

    This does not establish root ownership, inventories or ancestor traversal. A
    real host must supply those observations separately. Unsupported target ACEs,
    inherited broad target grants and ambiguous ACLs fail closed, never normalize.
    """
    def __init__(self, *, approved_root):
        canonical_local_path(approved_root)
        self.approved_root = approved_root

    def inspect_acl(self, path, sid):
        canonical_local_path(path)
        if path.casefold() != self.approved_root.casefold() and not _inside(path, self.approved_root):
            raise ProvisionError('ACL observation outside explicitly approved run root forbidden')
        if os.name != 'nt':
            raise ProvisionError('Win32 ACL observation unsupported')
        canonical_local_path(path)
        if not re.fullmatch(r'S-1-15-2-(?:[0-9]+-)*[0-9]+', sid):
            raise ProvisionError('AppContainer SID required')
        target = _checked(path)
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ProvisionError('only approved ordinary directories may receive grants')
        import ctypes as C
        from ctypes import wintypes as W
        adv = C.WinDLL('advapi32', use_last_error=True)
        kernel = C.WinDLL('kernel32', use_last_error=True)
        adv.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, W.DWORD, W.DWORD, C.c_void_p, C.c_void_p,
                                            C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)]
        adv.GetNamedSecurityInfoW.restype = W.DWORD
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [C.c_void_p, W.DWORD, W.DWORD,
                                                                           C.POINTER(W.LPWSTR), C.POINTER(W.DWORD)]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = W.BOOL
        adv.GetAclInformation.argtypes = [C.c_void_p, C.c_void_p, W.DWORD, C.c_int]
        adv.GetAclInformation.restype = W.BOOL
        adv.GetAce.argtypes = [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
        adv.GetAce.restype = W.BOOL
        adv.IsValidSid.argtypes = [C.c_void_p]; adv.IsValidSid.restype = W.BOOL
        adv.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
        adv.ConvertSidToStringSidW.restype = W.BOOL
        kernel.LocalFree.argtypes = [C.c_void_p]; kernel.LocalFree.restype = C.c_void_p
        descriptor, dacl = C.c_void_p(), C.c_void_p()
        status = adv.GetNamedSecurityInfoW(str(target), 1, 4, None, None, C.byref(dacl), None, C.byref(descriptor))
        if status:
            raise ProvisionError('DACL read failed; no changes attempted')
        try:
            if not dacl:
                raise ProvisionError('null DACL is unsafe')
            text = W.LPWSTR(); length = W.DWORD()
            if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, C.byref(text), C.byref(length)):
                raise ProvisionError('DACL SDDL conversion failed')
            try:
                sddl = text.value
            finally:
                kernel.LocalFree(text)
            class ACL_SIZE(C.Structure):
                _fields_ = [('AceCount', W.DWORD), ('AclBytesInUse', W.DWORD), ('AclBytesFree', W.DWORD)]
            size = ACL_SIZE()
            if not adv.GetAclInformation(dacl, C.byref(size), C.sizeof(size), 2) or size.AceCount > 4096:
                raise ProvisionError('ACL count unavailable/excessive')
            aces = []
            broad_package_access = False
            for index in range(size.AceCount):
                pointer = C.c_void_p()
                if not adv.GetAce(dacl, index, C.byref(pointer)):
                    raise ProvisionError('ACE read failed')
                header = C.string_at(pointer, 4)
                kind, flags, ace_size = header[0], header[1], int.from_bytes(header[2:4], 'little')
                if kind not in (0, 1) or ace_size < 16:
                    # Advanced object/callback ACE interpretation needs a separately
                    # reviewed adapter; do not pretend it is absent.
                    raise ProvisionError('unsupported ACE type; manual ACL review required')
                raw = C.string_at(pointer, ace_size)
                sid_pointer = C.c_void_p(pointer.value + 8)
                if not adv.IsValidSid(sid_pointer):
                    raise ProvisionError('invalid ACE SID')
                sid_text = W.LPWSTR()
                if not adv.ConvertSidToStringSidW(sid_pointer, C.byref(sid_text)):
                    raise ProvisionError('SID conversion failed')
                try:
                    principal = sid_text.value
                    matches = principal == sid
                    # Existing broad package grants make a minimal-isolation claim
                    # ambiguous. Do not remove them; block and ask for review.
                    if principal in {'S-1-15-2-1', 'S-1-15-2-2'} and kind == 0:
                        broad_package_access = True
                finally:
                    kernel.LocalFree(sid_text)
                if matches:
                    aces.append({'type': kind, 'flags': flags, 'mask': int.from_bytes(raw[4:8], 'little')})
            after = target.lstat()
            if _identity(info)[:2] != _identity(after)[:2]:
                raise ProvisionError('directory identity changed during ACL read')
            return {'source': 'host_os_query', 'path': path, 'sid': sid, 'sddl': sddl,
                    'identity_fingerprint': _sha({'device': info.st_dev, 'file_id': info.st_ino}),
                    'target_aces': aces, 'broader_access': broad_package_access or any(ace['type'] != 0 or ace['flags'] & ~3 for ace in aces)}
        finally:
            kernel.LocalFree(descriptor)
