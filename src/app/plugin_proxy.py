"""Pure host-proxy protocol and exact one-use authorization core.

This module does NOT create AppContainers, tokens, ACLs, pipes, sockets, processes,
network requests or files. A trusted host must attest BrokerSession, implement the
live_binding_provider and adapters, and wire a protected IPC channel separately.
Host methods (open/preview/mint/revoke) must never be exposed to package IPC.
An adapter deadline here is detected after return, not enforced against a hung
adapter. Production adapters require cancellable, independently bounded execution.
"""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import secrets
import threading
import time
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# Keep the historical host imports as aliases to one stdlib-only implementation.
# The frozen worker imports wire directly and never traverses this host module.
from app.plugin_wire import (
    MAX_FRAME, MAX_DEPTH, MAX_NODES, MAX_STRING_BYTES, PERMISSIONS, OPERATIONS,
    ProxyProtocolError, FrameDecoder, encode_json, decode_json, encode_frame,
    read_frame, write_frame, _fail, _json_shape, _SECRET, _LIKELY_KEY,
)

MAX_SESSIONS = 128
MAX_REQUESTS = 600
MAX_GRANTS = 4096
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_PLUGIN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_SID = re.compile(r'^S-1-15-2-\d+(?:-\d+)*$')
def _strict(value, keys, required=None):
    if type(value) is not dict or set(value) - set(keys) or not set(required if required is not None else keys) <= set(value):
        _fail('invalid_fields')
    return value


def _opaque(value):
    if type(value) is not str or not _ID.fullmatch(value):
        _fail('invalid_object_id')
    return value


def _sha(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        _fail('invalid_sha256')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail('invalid_integer')
    return value


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key:_freeze(child) for key, child in value.items()})
    if type(value) is list:
        return tuple(_freeze(child) for child in value)
    return value


def _clone(value):
    return decode_json(encode_json(value, safe=True))


def _digest(value):
    return hashlib.sha256(encode_json(value)).hexdigest()


def _token_hash(value):
    if type(value) is not str or not 32 <= len(value) <= 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        _fail('session_invalid')
    return hashlib.sha256(value.encode('ascii')).hexdigest()


@dataclass(frozen=True)
class BrokerSession:
    """Attested by the host, NOT OS attestation performed by this pure module."""
    plugin_id: str
    revision: int
    digest: str
    call_id: str
    pid: int
    sandbox_sid: str
    authority_fingerprint: str

    def __post_init__(self):
        if type(self.plugin_id) is not str or not _PLUGIN.fullmatch(self.plugin_id) or len(self.plugin_id) > 200:
            _fail('binding_invalid')
        _integer(self.revision, 1, 2**53 - 1); _sha(self.digest); _opaque(self.call_id)
        _integer(self.pid, 1, 2**32 - 1); _sha(self.authority_fingerprint)
        if type(self.sandbox_sid) is not str or not _SID.fullmatch(self.sandbox_sid) or len(self.sandbox_sid) > 256:
            _fail('binding_invalid')

    def wire(self):
        return {name:getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class Grant:
    session_hash: str
    binding: BrokerSession
    op: str
    params_sha256: str
    object_descriptor_sha256: str | None
    permission: str
    preview_fingerprint: str
    issued_at: float
    expires_at: float
    user_confirmed: bool
    human_intent_fingerprint: str | None = None
    bound_session_fingerprint: str | None = None


@dataclass(frozen=True)
class TrustedOperation:
    binding: BrokerSession
    op: str
    params: Mapping[str, Any]
    params_sha256: str
    permission: str
    deadline: float
    object_descriptor: Mapping[str, Any] | None = None
    object_descriptor_sha256: str | None = None


@dataclass(frozen=True)
class DescriptorCommitReceipt:
    """Host-only exact CAS receipt, never decoded from package/adapter JSON.

    Only a publishing file/data adapter may return this type. The broker still
    queries the real object provider against the NEW descriptor, preserves the
    immutable scope/budgets, and retires every grant for the old descriptor.
    A receipt does not undo a write if a later authority/deadline check fails.
    """
    binding: BrokerSession
    op: str
    params_sha256: str
    old_descriptor_sha256: str
    new_descriptor_json: bytes = field(repr=False)
    result_json: bytes = field(repr=False)


@dataclass
class _Session:
    binding: BrokerSession
    expires_at: float
    config_json: bytes
    state_json: bytes
    http_descriptors: dict[str, bytes]
    model_descriptors: dict[str, bytes]
    file_descriptors: dict[str, bytes]
    data_descriptors: dict[str, bytes]
    byte_budget: int
    time_budget_ms: float
    next_sequence: int = 1
    request_ids: set[str] = None
    times: deque = None
    pending: bool = False
    revoked: bool = False
    used_bytes: int = 0
    used_ms: float = 0

    def __post_init__(self):
        self.request_ids = set(); self.times = deque()


@dataclass(frozen=True)
class IntentSandboxPolicy:
    """Human-reviewed semantic policy; no handles, PID or random session material."""
    profile_sid: str
    runtime_inventory_sha256: str
    code_inventory_sha256: str
    paths_fingerprint: str
    mode: str = 'ordinary'
    wall_seconds: int = 10
    memory_limit_bytes: int = 256 * 1024**2
    cpu_rate_percent: int = 20
    active_process_limit: int = 1

    def __post_init__(self):
        if type(self.profile_sid) is not str or not _SID.fullmatch(self.profile_sid) or len(self.profile_sid) > 256:
            _fail('intent_policy_invalid')
        for value in (self.runtime_inventory_sha256,self.code_inventory_sha256,self.paths_fingerprint):
            _sha(value)
        expected = {'ordinary':(10,256*1024**2),'mcp':(30,512*1024**2)}
        if self.mode not in expected or any(type(value) is not int for value in
                (self.wall_seconds,self.memory_limit_bytes,self.cpu_rate_percent,self.active_process_limit)):
            _fail('intent_policy_invalid')
        if ((self.wall_seconds,self.memory_limit_bytes) != expected[self.mode]
                or self.cpu_rate_percent != 20 or self.active_process_limit != 1):
            _fail('intent_policy_invalid')

    def projection(self):
        return {name:getattr(self,name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class HostIntentOperation:
    op: str
    params_json: bytes = field(repr=False)

    def __post_init__(self):
        if self.op not in OPERATIONS or type(self.params_json) is not bytes:
            _fail('intent_operation_invalid')
        value=decode_json(self.params_json)
        if type(value) is not dict or encode_json(value,safe=True) != self.params_json:
            _fail('intent_operation_invalid')

    @classmethod
    def create(cls,op,params):
        return cls(op,encode_json(params,safe=True))


@dataclass(frozen=True)
class HostOperationIntent:
    """Immutable PRELAUNCH approval content, never parsed from package IPC.

    Frozen canonical bytes prevent mutation after preview. The trusted producer must
    derive semantic_fingerprint from the manager's exact tool/args/config/state/deps
    snapshot, not from an arbitrary package assertion. No OS identity is invented.
    """
    plugin_id: str
    revision: int
    digest: str
    call_id: str
    trust_fingerprint: str
    policy: IntentSandboxPolicy
    semantic_json: bytes = field(repr=False)
    operations: tuple[HostIntentOperation,...] = ()
    descriptor_json: bytes = field(default=b'{"data":{},"file":{},"http.request":{},"model.generate":{}}',repr=False)

    def __post_init__(self):
        if type(self.plugin_id) is not str or not _PLUGIN.fullmatch(self.plugin_id) or len(self.plugin_id)>200:
            _fail('intent_identity_invalid')
        _integer(self.revision,1,2**53-1);_sha(self.digest);_opaque(self.call_id);_sha(self.trust_fingerprint)
        if type(self.policy) is not IntentSandboxPolicy:
            _fail('intent_policy_invalid')
        if type(self.semantic_json) is not bytes or type(self.descriptor_json) is not bytes:
            _fail('intent_semantics_invalid')
        semantic=decode_json(self.semantic_json)
        _strict(semantic,('tool','args','config','state','dependency_fingerprint'))
        if (type(semantic['tool']) is not str or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,100}',semantic['tool'])
                or any(type(semantic[key]) is not dict for key in ('args','config','state'))):
            _fail('intent_semantics_invalid')
        _sha(semantic['dependency_fingerprint'])
        if encode_json(semantic,safe=True)!=self.semantic_json:
            _fail('intent_semantics_invalid')
        descriptors=decode_json(self.descriptor_json)
        _strict(descriptors,('file','data','http.request','model.generate'))
        if any(type(table) is not dict or len(table)>128 for table in descriptors.values()) or encode_json(descriptors,safe=True)!=self.descriptor_json:
            _fail('intent_descriptor_invalid')
        if type(self.operations) is not tuple or len(self.operations)>32 or any(type(op) is not HostIntentOperation for op in self.operations):
            _fail('intent_operation_invalid')
        seen=set()
        for operation in self.operations:
            identity=(operation.op,operation.params_json)
            if identity in seen:
                _fail('intent_duplicate_operation')
            seen.add(identity)
        # Validate the complete human projection and conservatively reserve wire
        # overhead for 32 fixed-length grants/session tokens BEFORE process launch.
        projection=self.projection()
        if len(encode_json(projection,safe=True))+8192>MAX_FRAME:
            _fail('intent_wire_budget')
        _validate_prelaunch_intent(self)

    @classmethod
    def create(cls,plugin_id,revision,digest,call_id,trust_fingerprint,policy,*,tool,args,config,state,
               dependency_fingerprint,operations=(),file_descriptors=None,data_descriptors=None,
               http_descriptors=None,model_descriptors=None):
        semantic={'tool':tool,'args':args,'config':config,'state':state,'dependency_fingerprint':dependency_fingerprint}
        descriptors={'file':{} if file_descriptors is None else file_descriptors,
                     'data':{} if data_descriptors is None else data_descriptors,
                     'http.request':{} if http_descriptors is None else http_descriptors,
                     'model.generate':{} if model_descriptors is None else model_descriptors}
        return cls(plugin_id,revision,digest,call_id,trust_fingerprint,policy,encode_json(semantic,safe=True),
                   tuple(operations),encode_json(descriptors,safe=True))

    def projection(self):
        return {'v':1,'plugin_id':self.plugin_id,'revision':self.revision,'digest':self.digest,
                'call_id':self.call_id,'trust_fingerprint':self.trust_fingerprint,
                'policy':self.policy.projection(),'semantic':decode_json(self.semantic_json),
                'descriptors':decode_json(self.descriptor_json),
                'operations':[{'op':op.op,'params':decode_json(op.params_json)} for op in self.operations]}

    @property
    def fingerprint(self):
        return _digest(self.projection())


@dataclass(frozen=True,eq=False)
class ApprovedOperationIntent:
    """Opaque owner capability. Copying its fields cannot forge the store identity."""
    _owner: object = field(repr=False)
    _token: str = field(repr=False)
    intent_fingerprint: str


class IntentApprovalStore:
    """Host-only human approval, with no process/session/Key/file I/O."""
    def __init__(self,*,clock=time.monotonic,ttl=120,max_records=128):
        if not callable(clock) or type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0<ttl<=300 or type(max_records) is not int or not 1<=max_records<=128:
            raise ValueError('bounded explicit intent approval policy required')
        self.clock,self.ttl,self.max_records=clock,ttl,max_records
        self._previews={};self._approved={};self._lock=threading.RLock()

    def _purge(self):
        now=self.clock()
        self._previews={key:item for key,item in self._previews.items() if item[1]>now}
        self._approved={key:item for key,item in self._approved.items() if item[2]>now}

    def preview(self,intent):
        if type(intent) is not HostOperationIntent:
            _fail('host_intent_required')
        with self._lock:
            self._purge()
            if len(self._previews)+len(self._approved)>=self.max_records:
                _fail('intent_capacity')
            token=secrets.token_urlsafe(32)
            self._previews[token]=(intent,self.clock()+self.ttl)
            return {'token':token,'fingerprint':intent.fingerprint,'snapshot':intent.projection(),
                    'approval_required':True,'expires_in':self.ttl,'process_started':False}

    def cancel_preview(self,token):
        """Host-only withdrawal of an unapproved preview, not an approved capability."""
        if type(token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',token):
            _fail('intent_preview_invalid')
        with self._lock:
            return self._previews.pop(token,None) is not None

    def approve(self,token,fingerprint,*,user_confirmed):
        with self._lock:
            record=self._previews.pop(token,None) if type(token) is str else None
            if (record is None or self.clock()>=record[1] or user_confirmed is not True
                    or type(fingerprint) is not str or not _SHA.fullmatch(fingerprint) or not secrets.compare_digest(record[0].fingerprint,fingerprint)):
                _fail('intent_confirmation_required')
            capability=ApprovedOperationIntent(self,secrets.token_urlsafe(32),fingerprint)
            self._approved[capability._token]=(capability,record[0],record[1])
            return capability

    def revoke(self,capability):
        with self._lock:
            if type(capability) is not ApprovedOperationIntent or capability._owner is not self:
                _fail('intent_approval_invalid')
            record=self._approved.get(capability._token)
            if record is None or record[0] is not capability:
                return False
            self._approved.pop(capability._token)
            return True

    def clear(self):
        with self._lock:
            self._previews.clear(); self._approved.clear()

    def _claim(self,capability):
        with self._lock:
            if type(capability) is not ApprovedOperationIntent or capability._owner is not self:
                _fail('intent_approval_invalid')
            record=self._approved.get(capability._token)
            if record is None or record[0] is not capability:
                _fail('intent_approval_invalid')
            self._approved.pop(capability._token)
            if self.clock()>=record[2]:
                _fail('intent_approval_expired')
            return record[1], record[2]


@dataclass(frozen=True)
class VerifiedIntentChild:
    """Trusted verifier return value. A dataclass/boolean alone is NOT OS proof.

    verify_child must query the held process/token/Job/handle ownership, including
    creation identity. Wire JSON, raw PID equality or SID derivation are not evidence.
    """
    child: object
    policy: IntentSandboxPolicy
    process_handle: int
    creation_time: int
    creation_identity_sha256: str
    source: str = 'unverified'
    is_appcontainer: bool = False
    job_verified: bool = False
    handles_verified: bool = False
    network_verified: bool = False

    def validate(self):
        from app.plugin_windows_launcher import AttestedChild
        if type(self.child) is not AttestedChild or type(self.policy) is not IntentSandboxPolicy:
            _fail('intent_child_unverified')
        _integer(self.process_handle,1,2**64-1);_integer(self.creation_time,1,2**64-1)
        _sha(self.creation_identity_sha256)
        expected=_digest({'process_handle':self.process_handle,'pid':self.child.pid,'creation_time':self.creation_time})
        if self.creation_identity_sha256!=expected or self.source!='host_os_query' or any(value is not True for value in
                (self.is_appcontainer,self.job_verified,self.handles_verified,self.network_verified)):
            _fail('intent_child_unverified')
        _integer(self.child.pid,1,2**32-1);_sha(self.child.policy_fingerprint)
        if (self.child.appcontainer_sid!=self.policy.profile_sid or self.child.runtime_sha256!=self.policy.runtime_inventory_sha256
                or type(self.child.capabilities) is not tuple or self.child.capabilities
                or type(self.child.network_capabilities) is not tuple or self.child.network_capabilities
                or type(self.child.inherited_roles) is not tuple or len(self.child.inherited_roles)!=3):
            _fail('intent_child_unverified')
        roles=[];handles=[]
        for item in self.child.inherited_roles:
            if type(item) is not tuple or len(item)!=2 or type(item[0]) is not str:
                _fail('intent_child_unverified')
            roles.append(item[0]);handles.append(_integer(item[1],1,2**64-1))
        if set(roles)!={'request_read','result_write','diagnostics_write'} or len(set(handles))!=3:
            _fail('intent_child_unverified')
        return self


def _validate_prelaunch_intent(intent):
    """Use the SAME broker parameter/descriptor validators, without a BrokerSession.

    This disposable pure validator does not open a session or observe an object.
    Actual binding and object liveness are mandatory again during child claim.
    """
    descriptors=decode_json(intent.descriptor_json)
    validator=object.__new__(PluginProxyBroker)
    validator._object_provider=lambda *_:None
    # Descriptor syntax needs only the plugin scope, never a fabricated PID/SID.
    scope=SimpleNamespace(plugin_id=intent.plugin_id)
    normalized={}
    for kind in ('file','data'):
        normalized[kind]={}
        for object_id,raw in descriptors[kind].items():
            _opaque(object_id)
            clean=validator._object_descriptor(raw,scope,kind,object_id)
            if clean!=raw:
                _fail('intent_descriptor_not_canonical')
            normalized[kind][object_id]=encode_json(clean,safe=True)
    for op in ('http.request','model.generate'):
        normalized[op]={}
        for object_id,raw in descriptors[op].items():
            _opaque(object_id)
            clean=validator._params(op,raw,None,descriptor_validation=True)
            field_name='request_descriptor' if op=='http.request' else 'profile_id'
            if clean[field_name]!=object_id:
                _fail('intent_descriptor_invalid')
            normalized[op][object_id]=encode_json(clean,safe=True)
    state=SimpleNamespace(file_descriptors=normalized['file'],data_descriptors=normalized['data'],
                          http_descriptors=normalized['http.request'],model_descriptors=normalized['model.generate'])
    for operation in intent.operations:
        validator._params(operation.op,decode_json(operation.params_json),state)


def bound_init_fingerprint(init):
    """Full post-bind integrity hash, NOT a prior human semantic confirmation.

    Tokens/PID may exist only now. The worker's v1 init fields are not changed by
    this helper; callers still validate exact init shape and the 64 KiB frame cap.
    """
    return hashlib.sha256(encode_json(init,safe=True)).hexdigest()


class PluginProxyBroker:
    """Host-only facade + request handler. No implicit adapter or live-check fallback."""
    def __init__(self, *, live_binding_provider: Callable[[BrokerSession], BrokerSession | None],
                 adapters: Mapping[str, Callable[[TrustedOperation], Any]], clock=time.monotonic,
                 session_ttl=300, grant_ttl=60, max_adapter_ms=10000,
                 object_descriptor_provider: Callable[[BrokerSession, str, str], dict | None] | None = None,
                 runtime_deadline_provider=None):
        if not callable(live_binding_provider):
            raise TypeError('An explicit trusted live_binding_provider is required')
        if type(adapters) is not dict or set(adapters) - set(PERMISSIONS) or not all(callable(f) for f in adapters.values()):
            raise TypeError('Explicit trusted operation adapters are required')
        for value, upper in ((session_ttl, 600), (grant_ttl, 120), (max_adapter_ms, 60000)):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
                raise ValueError('Invalid bounded deadline')
        if object_descriptor_provider is not None and not callable(object_descriptor_provider):
            raise TypeError('Explicit trusted object_descriptor_provider must be callable')
        if runtime_deadline_provider is not None and not callable(runtime_deadline_provider):
            raise TypeError('explicit actual invocation deadline provider required')
        self._deadline_provider = runtime_deadline_provider
        self._live_provider = live_binding_provider
        self._object_provider = object_descriptor_provider
        self._adapters = dict(adapters)
        self._clock = clock
        self._ttl, self._grant_ttl, self._adapter_ms = session_ttl, grant_ttl, max_adapter_ms
        self._sessions: dict[str, _Session] = {}
        self._grants: dict[str, Grant] = {}
        self._audit = deque(maxlen=4096)
        self._intent_bind_attempts: set[str] = set()
        self._lock = threading.RLock()

    def _audit_event(self, state, op, code, digest=''):
        # No params, prompt, path, URL, raw token or adapter result in audit.
        self._audit.append({'plugin_id':state.binding.plugin_id, 'revision':state.binding.revision,
            'digest':state.binding.digest, 'call_id':state.binding.call_id, 'op':op,
            'params_sha256':digest, 'code':code})

    def audit_records(self):
        with self._lock:
            return [dict(event) for event in self._audit]

    def _purge(self):
        now = self._clock()
        for key, session in list(self._sessions.items()):
            if session.expires_at <= now or session.revoked:
                self._revoke(key)
                if not session.pending:
                    self._sessions.pop(key, None)
        for key, grant in list(self._grants.items()):
            if grant.expires_at <= now or grant.session_hash not in self._sessions:
                self._grants.pop(key, None)

    def _revoke(self, key):
        state = self._sessions.get(key)
        if state:
            state.revoked = True
        for token, grant in list(self._grants.items()):
            if grant.session_hash == key:
                self._grants.pop(token, None)

    def _live(self, key, state):
        if state.revoked or self._clock() >= state.expires_at:
            self._revoke(key); _fail('session_expired')
        try:
            current = self._live_provider(state.binding)
        except Exception:
            current = None
        if type(current) is not BrokerSession or current != state.binding:
            self._revoke(key); _fail('binding_changed')

    def _object_descriptor(self, raw, binding, kind, object_id):
        """Host-only, path-free identity/CAS contract. Never supplied by package RPC.

        identity_fingerprint must identify the actual opened object, not the lookup
        alias. root_scope is an opaque host approval scope. Production adapters MUST
        compare these at the critical opened-handle/IO boundary as well: this pure
        broker's pre/post resolver checks cannot remove an OS TOCTOU race.
        """
        keys = {'object_id', 'kind', 'plugin_id', 'root_scope', 'identity_fingerprint',
                'permissions', 'max_extent_bytes', 'max_read_bytes', 'max_write_bytes', 'content_sha256'}
        _strict(raw, keys, keys - {'content_sha256'})
        _json_shape(raw, safe=True)
        if raw['kind'] != kind or raw['object_id'] != object_id or raw['plugin_id'] != binding.plugin_id:
            _fail('object_scope_invalid')
        _opaque(raw['object_id']); _opaque(raw['root_scope']); _sha(raw['identity_fingerprint'])
        permissions = raw['permissions']
        if (type(permissions) is not list or not permissions or len(permissions) > 2
                or any(type(p) is not str or p not in ('read', 'write') for p in permissions)
                or len(set(permissions)) != len(permissions)):
            _fail('invalid_object_descriptor')
        _integer(raw['max_extent_bytes'], 0, 2**53 - 1)
        _integer(raw['max_read_bytes'], 0, 12000); _integer(raw['max_write_bytes'], 0, 12000)
        if (('read' in permissions) != (raw['max_read_bytes'] > 0)
                or ('write' in permissions) != (raw['max_write_bytes'] > 0)):
            _fail('invalid_object_descriptor')
        if 'content_sha256' in raw:
            _sha(raw['content_sha256'])
        normalized = _clone(raw)
        normalized['permissions'] = sorted(permissions)
        return normalized

    def _object(self, state, op, params):
        if op not in ('file.read', 'file.write', 'data.read', 'data.write'):
            return None
        if self._object_provider is None:
            _fail('object_provider_required')
        kind = op.split('.')[0]
        table = state.file_descriptors if kind == 'file' else state.data_descriptors
        encoded = table.get(params['object_id'])
        if encoded is None:
            _fail('object_not_registered')
        return decode_json(encoded)

    def _object_live(self, key, state, descriptor):
        if descriptor is None:
            return
        try:
            current = self._object_provider(state.binding, descriptor['kind'], descriptor['object_id'])
            current = self._object_descriptor(current, state.binding, descriptor['kind'], descriptor['object_id'])
            matches = encode_json(current, safe=True) == encode_json(descriptor, safe=True)
        except Exception:
            matches = False
        if not matches:
            self._revoke(key)
            _fail('object_binding_changed')

    def _committed_descriptor(self, key, state, operation, old, receipt):
        if (operation.op not in ('file.write', 'data.write') or old is None
                or receipt.binding != operation.binding or receipt.op != operation.op
                or receipt.params_sha256 != operation.params_sha256
                or receipt.old_descriptor_sha256 != operation.object_descriptor_sha256
                or type(receipt.new_descriptor_json) is not bytes or type(receipt.result_json) is not bytes):
            _fail('write_receipt_invalid')
        current = self._object_descriptor(decode_json(receipt.new_descriptor_json), state.binding,
                                          old['kind'], old['object_id'])
        if (encode_json(current, safe=True) != receipt.new_descriptor_json
                or any(current.get(name) != value for name, value in old.items()
                       if name not in ('identity_fingerprint', 'content_sha256'))
                or set(current) != set(old) or 'content_sha256' not in current):
            _fail('write_receipt_scope_changed')
        # Not a skipped postcondition: freshly query the actual published object.
        self._object_live(key, state, current)
        table = state.file_descriptors if old['kind'] == 'file' else state.data_descriptors
        if table.get(old['object_id']) != encode_json(old, safe=True):
            _fail('write_receipt_cas_changed')
        table[old['object_id']] = receipt.new_descriptor_json
        for token, grant in list(self._grants.items()):
            if grant.session_hash == key and grant.object_descriptor_sha256 == receipt.old_descriptor_sha256:
                self._grants.pop(token, None)
        self._audit_event(state, operation.op, 'write_committed', operation.params_sha256)
        return current, decode_json(receipt.result_json)

    def open_session(self, binding: BrokerSession, *, config: dict, state: dict,
                     http_descriptors=None, model_descriptors=None,
                     file_descriptors=None, data_descriptors=None, byte_budget=4 * MAX_FRAME,
                     time_budget_ms=30000) -> str:
        if type(binding) is not BrokerSession or type(config) is not dict or type(state) is not dict:
            _fail('binding_invalid')
        _integer(byte_budget, 1024, 16 * MAX_FRAME)
        if type(time_budget_ms) not in (int, float) or not math.isfinite(time_budget_ms) or not 0 < time_budget_ms <= 60000:
            _fail('invalid_budget')
        config_json, state_json = encode_json(config, safe=True), encode_json(state, safe=True)
        descriptors = []
        for kind, raw in (('http.request', http_descriptors), ('model.generate', model_descriptors)):
            raw = {} if raw is None else raw
            if type(raw) is not dict or len(raw) > 128:
                _fail('invalid_descriptor')
            table = {}
            for opaque, definition in raw.items():
                _opaque(opaque)
                normalized = self._params(kind, definition, None, descriptor_validation=True)
                if normalized['request_descriptor' if kind == 'http.request' else 'profile_id'] != opaque:
                    _fail('invalid_descriptor')
                table[opaque] = encode_json(normalized, safe=True)
            descriptors.append(table)
        for kind, raw in (('file', file_descriptors), ('data', data_descriptors)):
            raw = {} if raw is None else raw
            if type(raw) is not dict or len(raw) > 128:
                _fail('invalid_descriptor')
            if raw and self._object_provider is None:
                _fail('object_provider_required')
            table = {}
            for opaque, definition in raw.items():
                _opaque(opaque)
                normalized = self._object_descriptor(definition, binding, kind, opaque)
                table[opaque] = encode_json(normalized, safe=True)
            descriptors.append(table)
        # Bound the complete host registry, including opaque keys/table overhead.
        descriptor_bytes = json.dumps(descriptors, default=lambda data:decode_json(data),
                                      ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(descriptor_bytes) > MAX_FRAME:
            _fail('descriptor_budget')
        with self._lock:
            self._purge()
            if len(self._sessions) >= MAX_SESSIONS:
                _fail('session_capacity')
            token = secrets.token_urlsafe(32); key = _token_hash(token)
            session = _Session(binding, self._clock() + self._ttl, config_json, state_json,
                               *descriptors, byte_budget, float(time_budget_ms))
            self._live(key, session)
            for table in (session.file_descriptors, session.data_descriptors):
                for encoded in table.values():
                    self._object_live(key, session, decode_json(encoded))
            self._sessions[key] = session
            self._audit_event(session, 'session.open', 'opened')
            return token

    def bind_approved_intent(self, approvals, capability, child, *, verify_child,
                             live_intent_provider, byte_budget=4*MAX_FRAME, time_budget_ms=None):
        """Host-only: mechanically bind a prior HUMAN semantic approval to a real child.

        No package-supplied PID/SID or synthetic prelaunch BrokerSession is accepted.
        Both callbacks must be bounded trusted host observations. This pure method
        cannot forcibly interrupt a hanging callback or certify native observations.
        A failed attempt consumes the approval and revokes any partially bound state.
        """
        if type(approvals) is not IntentApprovalStore:
            _fail('intent_approval_invalid')
        intent,approval_expires=approvals._claim(capability)  # consume before ANY bind attempt
        with self._lock:
            if intent.call_id in self._intent_bind_attempts:
                _fail('intent_call_already_attempted')
            if len(self._intent_bind_attempts)>=MAX_GRANTS:
                _fail('intent_bind_capacity')
            self._intent_bind_attempts.add(intent.call_id)
        token=None;key=None
        try:
            from app.plugin_windows_launcher import AttestedChild
            if type(child) is not AttestedChild or not callable(verify_child) or not callable(live_intent_provider):
                _fail('intent_child_unverified')
            def observe():
                if approvals.clock()>=approval_expires:
                    _fail('intent_approval_expired')
                try:
                    value=verify_child(child)
                    live=live_intent_provider(intent)
                except Exception:
                    _fail('intent_binding_unavailable')
                if type(value) is not VerifiedIntentChild:
                    _fail('intent_child_unverified')
                value.validate()
                if approvals.clock()>=approval_expires:
                    _fail('intent_approval_expired')
                if (value.child!=child or value.policy!=intent.policy
                        or type(live) is not HostOperationIntent or live!=intent):
                    _fail('intent_binding_changed')
                return value
            evidence=observe()
            # Binding is constructed only AFTER the actual held child observation.
            binding=BrokerSession(intent.plugin_id,intent.revision,intent.digest,intent.call_id,
                                  child.pid,child.appcontainer_sid,intent.trust_fingerprint)
            semantic=decode_json(intent.semantic_json);descriptors=decode_json(intent.descriptor_json)
            budget=intent.policy.wall_seconds*1000 if time_budget_ms is None else time_budget_ms
            if type(budget) not in (int,float) or not math.isfinite(budget) or not 0<budget<=intent.policy.wall_seconds*1000:
                _fail('invalid_budget')
            token=self.open_session(binding,config=semantic['config'],state=semantic['state'],
                http_descriptors=descriptors['http.request'],model_descriptors=descriptors['model.generate'],
                file_descriptors=descriptors['file'],data_descriptors=descriptors['data'],
                byte_budget=byte_budget,time_budget_ms=budget)
            grants=[]
            with self._lock:
                key,state=self._session(token)
                bound_snapshot={'v':1,'human_intent_fingerprint':intent.fingerprint,
                    'session_hash':key,'binding':binding.wire(),'creation_identity_sha256':evidence.creation_identity_sha256,
                    'launch_policy_fingerprint':child.policy_fingerprint,'policy':intent.policy.projection()}
                bound_fingerprint=_digest(bound_snapshot)
                if len(self._grants)+sum(op.op in PERMISSIONS for op in intent.operations)>MAX_GRANTS:
                    _fail('grant_capacity')
                for operation in intent.operations:
                    params=decode_json(operation.params_json)
                    if operation.op not in PERMISSIONS:
                        self._params(operation.op,params,state)
                        continue
                    snapshot,_=self._preview(key,state,operation.op,params)
                    grant_token=secrets.token_urlsafe(32);now=self._clock()
                    # This is a derived grant, NOT a new human mint confirmation.
                    self._grants[_token_hash(grant_token)]=Grant(key,binding,operation.op,
                        snapshot['params_sha256'],snapshot.get('object_descriptor_sha256'),
                        snapshot['permission'],_digest(snapshot),now,
                        min(state.expires_at,now+self._grant_ttl),True,intent.fingerprint,bound_fingerprint)
                    grants.append({'op':operation.op,'params_sha256':snapshot['params_sha256'],'token':grant_token})
                    self._audit_event(state,operation.op,'grant_derived_from_intent',snapshot['params_sha256'])
                    self._audit[-1].update({'human_intent_fingerprint':intent.fingerprint,
                                           'bound_session_fingerprint':bound_fingerprint})
                self._live(key,state)
                if observe()!=evidence:
                    _fail('intent_binding_changed')
                for table in (state.file_descriptors,state.data_descriptors):
                    for encoded in table.values():
                        self._object_live(key,state,decode_json(encoded))
                self._audit_event(state,'session.bind','intent_bound')
                self._audit[-1].update({'human_intent_fingerprint':intent.fingerprint,
                                       'bound_session_fingerprint':bound_fingerprint})
            # Ensure complete control material itself is within a single wire frame.
            result={'session':token,'grants':grants,'human_intent_fingerprint':intent.fingerprint,
                    'bound_session_fingerprint':bound_fingerprint}
            encode_json(result,safe=True)
            return result
        except BaseException:
            if token is not None:
                with self._lock:
                    key=_token_hash(token)
                    self._revoke(key)
                    if key in self._sessions and not self._sessions[key].pending:
                        self._sessions.pop(key,None)
            raise

    def revoke_session(self, session_token):
        with self._lock:
            key = _token_hash(session_token)
            self._revoke(key)

    def close(self):
        with self._lock:
            for key in list(self._sessions):
                self._revoke(key)
            self._sessions.clear(); self._grants.clear()

    def _session(self, token):
        self._purge()
        key = _token_hash(token)
        state = self._sessions.get(key)
        if state is None:
            _fail('session_invalid')
        self._live(key, state)
        return key, state

    def _params(self, op, params, state, *, descriptor_validation=False):
        if op not in OPERATIONS:
            _fail('operation_unknown')
        _json_shape(params, safe=True)
        if op in ('config.read', 'state.read', 'cancel'):
            _strict(params, ())
        elif op in ('file.read', 'data.read', 'file.write', 'data.write'):
            write = op.endswith('.write')
            _strict(params, ('object_id', 'offset', 'content_base64', 'content_sha256') if write else ('object_id', 'offset', 'length'))
            _opaque(params['object_id']); _integer(params['offset'], 0, 2**53 - 1)
            if write:
                content = params['content_base64']; _sha(params['content_sha256'])
                if type(content) is not str:
                    _fail('invalid_content')
                try:
                    binary = base64.b64decode(content, validate=True)
                except ValueError:
                    _fail('invalid_content')
                if len(binary) > 12000 or base64.b64encode(binary).decode('ascii') != content or hashlib.sha256(binary).hexdigest() != params['content_sha256']:
                    _fail('invalid_content')
            else:
                _integer(params['length'], 1, 12000)
            descriptor = self._object(state, op, params)
            mode = 'write' if write else 'read'
            if mode not in descriptor['permissions']:
                _fail('object_permission_denied')
            size = len(binary) if write else params['length']
            if size > descriptor['max_' + mode + '_bytes'] or params['offset'] + size > descriptor['max_extent_bytes']:
                _fail('object_range_denied')
        elif op == 'http.request':
            _strict(params, ('request_descriptor', 'url', 'method', 'body_sha256', 'max_response_bytes', 'allow_redirects'))
            opaque = _opaque(params['request_descriptor']); _sha(params['body_sha256'])
            _integer(params['max_response_bytes'], 1, 12000)
            if params['allow_redirects'] is not False or params['method'] not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
                _fail('invalid_http_descriptor')
            url = params['url']
            if type(url) is not str or len(url) > 4000 or any(ch.isspace() or ord(ch) < 32 for ch in url) or '\\' in url:
                _fail('invalid_http_descriptor')
            try:
                split = urlsplit(url)
                port = split.port
                if split.scheme not in ('https', 'http') or not split.hostname or split.username is not None or split.password is not None or split.fragment:
                    _fail('invalid_http_descriptor')
                host = split.hostname.encode('idna').decode('ascii').lower()
                if ':' in host:
                    host = '[' + host + ']'
                if port is not None and port != (443 if split.scheme == 'https' else 80):
                    host += ':' + str(port)
                normalized = urlunsplit((split.scheme, host, split.path or '/', split.query, ''))
                if normalized != url or any(_SECRET.search(key) for key, _ in parse_qsl(split.query, keep_blank_values=True)):
                    _fail('invalid_http_descriptor')
            except (ValueError, UnicodeError):
                _fail('invalid_http_descriptor')
            if not descriptor_validation and (state is None or state.http_descriptors.get(opaque) != encode_json(params)):
                _fail('descriptor_not_registered')
        elif op == 'model.generate':
            _strict(params, ('profile_id', 'provider', 'model', 'prompt_id', 'prompt_sha256', 'max_input_tokens', 'max_output_tokens'))
            opaque = _opaque(params['profile_id']); _opaque(params['prompt_id']); _sha(params['prompt_sha256'])
            for key in ('provider', 'model'):
                if type(params[key]) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', params[key]):
                    _fail('invalid_model_descriptor')
            _integer(params['max_input_tokens'], 1, 1000000); _integer(params['max_output_tokens'], 1, 32768)
            if not descriptor_validation and (state is None or state.model_descriptors.get(opaque) != encode_json(params)):
                _fail('descriptor_not_registered')
        return _clone(params)

    def _preview(self, key, state, op, params):
        if op not in PERMISSIONS:
            _fail('grant_not_applicable')
        clean = self._params(op, params, state)
        descriptor = self._object(state, op, clean)
        self._object_live(key, state, descriptor)
        snapshot = {'v':1, 'session_hash':key, 'binding':state.binding.wire(), 'op':op,
                    'permission':PERMISSIONS[op], 'params_sha256':_digest(clean), 'params':clean}
        if descriptor is not None:
            snapshot['object_descriptor'] = descriptor
            snapshot['object_descriptor_sha256'] = _digest(descriptor)
        return snapshot, clean

    def preview_grant(self, session_token, op, params):
        """Trusted UI preflight only. It neither mints a grant nor calls an adapter."""
        with self._lock:
            key, state = self._session(session_token)
            snapshot, _ = self._preview(key, state, op, params)
            return {'snapshot':snapshot, 'fingerprint':_digest(snapshot), 'approval_required':True}

    def mint_grant(self, session_token, op, params, *, preview_fingerprint, user_confirmed):
        """Host-only: call after a human confirms this exact displayed host snapshot."""
        with self._lock:
            key, state = self._session(session_token)
            snapshot, _ = self._preview(key, state, op, params)
            expected = _digest(snapshot)
            if user_confirmed is not True or type(preview_fingerprint) is not str or not secrets.compare_digest(expected, preview_fingerprint):
                _fail('confirmation_required')
            if len(self._grants) >= MAX_GRANTS or sum(g.session_hash == key for g in self._grants.values()) >= 128:
                _fail('grant_capacity')
            now = self._clock()
            token = secrets.token_urlsafe(32)
            self._grants[_token_hash(token)] = Grant(key, state.binding, op, snapshot['params_sha256'], snapshot.get('object_descriptor_sha256'),
                snapshot['permission'], expected, now, min(state.expires_at, now + self._grant_ttl), True)
            self._audit_event(state, op, 'grant_minted', snapshot['params_sha256'])
            return token

    def _consume(self, token, key, state, op, params_hash, descriptor_hash):
        if token is None:
            _fail('grant_required')
        try:
            token_hash = _token_hash(token)
        except ProxyProtocolError:
            _fail('grant_invalid')
        grant = self._grants.pop(token_hash, None)
        if (grant is None or grant.session_hash != key or grant.binding != state.binding or grant.op != op
                or grant.params_sha256 != params_hash or grant.object_descriptor_sha256 != descriptor_hash
                or grant.permission != PERMISSIONS[op]
                or grant.expires_at <= self._clock() or grant.user_confirmed is not True):
            _fail('grant_invalid')
        return grant

    def _response(self, message, *, result=None, error=None):
        # Never echo arbitrary malformed request content in an error response.
        response = {'v':1, 'session':message.get('session') if type(message.get('session')) is str and 32 <= len(message['session']) <= 128 and re.fullmatch(r'[A-Za-z0-9_-]+', message['session']) and not _LIKELY_KEY.search(message['session']) else '',
                    'seq':message.get('seq') if type(message.get('seq')) is int and 0 < message['seq'] < 2**53 else 0,
                    'request_id':message.get('request_id') if type(message.get('request_id')) is str and _ID.fullmatch(message['request_id']) and not _LIKELY_KEY.search(message['request_id']) else '',
                    'status':'error' if error else 'ok'}
        if error:
            response['error'] = {'code':error, 'message':'Proxy operation refused or outcome unconfirmed; do not replay.'}
        else:
            response['result'] = result
        encode_json(response, safe=True)
        return response

    def handle(self, message):
        """Package-facing request only; returns bounded, correlated generic outcomes."""
        clean_message = message if type(message) is dict else {}
        state = None; key = ''; op = ''; params_hash = ''; started = None
        dispatch = None
        try:
            wire_size = len(encode_json(message))
            _strict(message, ('v', 'session', 'seq', 'request_id', 'op', 'params', 'grant'), ('v', 'session', 'seq', 'request_id', 'op', 'params'))
            if type(message['v']) is not int or message['v'] != 1:
                _fail('version_unsupported')
            _opaque(message['request_id'])
            if _LIKELY_KEY.search(message['request_id']):
                _fail('secret_material')
            if type(message['op']) is not str:
                _fail('operation_unknown')
            op = message['op']
            with self._lock:
                key, state = self._session(message['session'])
                if type(message['seq']) is not int or message['seq'] != state.next_sequence or message['request_id'] in state.request_ids:
                    _fail('request_replay')
                now = self._clock()
                while state.times and state.times[0] <= now - 60:
                    state.times.popleft()
                if len(state.request_ids) >= MAX_REQUESTS or len(state.times) >= 60 or sum(t > now - 1 for t in state.times) >= 10:
                    _fail('request_rate')
                if state.used_bytes + wire_size > state.byte_budget or state.used_ms >= state.time_budget_ms:
                    self._revoke(key); _fail('session_budget')
                state.next_sequence += 1; state.request_ids.add(message['request_id']); state.times.append(now)
                state.used_bytes += wire_size
                params = self._params(op, message['params'], state)
                params_hash = _digest(params)
                if op == 'cancel':
                    if 'grant' in message:
                        _fail('grant_not_applicable')
                    inflight = state.pending
                    self._revoke(key); self._audit_event(state, op, 'cancelled')
                    return self._response(message, result={'cancelled':True, 'inflight_side_effects_not_undone':inflight})
                if state.pending:
                    _fail('request_pending')
                if op in ('config.read', 'state.read'):
                    if 'grant' in message:
                        _fail('grant_not_applicable')
                    result = decode_json(state.config_json if op == 'config.read' else state.state_json)
                    response = self._response(message, result={'value':result})
                    state.used_bytes += len(encode_json(response))
                    if state.used_bytes > state.byte_budget:
                        self._revoke(key); _fail('session_budget')
                    self._audit_event(state, op, 'completed', params_hash)
                    return response
                if op not in self._adapters:
                    _fail('adapter_unavailable')
                descriptor = self._object(state, op, params)
                descriptor_hash = _digest(descriptor) if descriptor is not None else None
                self._consume(message.get('grant'), key, state, op, params_hash, descriptor_hash)
                self._object_live(key, state, descriptor)
                started = self._clock()
                deadline = min(state.expires_at, started + min(self._adapter_ms, state.time_budget_ms - state.used_ms) / 1000)
                if self._deadline_provider is not None:
                    actual_deadline = self._deadline_provider(state.binding)
                    if (type(actual_deadline) not in (int, float) or not math.isfinite(actual_deadline)
                            or actual_deadline <= started):
                        _fail('runtime_deadline_unavailable')
                    deadline = min(deadline, actual_deadline)
                state.pending = True
                dispatch = (self._adapters[op], TrustedOperation(state.binding, op, _freeze(params), params_hash, PERMISSIONS[op], deadline,
                    _freeze(descriptor) if descriptor is not None else None, descriptor_hash))
                self._audit_event(state, op, 'started', params_hash)
            try:
                result = dispatch[0](dispatch[1])
            except Exception:
                _fail('adapter_failed')
            with self._lock:
                if type(result) is DescriptorCommitReceipt:
                    descriptor, result = self._committed_descriptor(key, state, dispatch[1], descriptor, result)
                elif op in ('file.write', 'data.write'):
                    _fail('write_commit_receipt_required')
                response = self._response(message, result=_clone(result))
                self._live(key, state)
                # Reject an unconfirmed result if an alias changed during adapter IO.
                # Effects may already have happened: the consumed grant is never replayed.
                self._object_live(key, state, descriptor)
                if self._clock() > dispatch[1].deadline:
                    _fail('adapter_deadline')
                response_bytes = len(encode_json(response))
                if state.used_bytes + response_bytes > state.byte_budget:
                    self._revoke(key); _fail('session_budget')
                state.used_bytes += response_bytes
                self._audit_event(state, op, 'completed', params_hash)
                return response
        except ProxyProtocolError as exc:
            with self._lock:
                if state is not None:
                    self._audit_event(state, op if op in OPERATIONS else 'unknown', exc.code, params_hash)
            return self._response(clean_message, error=exc.code)
        except Exception:
            with self._lock:
                if state is not None:
                    self._audit_event(state, op if op in OPERATIONS else 'unknown', 'request_invalid', params_hash)
            return self._response(clean_message, error='request_invalid')
        finally:
            if dispatch is not None and state is not None:
                with self._lock:
                    state.pending = False
                    state.used_ms += max(0, (self._clock() - started) * 1000)
                    if state.used_ms >= state.time_budget_ms:
                        self._revoke(key)
