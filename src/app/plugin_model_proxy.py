"""Explicit host-approved model proxy; not a chat entry, broker RPC or OS sandbox.

Only immutable, finalized payloads are sent. No history/persona/memory/tools, SDK
conversion, provider fallback, retry, default configuration or credential discovery.
BrokerSession remains the default strict authority. A host-held typed attempt
policy may bind one exact configured request to its actual running broker child;
HostChatScope retains its separately composed attempt path. Both require separate
user acknowledgement and never promote configured evidence to verified evidence.
Providers must be trusted, fast host callbacks. This module cannot forcibly stop a
hung synchronous provider or OS resolver; native call ownership remains the host's.
Credential generations cover Lingxi writes, not external OS Keyring replacements.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from app.model_submission import CompiledSubmission, HostChatScope, RouteBinding, MAX_WIRE_BYTES
from app.plugin_proxy import BrokerSession

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024  # Bounded exact 5000-part document manifests; not provider output.
MAX_PENDING = 128
MAX_PENDING_BYTES = 256 * 1024**2
_HEX = re.compile(r'^[0-9a-f]{64}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')


class ModelProxyError(ValueError):
    """Fixed safe codes only. No raw transport/provider/credential error text."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise ModelProxyError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail('model_invalid_json')


def _json(raw, maximum=MAX_RESPONSE_BYTES, *, max_nodes=32768):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        _fail('model_json_limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail('model_duplicate_json')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: _fail('model_nonfinite_json'))
    except (UnicodeError, ValueError, RecursionError):
        _fail('model_invalid_json')
    stack = [(value, 0)]; count = 0
    while stack:
        item, depth = stack.pop(); count += 1
        if depth > 32 or count > max_nodes:
            _fail('model_json_complexity')
        if type(item) is dict:
            stack.extend((child, depth+1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth+1) for child in item)
        elif type(item) is float and not math.isfinite(item):
            _fail('model_nonfinite_json')
    return value


def _endpoint(route, allow_plain_http=False):
    try:
        url = urlsplit(route.endpoint)
        port = url.port
    except ValueError:
        _fail('model_endpoint_invalid')
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password
            or url.query or url.fragment or '\\' in route.endpoint
            or any(c.isspace() or ord(c) < 32 for c in route.endpoint)
            or port is not None and not 1 <= port <= 65535):
        _fail('model_endpoint_invalid')
    if url.scheme == 'http' and not allow_plain_http:
        _fail('model_plain_http_not_approved')
    return route.endpoint


def _snapshot(value, route):
    if (type(value) is not dict or set(value) != {'authority', 'generation', 'status', 'metadata_fingerprint'}
            or value['authority'] != route.credential_authority
            or type(value['generation']) is not int or value['generation'] != route.credential_generation
            or value['status'] != 'ready' or type(value['metadata_fingerprint']) is not str
            or not _HEX.fullmatch(value['metadata_fingerprint'])):
        _fail('model_credential_changed')
    return dict(value)


def _checked_compiled(compiled):
    if type(compiled) is not CompiledSubmission or type(compiled.route) is not RouteBinding:
        _fail('model_finalized_submission_required')
    RouteBinding(**compiled.route.private_binding())
    if type(compiled.ready_for_verified_adapter) is not bool:
        _fail('model_unverified_submission')
    payload = _json(compiled.payload_bytes, MAX_WIRE_BYTES, max_nodes=200000)
    manifest = _json(compiled.submission_manifest_bytes, MAX_MANIFEST_BYTES, max_nodes=200000)
    preview = _json(compiled.preview_content_bytes, MAX_MANIFEST_BYTES, max_nodes=200000)
    if (type(payload) is not dict or type(manifest) is not dict or type(preview) is not dict
            or manifest.get('schema') != 'lingxi.model-submission.v1'
            or manifest.get('max_output_tokens') != compiled.route.max_output_tokens
            or manifest.get('timeout_seconds') != compiled.route.timeout_seconds
            or manifest.get('capability_source') not in ('host_configured', 'verified_provider')
            or compiled.ready_for_verified_adapter != (manifest['capability_source'] == 'verified_provider')
            or type(compiled.capability_fingerprint) is not str or not _HEX.fullmatch(compiled.capability_fingerprint)):
        _fail('model_submission_binding')
    # A trusted compiler is still required; forged Python receipts are not parser or
    # provider evidence. Refuse tool/history additions even if a caller bypassed it.
    if any(key in payload for key in ('tools', 'functions', 'system', 'conversation', 'previous_response_id')):
        _fail('model_payload_expansion_forbidden')
    protocol = compiled.route.protocol
    if protocol == 'gemini_content':
        if (set(payload) != {'contents', 'generationConfig'} or type(payload['generationConfig']) is not dict
                or payload['generationConfig'].get('maxOutputTokens') != compiled.route.max_output_tokens):
            _fail('model_payload_binding')
        messages = payload['contents']
    else:
        if payload.get('model') != compiled.route.model or payload.get('stream') is not False:
            _fail('model_payload_binding')
        if protocol == 'openai_responses':
            if payload.get('max_output_tokens') != compiled.route.max_output_tokens or payload.get('store') is not False:
                _fail('model_payload_binding')
            messages = payload.get('input')
        else:
            if payload.get('max_tokens') != compiled.route.max_output_tokens:
                _fail('model_payload_binding')
            messages = payload.get('messages')
    if type(messages) is not list or len(messages) != 1 or type(messages[0]) is not dict or messages[0].get('role') != 'user':
        _fail('model_implicit_context_forbidden')
    return compiled


_ATTEMPT_POLICY_SEAL = object()


@dataclass(frozen=True)
class ModelAttemptPolicy:
    """Host-only configured-attempt capability, NEVER an SDK/browser DTO.

    for_host_request is called only by the real authenticated export registry,
    after retaining its actual compiled request and source revalidator. Runtime
    binding adds the SAME pending object, native BrokerSession, original deadline
    and cancellation Event. A proxy additionally requires its own trusted registry
    to return that exact bound capability; a bool or another Python receipt is not
    a substitute. This is configuration consent, not provider/OS verification.
    """
    compiled: CompiledSubmission = field(repr=False)
    owner_session_id: str = field(repr=False)
    source_revalidate: object = field(repr=False, compare=False)
    material_fingerprint: str
    _fingerprint: str = field(repr=False)
    source: object = field(default=None, repr=False, compare=False)
    pending: object = field(default=None, repr=False, compare=False)
    deadline: float | None = None
    cancel: object = field(default=None, repr=False, compare=False)
    registry_provider: object = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if (self._seal is not _ATTEMPT_POLICY_SEAL
                or type(self.compiled) is not CompiledSubmission
                or type(self.owner_session_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', self.owner_session_id)
                or not callable(self.source_revalidate)
                or type(self.material_fingerprint) is not str
                or not _HEX.fullmatch(self.material_fingerprint)
                or type(self._fingerprint) is not str or not _HEX.fullmatch(self._fingerprint)):
            _fail('model_host_attempt_capability_required')

    @staticmethod
    def _materials(compiled):
        return _sha(_encoded({'compiled':compiled.fingerprint,
            'route':compiled.route.fingerprint, 'payload':compiled.payload_sha256,
            'manifest':_sha(compiled.submission_manifest_bytes),
            'preview':_sha(compiled.preview_content_bytes),
            'capability':compiled.capability_fingerprint,
            'text_tokens':compiled.estimated_text_tokens,
            'media_tokens':compiled.media_token_estimate,
            'ready_for_verified_adapter':compiled.ready_for_verified_adapter}))

    @classmethod
    def for_host_request(cls, compiled, *, owner_session_id, revalidate):
        """Only the server export registry may mint this source capability.

        revalidate(compiled) MUST check the actual authenticated owner, original
        material versions/hashes and selected route/capability/key generation.
        The factory cannot establish that provenance on behalf of its caller.
        """
        _checked_compiled(compiled)
        if compiled.ready_for_verified_adapter is not False:
            _fail('model_host_configured_source_required')
        # The held registry record may close over this capability. Validate its
        # live callback at first preparation/use, after that cycle is installed.
        materials = cls._materials(compiled)
        identity = _sha(_encoded({'materials':materials, 'owner':owner_session_id}))
        return cls(compiled, owner_session_id, revalidate, materials, identity,
                   _seal=_ATTEMPT_POLICY_SEAL)

    def validate_source(self, compiled, owner_session_id):
        root = self if self.source is None else self.source
        if (type(root) is not ModelAttemptPolicy or root.source is not None
                or compiled is not self.compiled or compiled is not root.compiled
                or self.owner_session_id != owner_session_id or root.owner_session_id != owner_session_id
                or self.material_fingerprint != root.material_fingerprint
                or compiled.ready_for_verified_adapter is not False):
            _fail('model_host_attempt_source_changed')
        try:
            if root.source_revalidate(compiled) is not True:
                _fail('model_host_attempt_source_changed')
        except ModelProxyError:
            raise
        except Exception:
            _fail('model_host_attempt_source_changed')
        return True

    def bind_runtime(self, pending, *, deadline, cancel, registry_provider):
        """Called by RuntimeBrokerServices only after actual pending registration."""
        from app.plugin_model_api import HostPendingModelRequest
        self.validate_source(self.compiled, self.owner_session_id)
        if (self.source is not None or self.pending is not None
                or type(pending) is not HostPendingModelRequest
                or pending.compiled is not self.compiled
                or pending.owner_session_id != self.owner_session_id
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not isinstance(cancel, threading.Event) or cancel.is_set()
                or not callable(registry_provider)):
            _fail('model_host_attempt_runtime_binding_required')
        identity = _sha(_encoded({'source':self.fingerprint,
            'request_id':pending.request_id, 'binding':pending.binding.wire(), 'deadline':deadline}))
        return ModelAttemptPolicy(self.compiled, self.owner_session_id,
            self.source_revalidate, self.material_fingerprint, identity, self, pending,
            deadline, cancel, registry_provider, _ATTEMPT_POLICY_SEAL)

    @property
    def fingerprint(self):
        # Frozen compiled bytes and exact identity are retained, so repeated live
        # polling need not re-hash a 192MiB media payload on every 20ms boundary.
        return self._fingerprint

    def validate_runtime(self, binding, compiled, *, clock, deadline=None, cancel=None):
        self.validate_source(compiled, self.owner_session_id)
        if (type(self.source) is not ModelAttemptPolicy or self.pending is None
                or type(binding) is not BrokerSession or self.pending.binding != binding
                or self.pending.compiled is not compiled
                or self.pending.owner_session_id != self.owner_session_id
                or not isinstance(self.cancel, threading.Event) or self.cancel.is_set()
                or type(self.deadline) not in (int, float) or not clock() < self.deadline
                or deadline is not None and deadline > self.deadline
                or cancel is not None and cancel is not self.cancel):
            _fail('model_host_attempt_runtime_changed')
        try:
            if not callable(self.registry_provider) or self.registry_provider(self) is not self:
                _fail('model_host_attempt_capability_revoked')
        except ModelProxyError:
            raise
        except Exception:
            _fail('model_host_attempt_capability_revoked')
        return True


@dataclass(frozen=True)
class ModelHttpResponse:
    status: int
    body: bytes = field(repr=False)
    content_type: str = 'application/json'


class ModelTransport(ABC):
    """Only host code supplies transports. Implementations enforce wall/cancel/bytes."""
    @abstractmethod
    async def send(self, route, payload, key, *, deadline, cancel, before_send, max_response_bytes):
        raise NotImplementedError


class HttpxModelTransport(ModelTransport):
    """Opt-in exact raw-byte HTTP transport, not used by real network tests.

    asyncio cancellation/total wall time closes the response/client. DNS may outlive
    cancellation in the OS resolver; no assertion of killing resolver threads is made.
    Ordinary HTTP requires the host's explicit insecure-transport approval.
    """
    def __init__(self, *, client_factory=None, clock=time.monotonic, allow_plain_http=False):
        if client_factory is not None and not callable(client_factory) or not callable(clock) or type(allow_plain_http) is not bool:
            raise TypeError('explicit trusted client/timing policy required')
        self.client_factory, self.clock, self.allow_plain_http = client_factory, clock, allow_plain_http

    async def send(self, route, payload, key, *, deadline, cancel, before_send, max_response_bytes):
        if (type(deadline) not in (int,float) or not math.isfinite(deadline)
                or not callable(getattr(cancel,'is_set',None))
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES):
            _fail('model_transport_limits')
        task = asyncio.create_task(self._send(route,payload,key,deadline=deadline,cancel=cancel,
                                             before_send=before_send,max_response_bytes=max_response_bytes))
        try:
            while not task.done():
                if cancel.is_set() or self.clock() >= deadline:
                    _fail('model_cancelled_or_deadline')
                await asyncio.wait((task,),timeout=min(.02,max(.001,deadline-self.clock())))
            if cancel.is_set() or self.clock() >= deadline:
                _fail('model_cancelled_or_deadline')
            return await task
        except asyncio.CancelledError:
            raise
        except ModelProxyError:
            raise
        except Exception:
            _fail('model_transport_failed')
        finally:
            if not task.done():task.cancel()
            try:await asyncio.wait_for(asyncio.shield(task),timeout=.2)
            except BaseException:pass

    async def _send(self, route, payload, key, *, deadline, cancel, before_send, max_response_bytes):
        endpoint = _endpoint(route, self.allow_plain_http)
        if type(key) is not str or not 1 <= len(key) <= 16384 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            _fail('model_credential_invalid')
        if type(payload) is not bytes or len(payload) > MAX_WIRE_BYTES:
            _fail('model_wire_limit')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        if route.protocol == 'anthropic_messages':
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        elif route.protocol == 'gemini_content':
            headers['x-goog-api-key'] = key
        else:
            headers['Authorization'] = 'Bearer '+key
        remaining = deadline-self.clock()
        if remaining <= 0 or cancel.is_set():
            _fail('model_cancelled_or_deadline')
        factory = self.client_factory
        if factory is None:
            import httpx2
            factory = httpx2.AsyncClient
        # Factory is trusted; credentials never become a dataclass, instance member,
        # repr, log, endpoint query or public response.
        async with factory(trust_env=False, follow_redirects=False, timeout=remaining) as client:
            before_send()
            if cancel.is_set() or self.clock() >= deadline:
                _fail('model_cancelled_or_deadline')
            async def trace(event, _info):
                # httpcore emits these immediately before headers/body network IO.
                # Recheck after DNS/TLS too, not only before entering stream().
                if event.endswith(('.send_request_headers.started','.send_request_body.started')):
                    before_send()
                    if cancel.is_set() or self.clock() >= deadline:
                        _fail('model_cancelled_or_deadline')
            async with client.stream('POST', endpoint, content=payload, headers=headers,
                                     extensions={'trace':trace}) as response:
                if (response.status_code < 200 or response.status_code >= 300
                        or getattr(response, 'history', ())):
                    _fail('model_provider_refused')
                if response.headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                    _fail('model_response_encoding_refused')
                content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if content_type not in ('application/json', 'application/problem+json'):
                    _fail('model_response_type')
                declared = response.headers.get('content-length')
                if declared is not None and (not declared.isascii() or not declared.isdecimal() or int(declared) > max_response_bytes):
                    _fail('model_response_limit')
                result = bytearray()
                async for chunk in response.aiter_raw():
                    if cancel.is_set() or self.clock() >= deadline:
                        _fail('model_cancelled_or_deadline')
                    if type(chunk) is not bytes or len(result)+len(chunk) > max_response_bytes:
                        _fail('model_response_limit')
                    result.extend(chunk)
                return ModelHttpResponse(response.status_code, bytes(result), content_type)


def _usage(raw, mapping):
    if raw is None:
        return {'input_tokens': None, 'output_tokens': None, 'total_tokens': None}
    if type(raw) is not dict:
        _fail('model_usage_invalid')
    result = {}
    for public, key in mapping.items():
        value = raw.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 2**53-1):
            _fail('model_usage_invalid')
        result[public] = value
    return result


def _texts(parts, kind, *, protocol):
    if type(parts) is not list or len(parts) > 4096:
        _fail('model_response_parts')
    result = []
    for part in parts:
        if type(part) is not dict:
            _fail('model_response_parts')
        if protocol == 'gemini_content':
            if 'thought' in part and type(part['thought']) is not bool:
                _fail('model_thought_marker_invalid')
            if part.get('thought') is True:
                continue
            if 'text' in part:
                if type(part['text']) is not str:
                    _fail('model_answer_invalid')
                result.append(part['text'])
        elif part.get('type') == kind:
            if type(part.get('text')) is not str:
                _fail('model_answer_invalid')
            result.append(part['text'])
    return ''.join(result)


def parse_model_response(protocol, raw):
    """Return only answer/numeric usage/finite finish. Never raw internal blocks."""
    value = _json(raw)
    if type(value) is not dict:
        _fail('model_response_object')
    answer = ''; finish = 'incomplete'
    if protocol == 'openai_chat':
        choices = value.get('choices')
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            _fail('model_response_choices')
        choice = choices[0]; message = choice.get('message')
        if type(message) is not dict:
            _fail('model_response_message')
        content = message.get('content')
        if type(content) is str:
            answer = content
        elif type(content) is list:
            answer = _texts(content, 'text', protocol=protocol)
        elif content is not None:
            _fail('model_answer_invalid')
        reason = choice.get('finish_reason')
        finish = {'stop':'completed', 'length':'length', 'tool_calls':'tool_requested', 'function_call':'tool_requested', 'content_filter':'blocked'}.get(reason, 'incomplete')
        if message.get('tool_calls') or message.get('function_call'):
            finish = 'tool_requested'
        elif message.get('refusal'):
            finish = 'refused'
        usage = _usage(value.get('usage'), {'input_tokens':'prompt_tokens','output_tokens':'completion_tokens','total_tokens':'total_tokens'})
    elif protocol == 'openai_responses':
        blocks = value.get('output')
        if type(blocks) is not list or len(blocks) > 4096:
            _fail('model_response_parts')
        refused = tools = False
        for block in blocks:
            if type(block) is not dict:
                _fail('model_response_parts')
            if block.get('type') == 'message':
                parts = block.get('content')
                answer += _texts(parts, 'output_text', protocol=protocol)
                refused = refused or any(p.get('type') == 'refusal' for p in parts)
            elif block.get('type') in ('function_call','computer_call','web_search_call','file_search_call'):
                tools = True
        finish = 'completed' if value.get('status') == 'completed' else 'incomplete'
        if tools:finish = 'tool_requested'
        elif refused:finish = 'refused'
        elif value.get('status') == 'incomplete' and type(value.get('incomplete_details')) is dict and value['incomplete_details'].get('reason') == 'max_output_tokens':finish = 'length'
        usage = _usage(value.get('usage'), {'input_tokens':'input_tokens','output_tokens':'output_tokens','total_tokens':'total_tokens'})
    elif protocol == 'anthropic_messages':
        parts = value.get('content'); answer = _texts(parts, 'text', protocol=protocol)
        finish = {'end_turn':'completed','stop_sequence':'completed','max_tokens':'length','tool_use':'tool_requested','refusal':'refused','pause_turn':'incomplete'}.get(value.get('stop_reason'),'incomplete')
        if any(p.get('type') == 'tool_use' for p in parts):finish = 'tool_requested'
        usage = _usage(value.get('usage'), {'input_tokens':'input_tokens','output_tokens':'output_tokens','total_tokens':'total_tokens'})
    elif protocol == 'gemini_content':
        candidates = value.get('candidates')
        if type(candidates) is not list or len(candidates) != 1 or type(candidates[0]) is not dict:
            _fail('model_response_choices')
        candidate = candidates[0]; content = candidate.get('content')
        if type(content) is not dict:
            _fail('model_response_message')
        parts = content.get('parts'); answer = _texts(parts, None, protocol=protocol)
        finish = {'STOP':'completed','MAX_TOKENS':'length','SAFETY':'blocked','RECITATION':'blocked','PROHIBITED_CONTENT':'blocked'}.get(candidate.get('finishReason'),'incomplete')
        if any('functionCall' in p for p in parts):finish = 'tool_requested'
        usage = _usage(value.get('usageMetadata'), {'input_tokens':'promptTokenCount','output_tokens':'candidatesTokenCount','total_tokens':'totalTokenCount'})
    else:
        _fail('model_protocol_unknown')
    # Some compatible providers inline private reasoning instead of using their
    # typed internal block. Never return an ambiguous/unclosed thinking section.
    # This does not infer reasoning from prose; only explicit protocol-like tags.
    if re.search(r'</?(?:think|thinking|analysis)(?:\s[^>]*)?>', answer, re.I):
        _fail('model_inline_internal_thinking_refused')
    try:
        answer_bytes = answer.encode('utf-8')
    except UnicodeError:
        _fail('model_answer_invalid')
    if '\x00' in answer or len(answer_bytes) > MAX_RESPONSE_BYTES:
        _fail('model_answer_limit')
    # A provider's success flag without a supported answer is not a completed
    # text response. Unknown/non-text blocks are never returned as opaque success.
    if finish == 'completed' and not answer:
        finish = 'incomplete'
    return {'answer': answer, 'usage': usage, 'finish': finish}


@dataclass(frozen=True)
class _Approval:
    binding: BrokerSession | HostChatScope
    compiled: CompiledSubmission = field(repr=False)
    credential: dict = field(repr=False)
    fingerprint: str
    expires: float
    host_configured_attempt: bool = False
    attempt_policy: ModelAttemptPolicy | None = field(default=None, repr=False)


class ModelProxyService:
    """Trusted async host service; NOT directly callable by plugin JSON.

    credential_loader(route) runs only after independent approval. The host must
    bind route/profile to its own credential reference and protect callback/Keyring
    critical sections. Snapshots cannot detect direct external Keyring replacement.
    """
    def __init__(self, *, route_provider, credential_snapshot_provider, credential_loader,
                 authority_provider, transport=None, clock=time.monotonic, ttl=60,
                 max_response_bytes=MAX_RESPONSE_BYTES, allow_plain_http=False, binding_type=BrokerSession,
                 attempt_policy_provider=None):
        if not all(callable(p) for p in (route_provider, credential_snapshot_provider, credential_loader, authority_provider, clock)):
            raise TypeError('explicit host providers required')
        if transport is not None and not isinstance(transport, ModelTransport):
            raise TypeError('explicit bounded model transport required')
        if (type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0 < ttl <= 60
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES
                or type(allow_plain_http) is not bool):
            raise ValueError('bounded approval/output policy required')
        if (binding_type not in (BrokerSession, HostChatScope)
                or attempt_policy_provider is not None and not callable(attempt_policy_provider)
                or attempt_policy_provider is not None and binding_type is not BrokerSession):
            raise TypeError('explicit broker or host-chat authority and host policy registry required')
        self.binding_type = binding_type
        self.attempt_policy_provider = attempt_policy_provider
        self.route_provider, self.snapshot_provider, self.key_loader = route_provider, credential_snapshot_provider, credential_loader
        self.authority_provider, self.transport, self.clock = authority_provider, transport, clock
        self.ttl, self.max_response_bytes, self.allow_plain_http = ttl, max_response_bytes, allow_plain_http
        self._pending = {}; self._lock = threading.RLock()

    def _live(self, binding, compiled, credential=None):
        try:
            actual = self.authority_provider(binding)
            route = self.route_provider(binding, compiled.route.profile_id)
            current = _snapshot(self.snapshot_provider(compiled.route), compiled.route)
        except ModelProxyError:
            raise
        except Exception:
            _fail('model_binding_unavailable')
        if type(actual) is not self.binding_type or actual != binding or type(route) is not RouteBinding or route != compiled.route:
            _fail('model_binding_changed')
        if credential is not None and current != credential:
            _fail('model_credential_changed')
        return current

    def _attempt_policy(self, policy, binding, compiled, *, deadline=None, cancel=None):
        if type(policy) is not ModelAttemptPolicy or not callable(self.attempt_policy_provider):
            _fail('model_host_attempt_capability_required')
        try:
            if self.attempt_policy_provider(policy) is not policy:
                _fail('model_host_attempt_capability_revoked')
        except ModelProxyError:
            raise
        except Exception:
            _fail('model_host_attempt_capability_revoked')
        policy.validate_runtime(binding, compiled, clock=self.clock, deadline=deadline, cancel=cancel)
        return policy

    def preview_compiled(self, binding, compiled, *, allow_host_configured_attempt=False,
                         attempt_policy=None):
        if type(binding) is not self.binding_type:
            _fail('model_attested_binding_required')
        if type(allow_host_configured_attempt) is not bool:
            _fail('model_host_attempt_scope_required')
        if self.binding_type is BrokerSession:
            if allow_host_configured_attempt and attempt_policy is None:
                _fail('model_host_attempt_scope_required')
            if not allow_host_configured_attempt and attempt_policy is not None:
                _fail('model_host_attempt_scope_required')
        elif attempt_policy is not None:
            _fail('model_host_attempt_scope_required')
        _checked_compiled(compiled); _endpoint(compiled.route, self.allow_plain_http)
        if self.binding_type is BrokerSession and allow_host_configured_attempt:
            self._attempt_policy(attempt_policy, binding, compiled)
        credential = self._live(binding, compiled)
        public = {**compiled.preview_to_ui(), 'endpoint':compiled.route.endpoint,
                  'max_response_bytes':self.max_response_bytes, 'plain_http':urlsplit(compiled.route.endpoint).scheme == 'http'}
        self._live(binding, compiled, credential)
        if attempt_policy is not None:
            self._attempt_policy(attempt_policy, binding, compiled)
        if (not compiled.ready_for_verified_adapter and not allow_host_configured_attempt) or self.transport is None:
            return {'preview':public,'blocked':'model_adapter_unverified' if not compiled.ready_for_verified_adapter else 'model_transport_unavailable',
                    'token':None,'fingerprint':None,'approval_required':False,'sent':False}
        private = {'binding':binding.wire(),'compiled':compiled.fingerprint,'wire':compiled.payload_sha256,
                   'manifest':_sha(compiled.submission_manifest_bytes),'preview':_sha(compiled.preview_content_bytes),
                   'credential':credential,'response_limit':self.max_response_bytes,'plain_http':self.allow_plain_http,
                   'host_configured_attempt':allow_host_configured_attempt,
                   'attempt_policy':attempt_policy.fingerprint if attempt_policy is not None else None}
        fingerprint = _sha(_encoded(private)); now = self.clock()
        expires = min(now+self.ttl, attempt_policy.deadline) if attempt_policy is not None else now+self.ttl
        if expires <= now:
            _fail('model_host_attempt_runtime_changed')
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._pending = {key:value for key,value in self._pending.items() if value.expires > now}
            pending_bytes = sum(len(item.compiled.payload_bytes)+len(item.compiled.submission_manifest_bytes)
                                + len(item.compiled.preview_content_bytes) for item in self._pending.values())
            proposal_bytes = len(compiled.payload_bytes)+len(compiled.submission_manifest_bytes)+len(compiled.preview_content_bytes)
            if len(self._pending) >= MAX_PENDING or pending_bytes+proposal_bytes > MAX_PENDING_BYTES:
                _fail('model_approval_capacity')
            self._pending[_sha(token.encode('ascii'))] = _Approval(binding,compiled,credential,fingerprint,
                expires,allow_host_configured_attempt,attempt_policy)
        return {'preview':public,'token':token,'fingerprint':fingerprint,'expires_in':expires-now,
                'approval_required':True,'sent':False}

    def cancel_approval(self, token):
        if type(token) is not str or not _TOKEN.fullmatch(token):
            _fail('model_approval_invalid')
        with self._lock:
            return self._pending.pop(_sha(token.encode('ascii')),None) is not None

    async def execute(self, token, fingerprint, *, user_confirmed, deadline, cancel,
                      accept_unverified_capabilities=False, allow_plain_http=False, revalidate=None):
        if type(token) is not str or not _TOKEN.fullmatch(token):
            _fail('model_approval_invalid')
        with self._lock:
            approval = self._pending.pop(_sha(token.encode('ascii')),None)
        if (approval is None or user_confirmed is not True or type(fingerprint) is not str
                or not _HEX.fullmatch(fingerprint) or not hmac.compare_digest(approval.fingerprint,fingerprint)
                or self.clock() >= approval.expires):
            _fail('model_approval_invalid')
        route, compiled = approval.compiled.route, approval.compiled
        if (type(accept_unverified_capabilities) is not bool or type(allow_plain_http) is not bool
                or revalidate is not None and not callable(revalidate)):
            _fail('model_confirmation_policy_invalid')
        if approval.host_configured_attempt:
            if accept_unverified_capabilities is not True:
                _fail('model_unverified_capabilities_confirmation_required')
            if type(approval.binding) is BrokerSession:
                self._attempt_policy(approval.attempt_policy, approval.binding, compiled)
            elif type(approval.binding) is not HostChatScope:
                _fail('model_host_attempt_scope_required')
        elif not compiled.ready_for_verified_adapter:
            _fail('model_unverified_submission')
        if (type(approval.binding) is HostChatScope and urlsplit(route.endpoint).scheme == 'http'
                and allow_plain_http is not True):
            _fail('model_plain_http_confirmation_required')
        if (type(deadline) not in (int,float) or not math.isfinite(deadline)
                or not self.clock() < deadline <= self.clock()+route.timeout_seconds
                or not callable(getattr(cancel,'is_set',None))):
            _fail('model_deadline_required')
        def check(*, material_boundary=False):
            if cancel.is_set() or self.clock() >= deadline:
                _fail('model_cancelled_or_deadline')
            self._live(approval.binding,compiled,approval.credential)
            if approval.attempt_policy is not None:
                self._attempt_policy(approval.attempt_policy, approval.binding, compiled,
                                     deadline=deadline, cancel=cancel)
            if revalidate is not None:
                try:
                    if revalidate(material_boundary) is not True:
                        _fail('model_submission_changed')
                except ModelProxyError:
                    raise
                except Exception:
                    _fail('model_submission_changed')
        check(material_boundary=True)
        try:
            key = self.key_loader(route)
        except Exception:
            _fail('model_credential_unavailable')
        if type(key) is not str or not 1 <= len(key) <= 16384 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            _fail('model_credential_invalid')
        check()
        if self.transport is None:
            _fail('model_transport_unavailable')
        async def perform():
            check(material_boundary=True)
            response = await self.transport.send(route,compiled.payload_bytes,key,deadline=deadline,cancel=cancel,
                before_send=lambda:check(material_boundary=True),max_response_bytes=self.max_response_bytes)
            check(material_boundary=True)
            if (type(response) is not ModelHttpResponse or type(response.status) is not int
                    or not 200 <= response.status < 300 or type(response.body) is not bytes
                    or not 0 < len(response.body) <= self.max_response_bytes
                    or response.content_type not in ('application/json','application/problem+json')):
                _fail('model_response_refused')
            result = parse_model_response(route.protocol,response.body)
            check()
            return result
        task = asyncio.create_task(perform())
        try:
            while not task.done():
                check()
                await asyncio.wait((task,),timeout=min(.02,max(.001,deadline-self.clock())))
            check()
            return await task
        except asyncio.CancelledError:
            raise
        except ModelProxyError:
            raise
        except Exception:
            _fail('model_transport_failed')
        finally:
            key = None
            if not task.done():task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task),timeout=.2)
            except (BaseException,):
                # A hostile trusted transport can ignore task cancellation. Do not
                # await it forever or accept its late result. Host resource owners
                # must retain/close unconfirmed resources; no retry is authorized.
                pass
