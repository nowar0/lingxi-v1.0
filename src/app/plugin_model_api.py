"""Host-UI-only model approval API; defaults inactive, never accepts plugin identities.

The embedding host MUST authenticate its UI session before mounting this router.
owner_session_id is correlation, not a replacement for session/Origin authentication.
HostPendingModelRequest and HostModelExecution are trusted Python contracts, NOT OS
attestation: only the real runtime may supply them. Import/construction performs no
I/O, configuration/Key discovery, context expansion, process launch or network call.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.model_submission import CompiledSubmission
from app.plugin_model_proxy import ModelAttemptPolicy, ModelProxyError, ModelProxyService
from app.plugin_proxy import BrokerSession

MAX_REQUEST_BYTES = 16 * 1024
MAX_API_APPROVALS = 64
MAX_BOUND_BYTES = 256 * 1024**2
_ID = re.compile(r'^[A-Za-z0-9_-]{16,128}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_BUDGETS = {'ordinary': 10, 'mcp': 30}


def _fail(code, status=409):
    raise HTTPException(status, {'code': code, 'message': '模型代理批准无效、已变化或结果未确认；没有自动重试'})


def _ident(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


@dataclass(frozen=True)
class HostPendingModelRequest:
    """Created by a trusted host registry/runtime, never parsed from browser JSON."""
    request_id: str
    binding: BrokerSession = field(repr=False)
    compiled: CompiledSubmission = field(repr=False)
    owner_session_id: str = field(repr=False)

    def __post_init__(self):
        if (not _ident(self.request_id) or not _ident(self.owner_session_id)
                or type(self.binding) is not BrokerSession
                or type(self.compiled) is not CompiledSubmission):
            raise ValueError('exact host-owned pending request required')


@dataclass(frozen=True)
class HostModelExecution:
    """Actual call's deadline/Event, obtained at execution, not started by preview.

    The host provider must reserve/observe the approved invocation's real budget.
    This API never creates an Event or adds time to an existing deadline. A Python
    source label is not proof that a pipe/process/token was actually inspected.
    """
    request_id: str
    binding: BrokerSession = field(repr=False)
    owner_session_id: str = field(repr=False)
    deadline: float
    cancel: threading.Event = field(repr=False)
    mode: str = 'ordinary'
    source: str = 'unverified'

    def __post_init__(self):
        if (not _ident(self.request_id) or not _ident(self.owner_session_id)
                or type(self.binding) is not BrokerSession
                or type(self.deadline) not in (int, float) or not math.isfinite(self.deadline)
                or not isinstance(self.cancel, threading.Event) or self.mode not in _BUDGETS
                or self.source != 'host_runtime'):
            raise ValueError('actual host execution context required')


@dataclass
class _Approval:
    pending: HostPendingModelRequest = field(repr=False)
    token: str = field(repr=False)
    fingerprint: str
    expires: float
    size: int
    context: HostModelExecution | None = field(default=None, repr=False)
    cancel_requested: bool = False
    attempt_policy: ModelAttemptPolicy | None = field(default=None, repr=False)


class HostModelApprovals:
    """Explicit host wiring only; no registry/path/PID/credential input from HTTP.

    pending_provider(request_id) must freshly validate runtime ownership and return
    its immutable finalized submission. execution_context_provider(pending) must
    return that invocation's own budget and cancellation Event. Optional
    policy_provider(pending) resolves only this actual server pending's typed
    configured-attempt capability; it cannot construct one from an HTTP flag.
    Trusted synchronous providers must be bounded; this layer cannot kill a hung
    Python callback.
    """
    def __init__(self, service, *, pending_provider, execution_context_provider=None,
                 clock=time.monotonic, ttl=60, max_approvals=MAX_API_APPROVALS, policy_provider=None):
        if (type(service) is not ModelProxyService or not callable(pending_provider)
                or execution_context_provider is not None and not callable(execution_context_provider)
                or policy_provider is not None and not callable(policy_provider)
                or not callable(clock)):
            raise TypeError('explicit model service and host pending provider required')
        if (type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= 60
                or type(max_approvals) is not int or not 1 <= max_approvals <= MAX_API_APPROVALS):
            raise ValueError('bounded API approvals required')
        self.service, self.pending_provider = service, pending_provider
        self.execution_context_provider = execution_context_provider
        self.policy_provider = policy_provider
        self.clock, self.ttl, self.maximum = clock, ttl, max_approvals
        self._pending, self._active = {}, {}
        self._lock = threading.RLock()

    def metadata(self):
        return {'configured': True, 'host_pending_requests_only': True,
                'execution_context_configured': self.execution_context_provider is not None,
                'host_configured_attempt_supported': self.policy_provider is not None
                    and self.service.attempt_policy_provider is not None,
                'unverified_capabilities_require_separate_final_confirmation': True,
                'approval_required': True, 'automatic_context': False,
                'automatic_retry': False, 'os_isolation_verified_by_api': False}

    def _host_pending(self, request_id, owner_session_id):
        try:
            value = self.pending_provider(request_id)
        except Exception:
            _fail('model_pending_unavailable', 404)
        if (type(value) is not HostPendingModelRequest or value.request_id != request_id
                or not hmac.compare_digest(value.owner_session_id, owner_session_id)):
            _fail('model_pending_unavailable', 404)
        return value

    def _policy(self, pending):
        if self.policy_provider is None:
            return None
        try:
            policy = self.policy_provider(pending)
            if policy is None:
                if pending.compiled.ready_for_verified_adapter is not True:
                    _fail('model_host_attempt_capability_required')
                return None
            if (type(policy) is not ModelAttemptPolicy or policy.pending is not pending
                    or policy.compiled is not pending.compiled
                    or policy.owner_session_id != pending.owner_session_id):
                _fail('model_host_attempt_capability_required')
            policy.validate_runtime(pending.binding, pending.compiled, clock=self.clock)
            return policy
        except HTTPException:
            raise
        except ModelProxyError as error:
            _fail(_safe_service_code(error))
        except Exception:
            _fail('model_host_attempt_capability_revoked')

    def _recheck(self, record):
        expected = record.pending
        current = self._host_pending(expected.request_id, expected.owner_session_id)
        if current.binding != expected.binding:
            _fail('model_pending_changed')
        if record.attempt_policy is not None:
            # Configured capabilities retain the exact frozen compiled object;
            # do not repeatedly hash a large media payload while polling live IO.
            if current is not expected or current.compiled is not expected.compiled:
                _fail('model_pending_changed')
        elif (current.compiled.fingerprint != expected.compiled.fingerprint
                or current.compiled.payload_sha256 != expected.compiled.payload_sha256):
            _fail('model_pending_changed')
        policy = self._policy(current)
        if policy is not record.attempt_policy:
            _fail('model_host_attempt_capability_changed')
        return current

    def _purge_locked(self):
        for key, record in tuple(self._pending.items()):
            if self.clock() >= record.expires:
                self._pending.pop(key)
                self.service.cancel_approval(record.token)

    def pending_metadata(self, request_id, owner_session_id):
        value = self._host_pending(request_id, owner_session_id)
        return {'request_id': request_id, 'submission_sha256': value.compiled.payload_sha256,
                'submission_fingerprint': value.compiled.fingerprint,
                'approval_required': True, 'sent': False}

    def preview(self, request_id, owner_session_id, expected_submission_sha):
        value = self._host_pending(request_id, owner_session_id)
        if not hmac.compare_digest(value.compiled.payload_sha256, expected_submission_sha):
            _fail('model_submission_changed')
        size = sum(len(item) for item in (value.compiled.payload_bytes,
                   value.compiled.submission_manifest_bytes, value.compiled.preview_content_bytes))
        with self._lock:
            self._purge_locked()
            if (len(self._pending)+len(self._active) >= self.maximum
                    or size+sum(item.size for item in (*self._pending.values(), *self._active.values())) > MAX_BOUND_BYTES):
                _fail('model_api_capacity', 429)
            # A configured attempt is authorized only by the actual server registry,
            # never by a browser bool. Preview still loads no Key and sends nothing.
            policy = self._policy(value)
            proposed = self.service.preview_compiled(value.binding, value.compiled,
                allow_host_configured_attempt=policy is not None, attempt_policy=policy)
            requires_unverified = not value.compiled.ready_for_verified_adapter
            acceptance = {'requires_unverified_capabilities_acceptance':requires_unverified,
                'unverified_capabilities_confirmation_field':'accept_unverified_capabilities',
                'final_send_requires_separate_ui_approval':True}
            token = proposed.get('token')
            if token is None:
                return {**proposed, **acceptance, 'request_id': request_id}
            try:
                if (type(token) is not str or not _TOKEN.fullmatch(token)
                        or type(proposed.get('fingerprint')) is not str
                        or not _SHA.fullmatch(proposed['fingerprint'])
                        or type(proposed.get('expires_in')) not in (int, float)
                        or not 0 < proposed['expires_in'] <= 60):
                    _fail('model_service_preview_invalid', 503)
                record = _Approval(value, token, proposed['fingerprint'], 0, size, attempt_policy=policy)
                self._recheck(record)
                expires = self.clock()+min(self.ttl, proposed['expires_in'])
                if policy is not None:
                    expires = min(expires, policy.deadline)
                if expires <= self.clock():
                    _fail('model_execution_deadline_invalid')
                record.expires = expires
                self._pending[_digest(token)] = record
            except BaseException:
                self.service.cancel_approval(token)
                raise
            return {**proposed, **acceptance, 'request_id': request_id,
                    'expires_in': max(0, expires-self.clock())}

    def _take(self, token, fingerprint, owner_session_id):
        key = _digest(token)
        with self._lock:
            self._purge_locked()
            record = self._pending.get(key)
            if record is None or not hmac.compare_digest(record.pending.owner_session_id, owner_session_id):
                _fail('model_api_approval_unknown', 404)
            # Only the correct owner can consume this handle. A malformed attempt
            # by that owner cannot leave a reusable approval behind.
            self._pending.pop(key)
            if not hmac.compare_digest(record.fingerprint, fingerprint):
                self.service.cancel_approval(record.token)
                _fail('model_api_confirmation_invalid')
            self._active[key] = record
            return key, record

    async def execute(self, token, fingerprint, owner_session_id, user_confirmed, *,
                      accept_unverified_capabilities=False):
        if self.execution_context_provider is None:
            _fail('model_execution_context_unconfigured', 503)
        key, record = self._take(token, fingerprint, owner_session_id)
        with self._lock:
            self._active[key] = record
        try:
            if user_confirmed is not True:
                _fail('model_api_confirmation_required')
            if type(accept_unverified_capabilities) is not bool:
                _fail('model_api_confirmation_invalid')
            if record.attempt_policy is not None and accept_unverified_capabilities is not True:
                _fail('model_unverified_capabilities_confirmation_required')
            current = self._recheck(record)
            try:
                context = self.execution_context_provider(current)
            except Exception:
                _fail('model_execution_context_unavailable', 503)
            if (type(context) is not HostModelExecution or context.request_id != current.request_id
                    or context.binding != current.binding or context.owner_session_id != current.owner_session_id):
                _fail('model_execution_context_changed')
            now = self.clock()
            if not now < context.deadline <= now+_BUDGETS[context.mode]:
                _fail('model_execution_deadline_invalid')
            if record.attempt_policy is not None:
                policy = record.attempt_policy
                if context.deadline != policy.deadline or context.cancel is not policy.cancel:
                    _fail('model_host_attempt_runtime_changed')
                policy.validate_runtime(current.binding, current.compiled, clock=self.clock,
                    deadline=context.deadline, cancel=context.cancel)
            with self._lock:
                record.context = context
                if record.cancel_requested:
                    context.cancel.set()
            self._recheck(record)
            def revalidate(_material_boundary):
                self._recheck(record)
                if record.cancel_requested or context.cancel.is_set() or self.clock() >= context.deadline:
                    return False
                return True
            result = await self.service.execute(token, fingerprint, user_confirmed=True,
                deadline=min(context.deadline, self.clock()+current.compiled.route.timeout_seconds),
                cancel=context.cancel, accept_unverified_capabilities=accept_unverified_capabilities,
                revalidate=revalidate)
            self._recheck(record)
            if context.cancel.is_set():
                _fail('model_execution_cancelled')
            return _result(result)
        except ModelProxyError as error:
            _fail(_safe_service_code(error), 409)
        except HTTPException:
            raise
        except asyncio.CancelledError:
            if record.context is not None:
                record.context.cancel.set()
            raise
        except Exception:
            _fail('model_execution_unknown', 502)
        finally:
            self.service.cancel_approval(record.token)
            with self._lock:
                self._active.pop(key, None)

    def cancel(self, token, fingerprint, owner_session_id):
        key = _digest(token)
        with self._lock:
            self._purge_locked()
            record = self._pending.get(key) or self._active.get(key)
            if (record is None or not hmac.compare_digest(record.pending.owner_session_id, owner_session_id)
                    or not hmac.compare_digest(record.fingerprint, fingerprint) or record.cancel_requested):
                _fail('model_api_approval_unknown', 404)
            active = key in self._active
            self._pending.pop(key, None)
            record.cancel_requested = True
            if record.context is not None:
                record.context.cancel.set()
            self.service.cancel_approval(record.token)
            return {'cancel_requested': True, 'active_execution': active, 'sent_undo_guaranteed': False,
                    'automatic_retry': False, 'notice': '已撤销批准或请求取消；已发送的外部效果不能撤回，不自动重试'}


def _safe_service_code(error):
    code = getattr(error, 'code', '')
    return code if type(code) is str and re.fullmatch(r'model_[a-z0-9_]{1,80}', code) else 'model_service_refused'


def _result(value):
    if (type(value) is not dict or set(value) != {'answer', 'usage', 'finish'}
            or type(value['answer']) is not str or len(value['answer'].encode('utf-8')) > 1024*1024
            or '\x00' in value['answer'] or type(value['finish']) is not str
            or value['finish'] not in ('completed', 'incomplete', 'refused', 'blocked', 'length', 'tool_requested')
            or type(value['usage']) is not dict
            or set(value['usage']) != {'input_tokens', 'output_tokens', 'total_tokens'}):
        _fail('model_result_invalid', 502)
    for item in value['usage'].values():
        if item is not None and (type(item) is not int or not 0 <= item <= 2**53-1):
            _fail('model_result_invalid', 502)
    return value


class _BoundedJsonRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request: Request):
            if request.method == 'POST':
                if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    _fail('model_json_required', 415)
                declared = request.headers.get('content-length')
                if declared is not None and (not declared.isascii() or not declared.isdecimal() or int(declared) > MAX_REQUEST_BYTES):
                    _fail('model_request_limit', 413)
                raw = bytearray()
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            raw.extend(chunk)
                            if len(raw) > MAX_REQUEST_BYTES:
                                _fail('model_request_limit', 413)
                except TimeoutError:
                    _fail('model_request_timeout', 408)
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate')
                        result[key] = value
                    return result
                try:
                    document = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
                    if type(document) is not dict:
                        raise ValueError('object required')
                except (ValueError, UnicodeError, RecursionError):
                    _fail('model_json_invalid', 422)
                request._body = bytes(raw)
            return await original(request)
        return handler


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Preview(Strict):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$')
    owner_session_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$')
    expected_submission_sha: str = Field(pattern=r'^[0-9a-f]{64}$')


class Approval(Strict):
    token: str = Field(pattern=r'^[A-Za-z0-9_-]{32,128}$')
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    owner_session_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$')


class Execute(Approval):
    user_confirmed: bool
    accept_unverified_capabilities: bool = False


def create_router(approvals=None,principal_provider=None):
    if approvals is not None and type(approvals) is not HostModelApprovals:
        raise TypeError('host model approval wiring required')
    router = APIRouter(prefix='/v11/plugin-model-proxy', tags=['plugin-model-proxy'], route_class=_BoundedJsonRoute)
    def owner(request,claimed=None):
        if principal_provider is None:
            return claimed
        principal=principal_provider(request)
        actual=principal.owner_session_id
        if claimed is not None and not hmac.compare_digest(actual,claimed):
            _fail('model_pending_unavailable',404)
        return actual
    def configured():
        if approvals is None:
            _fail('model_proxy_not_configured', 503)
        return approvals
    def invoke(callback):
        try:
            return callback()
        except ModelProxyError as error:
            _fail(_safe_service_code(error))
        except HTTPException:
            raise
        except Exception:
            _fail('model_host_request_unavailable', 503)
    @router.get('')
    def metadata(request:Request):
        owner(request)
        return configured().metadata()
    @router.get('/pending/{request_id}')
    def pending(request_id: str, owner_session_id: str, request:Request):
        if not _ident(request_id) or not _ident(owner_session_id):
            _fail('model_pending_unavailable', 404)
        actual=owner(request,owner_session_id)
        return invoke(lambda:configured().pending_metadata(request_id,actual))
    @router.post('/preview')
    def preview(body: Preview,request:Request):
        actual=owner(request,body.owner_session_id)
        return invoke(lambda:configured().preview(body.request_id,actual,body.expected_submission_sha))
    @router.post('/execute')
    async def execute(body: Execute,request:Request):
        actual=owner(request,body.owner_session_id)
        return await configured().execute(body.token,body.fingerprint,actual,body.user_confirmed,
            accept_unverified_capabilities=body.accept_unverified_capabilities)
    @router.post('/cancel')
    def cancel(body: Approval,request:Request):
        actual=owner(request,body.owner_session_id)
        return invoke(lambda:configured().cancel(body.token,body.fingerprint,actual))
    return router
