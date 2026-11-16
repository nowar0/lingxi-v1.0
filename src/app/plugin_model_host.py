"""Authenticated plugin-model composition; no import/construction/native/network I/O.

Browser JSON names only server-held host-chat requests and opaque pending handles.
It never supplies a BrokerSession, PID, owner identity, compiled payload or provider
receipt. A prepared model capability is optional and host-only (default None).
Ordinary pending requests keep the child's ORIGINAL deadline/Event; this module
neither waits for a person while suspended nor extends a ten-second invocation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.model_host import CatalogModelRoutes, HostModelRuntime, HostUiPrincipal
from app.model_pending_runtime import RuntimeModelInvocation, RuntimePendingModels
from app.model_submission import CompiledSubmission
from app.plugin_model_api import HostModelApprovals, _BoundedJsonRoute
from app.plugin_model_proxy import HttpxModelTransport, ModelAttemptPolicy, ModelProxyService
from app.plugin_proxy import encode_json
from app.plugin_runtime_brokers import PreparedModelRequest, RuntimeBrokerServices

_SHA = r'^[0-9a-f]{64}$'
_TOKEN = r'^[A-Za-z0-9_-]{32,128}$'
_PLUGIN = r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$'
_NOTICE = ('主机配置声明不等于提供方实测；仅真实宿主持有请求与运行时 policy 允许本次未验证尝试，'
           '不更改 ready/capability_source。未知图片/视频 token 预算保持未知；硬字节限制不因此放宽。'
           '必须另行确认未实测能力及预算风险，不续期、不重试、不切换。')
_FINAL_PREVIEW_FIELDS = (
    'schema','text_sha256','text_characters','attachments','attachment_count','part_count',
    'processed_text_sha256','max_output_tokens','timeout_seconds','capability_source','limits',
    'loss_notes','text','processed_text_parts','provider_id','protocol','model','profile_id',
    'profile_revision','route_fingerprint','capability_fingerprint','payload_sha256','wire_bytes',
    'submission_fingerprint','ready_for_verified_adapter','verification_note','estimated_text_tokens',
    'media_token_estimate','token_counts_are_estimates','requires_separate_explicit_approval',
    'execution_grant_minted','automatic_history_persona_memory_tools','endpoint','max_response_bytes','plain_http',
)


def _fail(code, status=409):
    raise HTTPException(status, {'code':code, 'message':
        '插件模型资料、归属、能力或运行时批准已变化/不可用；没有发送授权、续期或自动重试。'})


def _same(left, right):
    return type(left) is str and type(right) is str and hmac.compare_digest(left, right)


@dataclass(frozen=True)
class HostModelRequestExport:
    """Python-only receipt from a live authenticated HostModelRuntime request."""
    compiled: CompiledSubmission = field(repr=False)
    principal: HostUiPrincipal = field(repr=False)
    scope_id: str
    request_id: str
    session_id: int
    expires: float
    revalidate: object = field(repr=False, compare=False)


def make_model_host_request_exporter(runtime_provider):
    """Explicit host capability, NOT installed by default or accepted from HTTP.

    The embedding host may instead supply a narrower model_host attach callback
    with this contract. This bridge reads the actual held scope/request under its
    own lock and invokes its actual material/route/auth revalidator every time.
    It does not reconstruct CompiledSubmission from browser JSON or recompile it.
    """
    if not callable(runtime_provider):
        raise TypeError('actual shared model host accessor required')

    def export(principal, scope_id, request_id, expected_sha):
        runtime = runtime_provider()
        if type(runtime) is not HostModelRuntime:
            _fail('model_shared_host_runtime_unavailable', 503)
        with runtime._lock:
            scope = runtime._scope(principal, scope_id)
            record = scope.requests.get(request_id)
            if (record is None or record.started or record.finished or record.token is not None
                    or type(record.compiled) is not CompiledSubmission):
                _fail('model_host_unsent_request_required', 404)
            runtime._recheck(scope, record)
            if not _same(record.compiled.payload_sha256, expected_sha):
                _fail('model_submission_changed')
            compiled = record.compiled

            def revalidate(value):
                if runtime_provider() is not runtime:
                    return False
                with runtime._lock:
                    actual_scope = runtime._scope(principal, scope_id)
                    actual = actual_scope.requests.get(request_id)
                    if (actual_scope is not scope or actual is not record
                            or actual.compiled is not compiled or value is not compiled
                            or actual.started or actual.finished or actual.token is not None):
                        return False
                    return runtime._recheck(actual_scope, actual) is True

            return HostModelRequestExport(compiled, principal, scope_id, request_id,
                scope.binding.session_id, min(scope.expires, record.expires), revalidate)
    def list_requests(principal):
        """Only this live authenticated runtime supplies source handles; never mints one."""
        runtime = runtime_provider()
        if type(runtime) is not HostModelRuntime:
            _fail('model_shared_host_runtime_unavailable', 503)
        items = []
        with runtime._lock:
            runtime._principal(principal)
            for scope_id, scope in tuple(runtime._scopes.items()):
                if scope.principal != principal:
                    continue
                try:
                    runtime._scope(principal, scope_id)
                except Exception:
                    continue
                for request_id, record in tuple(scope.requests.items()):
                    if record.started or record.finished or record.token is not None:
                        continue
                    try:
                        runtime._recheck(scope, record)
                    except Exception:
                        continue
                    compiled = record.compiled
                    if type(compiled) is not CompiledSubmission:
                        continue
                    remaining = min(scope.expires, record.expires)-runtime.clock()
                    if remaining <= 0:
                        continue
                    items.append({'scope_id':scope_id, 'request_id':request_id,
                        'submission_sha256':compiled.payload_sha256,
                        'submission_fingerprint':compiled.fingerprint,
                        'profile_id':compiled.route.profile_id, 'provider':compiled.route.provider_id,
                        'model':compiled.route.model, 'expires_in':min(60, remaining),
                        'ready_for_verified_adapter':compiled.ready_for_verified_adapter,
                        'estimated_text_tokens':compiled.estimated_text_tokens,
                        'media_token_estimate':compiled.media_token_estimate,
                        'sent':False, 'send_approval_created':False})
        return {'items':items, 'server_held_model_host_requests_only':True,
            'automatic_retry':False, 'capability_notice':_NOTICE}

    # Optional capability on the same trusted callable; factory/call signature is unchanged.
    export.list_requests = list_requests
    return export


@dataclass
class _Prepared:
    value: PreparedModelRequest = field(repr=False)
    exported: HostModelRequestExport = field(repr=False)
    manager: object = field(repr=False)
    plugin_id: str
    tool: str
    revision: int
    digest: str
    manifest_json: bytes = field(repr=False)
    expires: float
    registered: bool = False


class PreparedModelRegistry:
    """Host-held pre-send capabilities bound to one plugin/tool/full SDK descriptor.

    model_request(source, request) is the trusted RuntimeBrokerServices callback.
    A browser can ask to attach its own server-held host-chat request after an
    explicit preparation confirmation, but cannot register compiled/provider data.
    No exporter means preparation is unavailable, never an invented fallback.
    """
    def __init__(self, *, manager_provider, live_principal, model_host_request_provider=None,
                 clock=time.monotonic, ttl=60, max_records=64):
        if (not all(callable(value) for value in (manager_provider, live_principal, clock))
                or model_host_request_provider is not None and not callable(model_host_request_provider)
                or type(ttl) not in (int, float) or not 0 < ttl <= 60
                or type(max_records) is not int or not 1 <= max_records <= 64):
            raise TypeError('bounded host preparation and real authentication providers required')
        self.manager_provider, self.live_principal = manager_provider, live_principal
        self.model_host_request_provider = model_host_request_provider
        self.clock, self.ttl, self.maximum = clock, ttl, max_records
        self._records, self._bytes = {}, 0
        self._lock = threading.RLock()
        self._closed = False

    def _principal(self, principal):
        try:
            valid = type(principal) is HostUiPrincipal and self.live_principal(principal) == principal
        except Exception:
            valid = False
        if not valid:
            _fail('model_authenticated_ui_session_required', 401)

    def _plugin(self, manager, plugin_id, tool, revision, digest=None):
        current=getattr(manager._db_depth,'connection',None)
        if current is not None:
            # Broker preview runs inside the manager's existing short writer.
            # Reuse its exact snapshot rather than nesting another _db transaction.
            row=manager._row(current,plugin_id,installed=True)
            value=manager._public(current,row)
        else:
            value = manager.get(plugin_id)
        if (type(value) is not dict or value.get('id') != plugin_id
                or value.get('revision') != revision or value.get('installed') is not True
                or value.get('enabled') is not True or value.get('missing')
                or value.get('files_mismatch') or value.get('blocked')
                or digest is not None and value.get('digest') != digest):
            _fail('model_prepared_plugin_changed')
        manifest = value.get('manifest')
        matches = [item for item in (manifest or {}).get('tools', ())
                   if type(item) is dict and item.get('name') == tool]
        if type(manifest) is not dict or len(matches) != 1:
            _fail('model_prepared_tool_permission_required')
        permissions = set(manifest.get('permissions', ())) | set(matches[0].get('permissions', ()))
        if 'model.generate' not in permissions:
            _fail('model_prepared_tool_permission_required')
        return value

    @staticmethod
    def _size(record):
        compiled = record.value.compiled
        return sum(len(value) for value in (compiled.payload_bytes,
            compiled.submission_manifest_bytes, compiled.preview_content_bytes))

    def _purge(self):
        for prompt_id, record in tuple(self._records.items()):
            if self.clock() >= record.expires:
                self._records.pop(prompt_id)
                self._bytes -= self._size(record)

    def _live(self, record, compiled):
        try:
            self._principal(record.exported.principal)
            with self._lock:
                prompt_id = json.loads(record.value.params_json).get('prompt_id')
                if (self._closed or self.clock() >= record.expires or compiled is not record.value.compiled
                        or record.registered and self._records.get(prompt_id) is not record):
                    record.expires=0
                    return False
            if self.manager_provider() is not record.manager:
                record.expires=0
                return False
            current = self._plugin(record.manager, record.plugin_id, record.tool,
                                   record.revision, record.digest)
            valid=(encode_json(current['manifest'],safe=True)==record.manifest_json
                   and record.exported.revalidate(compiled) is True)
            if not valid:record.expires=0
            return valid
        except Exception:
            record.expires=0
            return False

    def metadata(self):
        return {'configured':callable(self.model_host_request_provider),
            'source_listing_configured':callable(getattr(self.model_host_request_provider, 'list_requests', None)),
            'server_held_model_host_requests_only':True, 'browser_compiled_submission':False,
            'preparation_is_not_send_approval':True,
            'host_configured_attempt_supported':callable(self.model_host_request_provider),
            'capability_notice':_NOTICE}

    def sources(self, principal):
        self._principal(principal)
        provider = getattr(self.model_host_request_provider, 'list_requests', None)
        if not callable(provider):
            _fail('model_host_source_listing_not_integrated', 503)
        value = provider(principal)
        keys = ('scope_id','request_id','submission_sha256','submission_fingerprint','profile_id',
            'provider','model','expires_in','ready_for_verified_adapter','estimated_text_tokens',
            'media_token_estimate','sent','send_approval_created')
        if (type(value) is not dict or set(value) != {'items','server_held_model_host_requests_only',
                'automatic_retry','capability_notice'} or type(value['items']) is not list
                or len(value['items']) > 64 or value['server_held_model_host_requests_only'] is not True
                or value['automatic_retry'] is not False or value['capability_notice'] != _NOTICE):
            _fail('model_host_source_contract_invalid', 503)
        items = []
        seen = set()
        for item in value['items']:
            if (type(item) is not dict or set(item) != set(keys) or item['sent'] is not False
                    or item['send_approval_created'] is not False
                    or any(type(item[key]) is not str or not re.fullmatch(_TOKEN, item[key])
                        for key in ('scope_id','request_id'))
                    or any(type(item[key]) is not str or not re.fullmatch(_SHA, item[key])
                        for key in ('submission_sha256','submission_fingerprint'))
                    or any(type(item[key]) is not str or not 0 < len(item[key]) <= 200
                        for key in ('profile_id','provider','model'))
                    or type(item['expires_in']) not in (int, float) or not 0 < item['expires_in'] <= 60
                    or type(item['ready_for_verified_adapter']) is not bool
                    or type(item['estimated_text_tokens']) is not int or not 0 <= item['estimated_text_tokens'] <= 2**53-1
                    or item['media_token_estimate'] is not None and (type(item['media_token_estimate']) is not int
                        or not 0 <= item['media_token_estimate'] <= 2**53-1)
                    or item['request_id'] in seen):
                _fail('model_host_source_contract_invalid', 503)
            seen.add(item['request_id'])
            items.append({key:item[key] for key in keys})
        return {'items':items, 'server_held_model_host_requests_only':True,
            'automatic_retry':False, 'capability_notice':_NOTICE}

    def prepare_model_request(self, principal, *, plugin_id, tool, revision, scope_id,
                              request_id, expected_sha, max_input_tokens, user_confirmed):
        self._principal(principal)
        if user_confirmed is not True:
            _fail('model_prepared_explicit_confirmation_required')
        if not callable(self.model_host_request_provider):
            _fail('model_host_request_export_not_integrated', 503)
        manager = self.manager_provider()
        plugin = self._plugin(manager, plugin_id, tool, revision)
        exported = self.model_host_request_provider(principal, scope_id, request_id, expected_sha)
        if (type(exported) is not HostModelRequestExport or exported.principal != principal
                or exported.scope_id != scope_id or exported.request_id != request_id
                or type(exported.compiled) is not CompiledSubmission or not callable(exported.revalidate)
                or type(exported.session_id) is not int or exported.session_id <= 0
                or not self.clock() < exported.expires <= self.clock()+300
                or not _same(exported.compiled.payload_sha256, expected_sha)):
            _fail('model_actual_host_request_export_required')
        compiled, route = exported.compiled, exported.compiled.route
        # No ready/evidence promotion: only the actual exported request may mint a policy.
        if type(compiled.ready_for_verified_adapter) is not bool:
            _fail('model_actual_host_request_export_required')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}', route.profile_id)
                or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', value)
                       for value in (route.provider_id, route.model))):
            _fail('model_prepared_sdk_route_descriptor_invalid', 422)
        if not 1 <= route.max_output_tokens <= 32768:
            _fail('model_prepared_output_budget_invalid', 422)
        if type(max_input_tokens) is not int or not 1 <= max_input_tokens <= 1000000:
            _fail('model_prepared_input_budget_invalid', 422)
        manifest = json.loads(compiled.submission_manifest_bytes)
        prompt_sha = manifest.get('text_sha256')
        if type(prompt_sha) is not str or not re.fullmatch(_SHA, prompt_sha):
            _fail('model_host_prompt_digest_unavailable')
        prompt_id = secrets.token_hex(16)
        params = {'profile_id':route.profile_id, 'provider':route.provider_id, 'model':route.model,
            'prompt_id':prompt_id, 'prompt_sha256':prompt_sha, 'max_input_tokens':max_input_tokens,
            'max_output_tokens':route.max_output_tokens}
        # Resolve the cyclic closure only after the actual immutable record exists.
        held = []
        def source_live(value):
            return bool(held) and self._live(held[0], value) is True
        unverified = not compiled.ready_for_verified_adapter
        policy = (ModelAttemptPolicy.for_host_request(compiled,
            owner_session_id=principal.owner_session_id, revalidate=source_live) if unverified else None)
        prepared = PreparedModelRequest(compiled, encode_json(params, safe=True),
            principal.owner_session_id, revalidate=source_live,
            allow_host_configured=unverified, attempt_policy=policy)
        record = _Prepared(prepared, exported, manager, plugin_id, tool, revision, plugin['digest'],
            encode_json(plugin['manifest'], safe=True), min(exported.expires, self.clock()+self.ttl))
        held.append(record)
        prepared.validate(params)
        size = self._size(record)
        with self._lock:
            self._purge()
            if self._closed or len(self._records) >= self.maximum or self._bytes+size > 256*1024**2:
                _fail('model_prepared_capacity', 429)
            if prompt_id in self._records:
                _fail('model_prepared_prompt_collision')
            # Replace this owner's older attachment for the exact plugin/tool/route.
            for old_id, old in tuple(self._records.items()):
                if (old.plugin_id == plugin_id and old.tool == tool
                        and old.value.compiled.route.profile_id == route.profile_id
                        and old.exported.principal == principal):
                    self._records.pop(old_id); self._bytes -= self._size(old)
                    old.expires = 0
            self._records[prompt_id] = record; self._bytes += size
            record.registered = True
        return {'plugin_id':plugin_id, 'tool':tool, 'revision':revision, 'params':params,
            'submission_sha256':compiled.payload_sha256, 'submission_fingerprint':compiled.fingerprint,
            'expires_in':max(0, record.expires-self.clock()), 'prepared':True, 'sent':False,
            'approval_required':True, 'final_send_requires_separate_ui_approval':True}

    def model_request(self, source, request):
        """Trusted broker provider; neither source nor this method is an HTTP DTO."""
        if type(source) is not dict or type(request) is not dict or request.get('op') != 'model.generate':
            _fail('model_prepared_binding_required')
        params = request.get('params')
        if type(params) is not dict:
            _fail('model_prepared_descriptor_required')
        with self._lock:
            self._purge()
            record = self._records.get(params.get('prompt_id'))
        # The manager's authenticated request context is host-owned middleware state,
        # not an args/body owner claim. Another UI session cannot reuse this prompt ID.
        from app.ui_auth import current_principal
        if record is not None and current_principal.get() != record.exported.principal:
            _fail('model_prepared_authenticated_owner_required', 404)
        if (record is None or source.get('plugin_id') != record.plugin_id
                or source.get('tool') != record.tool or source.get('revision') != record.revision
                or source.get('digest') != record.digest
                or encode_json(source.get('manifest'), safe=True) != record.manifest_json
                or request.get('target') != record.value.compiled.route.profile_id
                or encode_json(params, safe=True) != record.value.params_json):
            _fail('model_prepared_binding_changed_or_unknown')
        record.value.validate(params)
        return record.value

    def revoke(self, principal, prompt_id):
        self._principal(principal)
        with self._lock:
            record = self._records.get(prompt_id)
            if record is None or record.exported.principal != principal:
                _fail('model_prepared_unknown', 404)
            self._records.pop(prompt_id); self._bytes -= self._size(record)
        # Held plans also observe this revocation, without undoing external IO.
        record.expires = 0
        return {'revoked':True, 'sent_undo_guaranteed':False, 'automatic_retry':False}

    def close(self):
        with self._lock:
            self._closed = True
            for record in self._records.values():
                record.expires = 0
            self._records.clear(); self._bytes = 0


@dataclass(frozen=True)
class PluginModelServices:
    approvals: HostModelApprovals = field(repr=False)
    pending: RuntimePendingModels = field(repr=False)
    clock: object = field(repr=False, compare=False)

    def invocation(self, request_id, owner_session_id):
        value = self.approvals._host_pending(request_id, owner_session_id)
        try:
            actual = self.pending.live_invocation(value.binding)
        except Exception:
            _fail('model_runtime_invocation_changed_or_expired', 404)
        if (type(actual) is not RuntimeModelInvocation or actual.binding != value.binding
                or not _same(actual.owner_session_id, owner_session_id)
                or actual.cancel.is_set() or self.clock() >= actual.deadline):
            _fail('model_runtime_invocation_changed_or_expired', 404)
        return actual


def build_service_provider(manager_provider, *, routes_provider=CatalogModelRoutes,
                           clock=time.monotonic, allow_plain_http=False):
    """Return a LAZY provider, never initialize a manager/native runtime at mount.

    Each manager/broker lifetime receives its own approval service. Credential
    snapshot/load use the SAME real catalog bridge as the shared chat model host;
    only execute may call load_key. Plain HTTP remains closed unless host opted in.
    """
    if (not all(callable(value) for value in (manager_provider, routes_provider, clock))
            or type(allow_plain_http) is not bool):
        raise TypeError('explicit lazy manager/catalog/timing policy required')
    lock, cache = threading.RLock(), []

    def provide():
        manager = manager_provider()
        brokers = getattr(getattr(manager, 'runtime', None), 'broker_services', None)
        if type(brokers) is not RuntimeBrokerServices or type(brokers.pending_models) is not RuntimePendingModels:
            _fail('model_runtime_broker_pending_not_integrated', 503)
        with lock:
            if cache and cache[0] is manager and cache[1] is brokers:
                return cache[2]
            routes = routes_provider()
            if type(routes) is not CatalogModelRoutes:
                _fail('model_actual_catalog_routes_required', 503)
            pending = brokers.pending_models
            # Ten seconds is only an upper bound for handles, NOT a new child budget.
            service = ModelProxyService(route_provider=pending.route_provider,
                authority_provider=pending.live_binding,
                credential_snapshot_provider=routes.snapshot, credential_loader=routes.load_key,
                transport=HttpxModelTransport(clock=clock, allow_plain_http=allow_plain_http),
                clock=clock, ttl=10, allow_plain_http=allow_plain_http,
                attempt_policy_provider=brokers.live_model_attempt_policy)
            approvals = HostModelApprovals(service, pending_provider=pending.pending,
                execution_context_provider=pending.execution_context, clock=clock, ttl=10,
                policy_provider=brokers.model_attempt_policy)
            value = PluginModelServices(approvals, pending, clock)
            cache[:] = [manager, brokers, value]
            return value
    return provide


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PendingReference(Strict):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$')


class Preview(PendingReference):
    expected_submission_sha: str = Field(pattern=_SHA)


class Approval(PendingReference):
    token: str = Field(pattern=_TOKEN)
    fingerprint: str = Field(pattern=_SHA)


class Execute(Approval):
    user_confirmed: bool
    accept_unverified_capabilities: bool = False


class Cancel(PendingReference):
    token: str | None = Field(default=None, pattern=_TOKEN)
    fingerprint: str | None = Field(default=None, pattern=_SHA)


class Prepare(Strict):
    plugin_id: str = Field(pattern=_PLUGIN)
    tool: str = Field(min_length=1, max_length=101)
    expected_revision: int = Field(ge=1)
    scope_id: str = Field(pattern=_TOKEN)
    request_id: str = Field(pattern=_TOKEN)
    expected_submission_sha: str = Field(pattern=_SHA)
    max_input_tokens: int = Field(ge=1, le=1000000)
    user_confirmed: bool


class PreparedRevoke(Strict):
    prompt_id: str = Field(pattern=r'^[0-9a-f]{32}$')


def create_router(*, service_provider, principal_provider, prepared_registry=None):
    """Owner comes exclusively from authenticated host principal, never HTTP claims.

    Mount INSTEAD OF the old inactive plugin_model_api router, not alongside it.
    HostUiAuthMiddleware/local Origin checks remain the embedding host's boundary.
    """
    if (not callable(service_provider) or not callable(principal_provider)
            or prepared_registry is not None and type(prepared_registry) is not PreparedModelRegistry):
        raise TypeError('lazy production services and real UI authentication required')
    router = APIRouter(prefix='/v11/plugin-model-proxy', tags=['plugin-model-proxy'],
                       route_class=_BoundedJsonRoute)

    def principal(request):
        try:
            value = principal_provider(request)
            if type(value) is not HostUiPrincipal:
                raise ValueError('principal')
            return value
        except Exception:
            _fail('model_authenticated_ui_session_required', 401)

    def services():
        try:
            value = service_provider()
        except HTTPException:
            raise
        except Exception:
            _fail('model_host_service_not_integrated', 503)
        if (type(value) is not PluginModelServices or type(value.approvals) is not HostModelApprovals
                or type(value.pending) is not RuntimePendingModels):
            _fail('model_host_service_not_integrated', 503)
        return value

    def response(callback):
        try:
            value = callback()
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(raw) > 8*1024**2:
                _fail('model_preview_response_limit_no_truncation', 413)
            return JSONResponse(value, headers={'Cache-Control':'no-store',
                'X-Content-Type-Options':'nosniff'})
        except HTTPException:
            raise
        except Exception as error:
            code = getattr(error, 'code', '')
            _fail(code if type(code) is str and re.fullmatch(r'model_[a-z0-9_]{1,100}', code)
                  else 'model_host_operation_unconfirmed', 409 if getattr(error, 'code', None) else 503)

    def handle(current, body, owner):
        # Correlate token to this request before public execute/cancel consumes it.
        # Approval storage is host-owned; no browser owner/PID claim is consulted.
        key = hashlib.sha256(body.token.encode('ascii')).hexdigest()
        with current.approvals._lock:
            record = current.approvals._pending.get(key) or current.approvals._active.get(key)
            if (record is None or record.pending.request_id != body.request_id
                    or not _same(record.pending.owner_session_id, owner.owner_session_id)
                    or not _same(record.fingerprint, body.fingerprint)):
                _fail('model_api_approval_unknown', 404)

    @router.get('')
    def metadata(request:Request):
        principal(request)
        def read():
            value = services().approvals.metadata()
            keys = ('configured','host_pending_requests_only','execution_context_configured',
                'host_configured_attempt_supported','unverified_capabilities_require_separate_final_confirmation',
                'approval_required','automatic_context','automatic_retry','os_isolation_verified_by_api')
            if type(value) is not dict or set(value) != set(keys):
                _fail('model_host_metadata_contract_invalid', 503)
            return {**{key:value[key] for key in keys},
                'owner_from_authenticated_principal':True, 'original_runtime_deadline':True,
                'capability_notice':_NOTICE,
                'preparation':prepared_registry.metadata() if prepared_registry is not None else {'configured':False}}
        return response(read)

    @router.get('/pending/{request_id}')
    def pending(request_id:str, request:Request):
        owner = principal(request)
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_id) or request.query_params:
            _fail('model_pending_unavailable', 404)
        def read():
            current = services()
            invocation = current.invocation(request_id, owner.owner_session_id)
            value = current.approvals.pending_metadata(request_id, owner.owner_session_id)
            keys = ('request_id','submission_sha256','submission_fingerprint','approval_required','sent')
            if type(value) is not dict or set(value) != set(keys):
                _fail('model_host_pending_contract_invalid', 503)
            return {**{key:value[key] for key in keys},
                'expires_in':max(0, invocation.deadline-current.clock()),
                'original_runtime_deadline':True}
        return response(read)

    @router.post('/preview')
    def preview(body:Preview, request:Request):
        owner = principal(request)
        def propose():
            current = services()
            invocation = current.invocation(body.request_id, owner.owner_session_id)
            value = current.approvals.preview(body.request_id, owner.owner_session_id,
                                               body.expected_submission_sha)
            try:
                remaining = invocation.deadline-current.clock()
                if remaining <= 0:
                    _fail('model_runtime_invocation_changed_or_expired', 404)
                actual = current.approvals._host_pending(body.request_id, owner.owner_session_id)
                keys = {'request_id','preview','token','fingerprint','approval_required','sent',
                    'requires_unverified_capabilities_acceptance','unverified_capabilities_confirmation_field',
                    'final_send_requires_separate_ui_approval'}
                has_token = type(value) is dict and value.get('token') is not None
                keys.add('expires_in' if has_token else 'blocked')
                public = value.get('preview') if type(value) is dict else None
                expected = {**actual.compiled.preview_to_ui(), 'endpoint':actual.compiled.route.endpoint,
                    'max_response_bytes':current.approvals.service.max_response_bytes,
                    'plain_http':actual.compiled.route.endpoint.startswith('http:')}
                supported = current.approvals.metadata().get('host_configured_attempt_supported')
                if (type(value) is not dict or set(value) != keys or type(public) is not dict
                        or set(public) != set(_FINAL_PREVIEW_FIELDS) or public != expected
                        or value['request_id'] != body.request_id or value['sent'] is not False
                        or type(supported) is not bool
                        or type(value['requires_unverified_capabilities_acceptance']) is not bool
                        or value['requires_unverified_capabilities_acceptance'] != (not actual.compiled.ready_for_verified_adapter)
                        or value['unverified_capabilities_confirmation_field'] != 'accept_unverified_capabilities'
                        or value['final_send_requires_separate_ui_approval'] is not True):
                    _fail('model_host_preview_contract_invalid', 503)
                if has_token:
                    if (type(value['token']) is not str or not re.fullmatch(_TOKEN, value['token'])
                            or type(value['fingerprint']) is not str or not re.fullmatch(_SHA, value['fingerprint'])
                            or value['approval_required'] is not True
                            or type(value['expires_in']) not in (int, float) or not 0 < value['expires_in'] <= 60):
                        _fail('model_host_preview_contract_invalid', 503)
                elif (value['fingerprint'] is not None or value['approval_required'] is not False
                        or type(value['blocked']) is not str or not re.fullmatch(r'model_[a-z0-9_]{1,100}', value['blocked'])):
                    _fail('model_host_preview_contract_invalid', 503)
                result = {key:value[key] for key in keys}
                result['preview'] = {key:public[key] for key in _FINAL_PREVIEW_FIELDS}
                result.update(expires_in=min(value.get('expires_in', remaining), remaining),
                    original_runtime_deadline=True, host_configured_attempt_supported=supported,
                    capability_notice=_NOTICE)
                return result
            except BaseException:
                if type(value) is dict and value.get('token') is not None:
                    current.approvals.cancel(value['token'], value.get('fingerprint'), owner.owner_session_id)
                raise
        return response(propose)

    @router.post('/execute')
    async def execute(body:Execute, request:Request):
        owner = principal(request)
        current = services()
        handle(current, body, owner)
        current.invocation(body.request_id, owner.owner_session_id)
        task = asyncio.create_task(current.approvals.execute(body.token, body.fingerprint,
            owner.owner_session_id, body.user_confirmed,
            accept_unverified_capabilities=body.accept_unverified_capabilities))
        try:
            while not task.done():
                if await request.is_disconnected():
                    try:
                        current.approvals.cancel(body.token, body.fingerprint, owner.owner_session_id)
                    except Exception:
                        pass
                    current.pending.revoke(body.request_id)
                    task.cancel()
                    _fail('model_ui_disconnected_result_unknown')
                await asyncio.wait((task,), timeout=.05)
            try:
                value = await task
            except Exception as error:
                return response(lambda:(_ for _ in ()).throw(error))
            return response(lambda:value)
        finally:
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=.25)
            except BaseException:
                pass

    @router.post('/cancel')
    def cancel(body:Cancel, request:Request):
        owner = principal(request)
        def revoke():
            current = services()
            if (body.token is None) != (body.fingerprint is None):
                _fail('model_api_confirmation_invalid', 422)
            if body.token is not None:
                handle(current, body, owner)
                result = current.approvals.cancel(body.token, body.fingerprint, owner.owner_session_id)
            else:
                current.approvals._host_pending(body.request_id, owner.owner_session_id)
                result = {'cancel_requested':True, 'active_execution':False,
                    'sent_undo_guaranteed':False, 'automatic_retry':False}
            keys = {'cancel_requested','active_execution','sent_undo_guaranteed','automatic_retry'}
            if body.token is not None:
                keys.add('notice')
            if (type(result) is not dict or set(result) != keys or result['cancel_requested'] is not True
                    or type(result['active_execution']) is not bool or result['sent_undo_guaranteed'] is not False
                    or result['automatic_retry'] is not False
                    or body.token is not None and type(result['notice']) is not str):
                current.pending.revoke(body.request_id)
                _fail('model_host_cancel_contract_invalid', 503)
            current.pending.revoke(body.request_id)
            return {**{key:result[key] for key in keys}, 'request_id':body.request_id}
        return response(revoke)

    @router.get('/prepared/sources')
    def prepared_sources(request:Request):
        owner = principal(request)
        if request.query_params:
            _fail('model_host_source_contract_invalid', 422)
        def read():
            if prepared_registry is None:
                _fail('model_host_request_export_not_integrated', 503)
            return prepared_registry.sources(owner)
        return response(read)

    @router.post('/prepared/prepare')
    def prepare(body:Prepare, request:Request):
        owner = principal(request)
        def attach():
            if prepared_registry is None:
                _fail('model_host_request_export_not_integrated', 503)
            return prepared_registry.prepare_model_request(owner, plugin_id=body.plugin_id, tool=body.tool,
                revision=body.expected_revision, scope_id=body.scope_id, request_id=body.request_id,
                expected_sha=body.expected_submission_sha, max_input_tokens=body.max_input_tokens,
                user_confirmed=body.user_confirmed)
        return response(attach)

    @router.post('/prepared/revoke')
    def revoke_prepared(body:PreparedRevoke, request:Request):
        owner = principal(request)
        def revoke():
            if prepared_registry is None:
                _fail('model_host_request_export_not_integrated', 503)
            return prepared_registry.revoke(owner, body.prompt_id)
        return response(revoke)

    return router
