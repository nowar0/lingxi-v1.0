"""Host-only ordinary strict invocation adapters; no import/construction I/O.

Explicit args.broker_requests entries are {op,target_argument,params}; the tool
input_schema must declare that field. Existing manifest tools.targets still binds
permission/kind/argument. A path or URL NEVER implies read/write/GET/body/model.
Host composition supplies real scopes, pinned requests and compiled materials;
none of those capabilities are accepted from plugin RPC or browser JSON.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from app.plugin_proxy import (BrokerSession, HostIntentOperation, HostOperationIntent,
    TrustedOperation, VerifiedIntentChild, PERMISSIONS, encode_json, decode_json)
from app.plugin_file_proxy import FileObjectRegistry, PreparedWrite
from app.plugin_http_proxy import HttpTarget, ExactHttpProxy, PinnedHttpTransport, system_resolver
from app.model_submission import CompiledSubmission
from app.model_pending_runtime import RuntimeModelInvocation, RuntimePendingModels
from app.plugin_model_api import HostPendingModelRequest
from app.plugin_model_proxy import ModelAttemptPolicy
from app.plugin_wire import _SECRET, _LIKELY_KEY

EXTERNAL_OPS = frozenset(('file.read', 'file.write', 'data.read', 'data.write', 'http.request', 'model.generate'))
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')


class RuntimeBrokerError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise RuntimeBrokerError(code)


def _plain(value):
    if type(value) is dict: return {key:_plain(child) for key, child in value.items()}
    if type(value) in (tuple, list): return [_plain(child) for child in value]
    return value


def _host_bytes(value):
    # Manager snapshots are not wire frames; actual intent/init retain the 64KiB cap.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode('utf-8')
    if len(raw) > 2 * 1024**2: _fail('broker_host_snapshot_limit')
    return raw


def _copy(value):
    return json.loads(_host_bytes(_plain(value)))


def _hash(value):
    return hashlib.sha256(_host_bytes(value)).hexdigest()


def _absolute(value):
    if type(value) is not str or not Path(value).is_absolute() or '..' in Path(value).parts:
        _fail('broker_absolute_target_required')
    return os.path.abspath(value)


@dataclass(frozen=True)
class ObjectScope:
    """Real host-approved root plus manager owner/generation/epoch lease.

    live(scope)->True must observe the actual manager, including this call's owned
    epoch transitions. It cannot trust serialized evidence or return default True.
    """
    approved_root: str
    root_scope: str
    binding_json: bytes = field(repr=False)
    live: object = field(repr=False, compare=False)
    data_root: str | None = None

    def __post_init__(self):
        _absolute(self.approved_root)
        if (type(self.root_scope) is not str or not _ID.fullmatch(self.root_scope)
                or type(self.binding_json) is not bytes or not callable(self.live)
                or type(decode_json(self.binding_json)) is not dict
                or encode_json(decode_json(self.binding_json), safe=True) != self.binding_json):
            _fail('broker_object_scope_invalid')
        if self.data_root is not None: _absolute(self.data_root)

    def projection(self):
        return {'approved_root':self.approved_root, 'root_scope':self.root_scope,
                'data_root':self.data_root, 'binding':decode_json(self.binding_json)}

    def revalidate(self):
        if self.live(self) is not True: _fail('broker_object_scope_changed')


@dataclass(frozen=True)
class PreparedHttpRequest:
    """Host's already-approved DNS/IP/target/body; validation performs no DNS."""
    target: HttpTarget
    body: bytes = field(repr=False)
    live: object = field(repr=False, compare=False)

    def validate(self):
        if (type(self.target) is not HttpTarget or type(self.body) is not bytes
                or len(self.body) > 12000 or not callable(self.live)
                or hashlib.sha256(self.body).hexdigest() != self.target.body_sha256
                or self.target.method in ('GET', 'HEAD') and self.body):
            _fail('broker_http_prepared_request_invalid')
        try:
            text = self.body.decode('utf-8')
        except UnicodeError:
            _fail('broker_http_body_not_inspectable')
        if _SECRET.search(text) or _LIKELY_KEY.search(text): _fail('broker_http_body_secret_forbidden')
        if self.live(self) is not True: _fail('broker_http_prepared_request_changed')

    def projection(self):
        return {'target':_plain(asdict(self.target)), 'target_fingerprint':self.target.fingerprint,
                'body_sha256':hashlib.sha256(self.body).hexdigest(), 'body_bytes':len(self.body),
                'body_utf8':self.body.decode('utf-8')}


@dataclass(frozen=True)
class PreparedModelRequest:
    """Actual precompiled materials/route, never placeholder prompt/evidence.

    revalidate(compiled) checks real materials, selection, route/capability and key
    metadata generation without loading a key. The default remains verified-only.
    allow_host_configured is accepted only together with the actual host export's
    typed source capability and identical live callback, never SDK evidence/bools.
    Final sending requires independent authenticated HostModelApprovals UI approval.
    """
    compiled: CompiledSubmission = field(repr=False)
    params_json: bytes = field(repr=False)
    owner_session_id: str = field(repr=False)
    revalidate: object = field(repr=False, compare=False)
    allow_host_configured: bool = False
    attempt_policy: ModelAttemptPolicy | None = field(default=None, repr=False, compare=False)

    def validate(self, params):
        if (type(self.compiled) is not CompiledSubmission or not callable(self.revalidate)
                or type(params) is not dict or type(self.params_json) is not bytes
                or encode_json(params, safe=True) != self.params_json
                or type(self.allow_host_configured) is not bool):
            _fail('model_prepared_materials_required')
        if self.compiled.ready_for_verified_adapter is not True:
            if (self.allow_host_configured is not True or type(self.attempt_policy) is not ModelAttemptPolicy
                    or self.attempt_policy.source is not None
                    or self.attempt_policy.source_revalidate is not self.revalidate):
                _fail('model_prepared_materials_required')
            self.attempt_policy.validate_source(self.compiled, self.owner_session_id)
        elif self.allow_host_configured or self.attempt_policy is not None:
            _fail('model_prepared_attempt_policy_mismatch')
        route = self.compiled.route
        manifest = json.loads(self.compiled.submission_manifest_bytes)
        if (type(manifest) is not dict or params.get('prompt_sha256') != manifest.get('text_sha256')
                or type(self.owner_session_id) is not str or not 16 <= len(self.owner_session_id) <= 128
                or any(not (char.isascii() and (char.isalnum() or char in '_-')) for char in self.owner_session_id)):
            _fail('model_prepared_prompt_or_owner_changed')
        media_estimate = self.compiled.media_token_estimate
        text_estimate = self.compiled.estimated_text_tokens
        if (params.get('profile_id') != route.profile_id or params.get('provider') != route.provider_id
                or params.get('model') != route.model or params.get('max_output_tokens') != route.max_output_tokens
                or type(params.get('max_input_tokens')) is not int or not 1 <= params['max_input_tokens'] <= 1000000
                or type(text_estimate) is not int or text_estimate < 0
                or media_estimate is not None and (type(media_estimate) is not int or media_estimate < 0)):
            _fail('model_prepared_descriptor_changed')
        if media_estimate is None:
            # Only the explicitly configured, source-capability-bound attempt may
            # retain an UNKNOWN media estimate. This does NOT claim a total-input
            # token ceiling: the exact bytes/material limits and known text budget
            # still bind the request, and the UI must show that limitation.
            if self.allow_host_configured is not True or text_estimate > params['max_input_tokens']:
                _fail('model_prepared_descriptor_changed')
        elif text_estimate + media_estimate > params['max_input_tokens']:
            _fail('model_prepared_descriptor_changed')
        if self.revalidate(self.compiled) is not True: _fail('model_prepared_materials_changed')

    def projection(self):
        route = self.compiled.route
        return {'submission':self.compiled.preview_to_ui(), 'endpoint':route.endpoint,
                'route_fingerprint':route.fingerprint, 'capability_fingerprint':self.compiled.capability_fingerprint,
                'key_generation':route.credential_generation,
                'key_authority_sha256':hashlib.sha256(route.credential_authority.encode()).hexdigest(),
                'owner_session_id_sha256':hashlib.sha256(self.owner_session_id.encode()).hexdigest(),
                'input_token_budget_verified':False,
                'media_token_estimate_known':self.compiled.media_token_estimate is not None,
                'input_token_budget_notice':('输入 token 数仅为估计，未实测验证。' if self.compiled.media_token_estimate is not None
                    else '媒体 token 估计未知；max_input_tokens 只校验已知文本估计，不保证实际总输入 token 上限。'),
                'final_send_requires_separate_ui_approval':True, 'sent':False}


@dataclass(frozen=True)
class BrokerPlan:
    call_id: str
    input_sha256: str
    operations: tuple[HostIntentOperation, ...]
    descriptors_json: bytes = field(repr=False)
    public_json: bytes = field(repr=False)
    objects: tuple = field(repr=False, compare=False)
    http: tuple = field(repr=False, compare=False)
    model: tuple = field(repr=False, compare=False)

    def projection(self):
        return json.loads(self.public_json)

    def intent_kwargs(self):
        tables = decode_json(self.descriptors_json)
        return {'operations':self.operations, 'file_descriptors':tables['file'],
                'data_descriptors':tables['data'], 'http_descriptors':tables['http.request'],
                'model_descriptors':tables['model.generate']}


def request_contract(manifest, tool, args):
    """Strict explicit operation identity; no inference from permissions/targets."""
    declared = [item for item in manifest.get('tools', []) if type(item) is dict and item.get('name') == tool]
    if len(declared) != 1 or type(args) is not dict: _fail('broker_tool_contract_invalid')
    declaration = declared[0]
    permissions = set(manifest.get('permissions', [])) | set(declaration.get('permissions', []))
    requests = args.get('broker_requests')
    if requests is None:
        if permissions & set(PERMISSIONS.values()) or declaration.get('targets'):
            _fail('broker_explicit_operation_declaration_required')
        return (), ()
    schema = declaration.get('input_schema', {}).get('properties', {}).get('broker_requests')
    if type(schema) is not dict or schema.get('type') != 'array':
        _fail('broker_request_input_schema_declaration_required')
    if type(requests) is not list or not 1 <= len(requests) <= 32: _fail('broker_explicit_requests_invalid')
    targets = declaration.get('targets', [])
    normalized = []; used = set()
    for request in requests:
        if type(request) is not dict or set(request) != {'op', 'target_argument', 'params'}:
            _fail('broker_explicit_request_fields_invalid')
        op, argument, params = request['op'], request['target_argument'], request['params']
        if op not in EXTERNAL_OPS or type(argument) is not str or type(params) is not dict:
            _fail('broker_explicit_request_invalid')
        permission = PERMISSIONS[op]
        if permission not in permissions: _fail('broker_operation_permission_not_declared')
        kind = 'path' if op.startswith(('file.', 'data.')) else 'url' if op == 'http.request' else 'string'
        matches = [(index, target) for index, target in enumerate(targets)
            if type(target) is dict and target == {'permission':permission, 'argument':argument, 'kind':kind}]
        if len(matches) != 1 or type(args.get(argument)) is not str: _fail('broker_operation_target_not_declared')
        used.add(matches[0][0]); target = args[argument]
        if kind == 'path':
            target = _absolute(target)
            expected = {'object_id', 'offset', 'content_base64', 'content_sha256'} if op.endswith('.write') else {'object_id', 'offset', 'length'}
            if (set(params) != expected or type(params.get('object_id')) is not str
                    or not _ID.fullmatch(params['object_id']) or type(params.get('offset')) is not int
                    or not 0 <= params['offset'] <= 2**53-1):
                _fail('broker_object_request_fields_invalid')
            if op.endswith('.read'):
                if type(params['length']) is not int or not 1 <= params['length'] <= 12000:
                    _fail('broker_object_request_range_invalid')
            else:
                if (type(params['content_base64']) is not str or type(params['content_sha256']) is not str
                        or not _SHA.fullmatch(params['content_sha256'])):
                    _fail('broker_object_request_content_invalid')
                try:
                    binary = base64.b64decode(params['content_base64'], validate=True)
                except ValueError:
                    _fail('broker_object_request_content_invalid')
                if (not 1 <= len(binary) <= 12000 or base64.b64encode(binary).decode('ascii') != params['content_base64']
                        or hashlib.sha256(binary).hexdigest() != params['content_sha256']):
                    _fail('broker_object_request_content_invalid')
        if op == 'http.request' and params.get('url') != target: _fail('broker_http_url_target_changed')
        if op == 'model.generate' and params.get('profile_id') != target: _fail('broker_model_profile_target_changed')
        normalized.append({'op':op, 'target_argument':argument, 'target':target, 'params':_copy(params)})
    if used != set(range(len(targets))): _fail('broker_declared_target_not_scoped')
    public_targets = tuple({'permission':target['permission'], 'kind':target['kind'],
                            'value':args[target['argument']]} for target in targets)
    return tuple(normalized), public_targets


class RuntimeBrokerServices:
    """Concrete adapters plus explicit trusted host composition providers.

    object_scope(input,request)->ObjectScope; http_request->PreparedHttpRequest;
    model_request->PreparedModelRequest. effect_guard(operation,None_or_http)
    returns a manager context manager that persists epoch/dirty BEFORE yielding.
    Missing providers fail closed; no callback may be obtained from plugin JSON.
    """
    def __init__(self, *, object_scope=None, http_request=None, model_request=None,
                 effect_guard=None, clock=time.monotonic, request_contract_approved=False):
        if type(request_contract_approved) is not bool:
            raise TypeError('explicit host decision about the public broker request contract required')
        self.request_contract_approved = request_contract_approved
        if not callable(clock) or any(value is not None and not callable(value)
                for value in (object_scope, http_request, model_request, effect_guard)):
            raise TypeError('explicit trusted broker providers required')
        self.object_scope, self.http_request, self.model_request = object_scope, http_request, model_request
        self.effect_guard, self.clock = effect_guard, clock
        self._plans = {}; self._active = {}; self._lock = threading.RLock(); self._closed = False
        self._plan_bytes = 0
        self.pending_models = RuntimePendingModels(live_invocation=self.live_model_invocation, clock=clock)

    def status(self):
        return {'request_contract_approved':self.request_contract_approved,
                'file_read':self.request_contract_approved and callable(self.object_scope),
                'data_read':self.request_contract_approved and callable(self.object_scope),
                'file_write':self.request_contract_approved and callable(self.object_scope) and callable(self.effect_guard),
                'data_write':self.request_contract_approved and callable(self.object_scope) and callable(self.effect_guard),
                'http_request':self.request_contract_approved and callable(self.http_request) and callable(self.effect_guard),
                'model_pending':self.request_contract_approved and callable(self.model_request), 'model_final_send_integrated':False}

    def prepare(self, *, manifest, tool, args, config, state, plugin_id, revision,
                digest, trust_fingerprint, dependency_binding, call_id):
        source = {'manifest':manifest, 'tool':tool, 'args':args, 'config':config, 'state':state,
                  'plugin_id':plugin_id, 'revision':revision, 'digest':digest,
                  'trust_fingerprint':trust_fingerprint, 'dependency_binding':dependency_binding, 'call_id':call_id}
        requests, targets = request_contract(manifest, tool, args)
        if requests and self.request_contract_approved is not True:
            _fail('broker_request_contract_host_decision_required')
        input_sha = _hash(source)
        with self._lock:
            if self._closed: _fail('broker_services_closed')
            previous = self._plans.get(call_id)
            if previous is not None and previous.input_sha256 != input_sha: _fail('broker_prestart_input_changed')
            if previous is None and len(self._plans) >= 4096: _fail('broker_plan_capacity')
        if previous is not None:
            if (previous.objects and not callable(self.object_scope)
                    or previous.http and not callable(self.http_request)
                    or previous.model and not callable(self.model_request)
                    or any(item.op.endswith('.write') or item.op == 'http.request' for item in previous.operations)
                       and not callable(self.effect_guard)):
                _fail('broker_composition_changed')
            # Foreign manager callbacks run outside this registry lock. Never adopt
            # a new object descriptor; initial scope remains the human approval.
            for kind, object_id, _, scope in previous.objects:
                scope.revalidate()
                request = next(item for item in requests if item['op'].split('.')[0] == kind
                               and item['params'].get('object_id') == object_id)
                fresh = self.object_scope(_copy(source), _copy(request))
                if type(fresh) is not ObjectScope or fresh.projection() != scope.projection():
                    _fail('broker_prestart_object_scope_changed')
                fresh.revalidate()
            for prepared in previous.http: prepared.validate()
            for prepared in previous.model: prepared.validate(decode_json(prepared.params_json))
            return previous
        tables = {'file':{}, 'data':{}, 'http.request':{}, 'model.generate':{}}
        operations = []; objects = []; http = []; model = []; scopes = []; seen_objects = set()
        for request in requests:
            op, params = request['op'], request['params']
            operations.append(HostIntentOperation.create(op, params))
            if op.startswith(('file.', 'data.')):
                if not callable(self.object_scope): _fail('broker_object_scope_not_integrated')
                if op.endswith('.write') and not callable(self.effect_guard): _fail('broker_write_epoch_guard_not_integrated')
                scope = self.object_scope(_copy(source), _copy(request))
                if type(scope) is not ObjectScope: _fail('broker_object_scope_not_integrated')
                scope.revalidate(); kind = op.split('.')[0]; object_id = params.get('object_id')
                if object_id in seen_objects: _fail('broker_one_operation_per_object_required')
                seen_objects.add(object_id)
                if kind == 'data' and scope.data_root is None: _fail('broker_data_generation_scope_required')
                registry = FileObjectRegistry(scope.approved_root,
                    live_authority_provider=lambda binding:self._live_binding(binding),
                    data_roots={plugin_id:scope.data_root} if scope.data_root is not None else {}, clock=self.clock)
                descriptor = registry.register(request['target'], kind=kind, plugin_id=plugin_id,
                    permissions=['write' if op.endswith('.write') else 'read'], root_scope=scope.root_scope,
                    max_read_bytes=params.get('length', 0) if op.endswith('.read') else 0,
                    max_write_bytes=len(base64.b64decode(params.get('content_base64', ''), validate=True)) if op.endswith('.write') else 0,
                    object_id=object_id)
                tables[kind][object_id] = descriptor; objects.append((kind, object_id, registry, scope))
                scopes.append({'op':op, 'target_argument':request['target_argument'], 'path':request['target'],
                               'params':params, 'descriptor':descriptor, 'scope':scope.projection()})
            elif op == 'http.request':
                if not callable(self.http_request) or not callable(self.effect_guard): _fail('broker_http_not_integrated')
                prepared = self.http_request(_copy(source), _copy(request))
                if type(prepared) is not PreparedHttpRequest: _fail('broker_http_prepared_request_required')
                prepared.validate()
                if params != prepared.target.descriptor(): _fail('broker_http_descriptor_changed')
                key = prepared.target.request_descriptor
                if key in tables['http.request']: _fail('broker_duplicate_http_request')
                tables['http.request'][key] = params; http.append(prepared)
                scopes.append({'op':op, 'target_argument':request['target_argument'], 'params':params, 'scope':prepared.projection()})
            else:
                if not callable(self.model_request): _fail('model_prepared_materials_not_integrated')
                prepared = self.model_request(_copy(source), _copy(request))
                if type(prepared) is not PreparedModelRequest: _fail('model_prepared_materials_required')
                prepared.validate(params)
                if model: _fail('model_one_pending_per_invocation_required')
                tables['model.generate'][params['profile_id']] = params; model.append(prepared)
                scopes.append({'op':op, 'target_argument':request['target_argument'], 'params':params, 'scope':prepared.projection()})
        public = {'schema':'lingxi.ordinary-broker-plan.v1', 'call_id':call_id, 'input_sha256':input_sha,
                  'semantic_sha256':_hash({'tool':tool, 'args':args, 'config':config, 'state':state,
                                          'dependency_fingerprint':_hash(dependency_binding)}),
                  'identity':{'plugin_id':plugin_id, 'revision':revision, 'digest':digest, 'trust_fingerprint':trust_fingerprint},
                  'targets':list(targets), 'scopes':scopes,
                  'operations':[{'op':item.op, 'params':decode_json(item.params_json)} for item in operations],
                  'descriptors':tables, 'prestart_pid':None, 'no_direct_path_socket_shell_grants':True,
                  'model_final_send_integrated':False}
        plan = BrokerPlan(call_id, input_sha, tuple(operations), encode_json(tables, safe=True),
                          _host_bytes(public), tuple(objects), tuple(http), tuple(model))
        retained_bytes = len(plan.descriptors_json) + len(plan.public_json) + sum(len(item.params_json) for item in plan.operations)
        retained_bytes += sum(len(item.body) for item in plan.http)
        retained_bytes += sum(len(item.compiled.payload_bytes) + len(item.compiled.submission_manifest_bytes)
                              + len(item.compiled.preview_content_bytes) for item in plan.model)
        with self._lock:
            if self._closed or call_id in self._plans: _fail('broker_plan_already_prepared')
            if self._plan_bytes + retained_bytes > 256 * 1024**2: _fail('broker_plan_byte_capacity')
            self._plans[call_id] = plan; self._plan_bytes += retained_bytes
        return plan

    def invocation(self, plan, *, live_authority):
        if type(plan) is not BrokerPlan or not callable(live_authority):
            raise TypeError('exact prestart broker plan and manager authority required')
        with self._lock:
            if self._closed or self._plans.get(plan.call_id) is not plan or plan.call_id in self._active:
                _fail('broker_invocation_already_claimed_or_unknown')
            owner = InvocationBrokerAdapters(self, plan, live_authority)
            self._active[plan.call_id] = owner
            return owner

    def _live_binding(self, binding):
        with self._lock: owner = self._active.get(binding.call_id)
        return owner.live_binding(binding) if owner is not None else None

    def live_model_invocation(self, binding):
        with self._lock: owner = self._active.get(binding.call_id)
        if owner is None or owner.live_binding(binding) != binding or owner.deadline is None: return None
        if not owner.plan.model: return None
        return RuntimeModelInvocation(binding, owner.plan.model[0].owner_session_id, owner.deadline, owner.cancel, 'ordinary')

    def model_attempt_policy(self, pending):
        """HostModelApprovals callback: exact server-held pending, never HTTP data.

        Returning None preserves the strict verified path. Configured attempts
        resolve ONLY the capability attached by this service's own runtime adapter.
        Looking up a source policy by profile/owner alone is deliberately forbidden.
        """
        if type(pending) is not HostPendingModelRequest:
            _fail('model_actual_pending_policy_required')
        with self._lock:
            owner = self._active.get(pending.binding.call_id)
        if (self._closed or owner is None or owner._pending != pending.request_id
                or owner.live_binding(pending.binding) != pending.binding
                or self.pending_models.pending(pending.request_id) is not pending
                or len(owner.plan.model) != 1):
            _fail('model_actual_pending_policy_changed')
        prepared = owner.plan.model[0]
        prepared.validate(decode_json(prepared.params_json))
        if (prepared.compiled is not pending.compiled
                or prepared.owner_session_id != pending.owner_session_id):
            _fail('model_actual_pending_policy_changed')
        policy = owner._model_policy
        if prepared.allow_host_configured is not True:
            if policy is not None:
                _fail('model_actual_pending_policy_changed')
            return None
        if (type(policy) is not ModelAttemptPolicy or policy.source is not prepared.attempt_policy
                or policy.pending is not pending or policy.compiled is not pending.compiled
                or policy.owner_session_id != pending.owner_session_id
                or policy.deadline != owner.deadline or policy.cancel is not owner.cancel
                or owner.live_binding(pending.binding) != pending.binding):
            _fail('model_actual_pending_policy_changed')
        return policy

    def live_model_attempt_policy(self, policy):
        """ModelProxyService registry callback; same bound instance or fail closed."""
        if (type(policy) is not ModelAttemptPolicy or policy.pending is None
                or self.model_attempt_policy(policy.pending) is not policy):
            _fail('model_actual_pending_policy_changed')
        return policy

    def pending_resources(self):
        with self._lock: plans = tuple(self._plans.values())
        result = {}
        for plan in plans:
            records = [registry.resource_status() for _, _, registry, _ in plan.objects]
            if any(item['retained_stages'] or item['unconfirmed_publications'] for item in records): result[plan.call_id] = records
        return result

    def close(self):
        with self._lock:
            self._closed = True; owners = tuple(self._active.values())
        for owner in owners: owner.close()
        self.pending_models.close()


class InvocationBrokerAdapters:
    """One real native child claim and one Resume-derived runtime deadline."""
    def __init__(self, services, plan, live_authority):
        self.services, self.plan, self.authority = services, plan, live_authority
        self.binding = None; self.evidence = None; self.cancel = None; self.deadline = None
        self._native_live = None; self._closed = False; self._intent = None
        self._http = None; self._pending = None; self._model_policy = None
        self._lock = threading.RLock()

    def validate_intent(self, intent):
        public = self.plan.projection()
        if (type(intent) is not HostOperationIntent or intent.call_id != self.plan.call_id
                or intent.operations != self.plan.operations or intent.descriptor_json != self.plan.descriptors_json
                or hashlib.sha256(intent.semantic_json).hexdigest() != public['semantic_sha256']
                or public['identity'] != {'plugin_id':intent.plugin_id, 'revision':intent.revision,
                    'digest':intent.digest, 'trust_fingerprint':intent.trust_fingerprint}):
            _fail('broker_invocation_intent_changed')
        if self._intent is not None and self._intent != intent: _fail('broker_invocation_intent_changed')
        self._intent = intent

    def claim_child(self, evidence, binding, cancel, *, live_binding):
        if type(evidence) is not VerifiedIntentChild or type(binding) is not BrokerSession: _fail('broker_actual_child_required')
        evidence.validate()
        if (self._intent is None or evidence.policy != self._intent.policy
                or binding.call_id != self.plan.call_id or binding.pid != evidence.child.pid
                or binding.sandbox_sid != evidence.child.appcontainer_sid
                or binding.plugin_id != self._intent.plugin_id or binding.revision != self._intent.revision
                or binding.digest != self._intent.digest or binding.authority_fingerprint != self._intent.trust_fingerprint
                or not isinstance(cancel, threading.Event) or not callable(live_binding)):
            _fail('broker_actual_child_binding_changed')
        with self._lock:
            if self._closed or self.binding is not None: _fail('broker_child_already_claimed')
            self.evidence, self.binding, self.cancel, self._native_live = evidence, binding, cancel, live_binding
        if self.live_binding(binding) != binding: _fail('broker_actual_child_no_longer_live')
        if self.plan.http:
            self._http = ExactHttpProxy(live_authority_provider=self.live_binding,
                resolver=system_resolver, transport=PinnedHttpTransport(clock=self.services.clock),
                body_provider=self._body, clock=self.services.clock)
            for prepared in self.plan.http:
                prepared.validate(); preview = self._http.preview_target(binding, prepared.target)
                # Mechanical projection of consumed PRESTART approval, no human wait.
                if prepared.projection() not in [item.get('scope') for item in self.plan.projection()['scopes']]:
                    _fail('broker_http_prestart_scope_changed')
                self._http.register_target(binding, prepared.target, preview_fingerprint=preview['fingerprint'], user_confirmed=True)

    def started(self, deadline):
        if (self.binding is None or self.deadline is not None or type(deadline) not in (int, float)
                or not math.isfinite(deadline) or not self.services.clock() < deadline <= self.services.clock()+10):
            _fail('broker_actual_runtime_deadline_required')
        self.deadline = deadline; self._check(self.binding, deadline)

    def live_binding(self, binding):
        if (self.services._closed or self.services.request_contract_approved is not True
                or self._closed or self.binding != binding or self.cancel is None or self.cancel.is_set()): return None
        if self.deadline is not None and self.services.clock() >= self.deadline: return None
        try:
            if self.authority() is not True or self._native_live(binding) != binding: return None
            for _, _, _, scope in self.plan.objects: scope.revalidate()
        except Exception: return None
        return binding

    def runtime_deadline(self, binding):
        self._check(binding, self.deadline)
        return self.deadline

    def _check(self, binding, deadline):
        if (deadline is None or self.live_binding(binding) != binding
                or self.services.clock() >= deadline or deadline > self.deadline):
            _fail('broker_cancelled_deadline_or_authority_changed')

    def _object(self, kind, object_id):
        choices = [item for item in self.plan.objects if item[:2] == (kind, object_id)]
        if len(choices) != 1: _fail('broker_object_not_prepared')
        return choices[0][2], choices[0][3]

    def object_descriptor_provider(self, binding, kind, object_id):
        if self.live_binding(binding) != binding: _fail('broker_object_authority_changed')
        registry, scope = self._object(kind, object_id); scope.revalidate()
        return registry.object_descriptor_provider(binding, kind, object_id, deadline=self.deadline)

    def _authorized(self, operation, prepared=None):
        if type(operation) is not TrustedOperation: _fail('broker_trusted_operation_required')
        self._check(operation.binding, operation.deadline)
        if not any(item.op == operation.op and hashlib.sha256(item.params_json).hexdigest() == operation.params_sha256
                   for item in self.plan.operations): _fail('broker_operation_not_in_consumed_intent')
        if prepared is not None:
            if (type(prepared) is not PreparedWrite or prepared.snapshot.get('params_sha256') != operation.params_sha256
                    or prepared.snapshot.get('old_descriptor_sha256') != operation.object_descriptor_sha256):
                _fail('broker_prepared_write_binding_changed')
        return True

    @contextmanager
    def _effect_guard(self, operation, prepared):
        self._authorized(operation)
        if not callable(self.services.effect_guard): _fail('broker_effect_epoch_guard_not_integrated')
        with self.services.effect_guard(operation, prepared):
            self._authorized(operation)
            yield
        # Effects may have happened; never compensate/reset dirty/erase owner.
        self._check(operation.binding, operation.deadline)

    def _file_read(self, operation):
        self._authorized(operation)
        registry, scope = self._object(operation.op.split('.')[0], operation.params['object_id'])
        scope.revalidate(); result = registry.read_adapter(operation)
        self._check(operation.binding, operation.deadline)
        return result

    def _file_write(self, operation):
        self._authorized(operation)
        registry, scope = self._object(operation.op.split('.')[0], operation.params['object_id']); scope.revalidate()
        return registry.write_adapter(operation, commit_authorizer=self._authorized, publication_guard=self._effect_guard)

    def _body(self, binding, body_id):
        self._check(binding, self.deadline)
        matches = [item for item in self.plan.http if item.target.body_id == body_id]
        if len(matches) != 1: _fail('broker_http_body_unavailable')
        matches[0].validate()
        return matches[0].body

    def _http_request(self, operation):
        self._authorized(operation)
        if self._http is None: _fail('broker_http_not_claimed')
        prepared = [item for item in self.plan.http if item.target.descriptor() == dict(operation.params)]
        if len(prepared) != 1: _fail('broker_http_prestart_scope_changed')
        prepared[0].validate()
        with self._effect_guard(operation, prepared[0]): return self._http(operation, cancel=self.cancel)

    def _model_pending(self, operation):
        self._authorized(operation)
        if len(self.plan.model) != 1 or self._pending is not None: _fail('model_pending_not_prepared_or_already_registered')
        prepared = self.plan.model[0]; prepared.validate(dict(operation.params))
        invocation = self.services.live_model_invocation(operation.binding)
        if type(invocation) is not RuntimeModelInvocation: _fail('model_actual_running_child_required')
        result = self.services.pending_models.register(invocation, prepared.compiled, materials_revalidate=prepared.revalidate)
        self._pending = result['request_id']
        try:
            if prepared.allow_host_configured:
                pending = self.services.pending_models.pending(self._pending)
                self._model_policy = prepared.attempt_policy.bind_runtime(pending,
                    deadline=invocation.deadline, cancel=invocation.cancel,
                    registry_provider=self.services.live_model_attempt_policy)
                self._model_policy.validate_runtime(operation.binding, prepared.compiled,
                    clock=self.services.clock, deadline=operation.deadline, cancel=self.cancel)
            self._check(operation.binding, operation.deadline)
        except BaseException:
            self._model_policy = None
            self.services.pending_models.revoke(self._pending)
            raise
        return {'request_id':result['request_id'], 'submission_fingerprint':result['submission_fingerprint'],
                'approval_required':True, 'sent':False, 'expires_in':result['expires_in']}

    def adapters(self):
        result = {}
        for op in {item.op for item in self.plan.operations}:
            result[op] = self._file_read if op.endswith('.read') else self._file_write if op.endswith('.write') else self._http_request if op == 'http.request' else self._model_pending
        return result

    def close(self):
        with self._lock:
            self._closed = True; binding = self.binding; self._model_policy = None
        if binding is not None:
            if self._http is not None: self._http.revoke_binding(binding)
            self.services.pending_models.revoke_binding(binding,cancel_invocation=False)
        for _, _, registry, _ in self.plan.objects: registry.close()
        # Plans/publication records remain host-owned for explicit review; no undo.
