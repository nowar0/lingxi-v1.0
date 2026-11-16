"""Explicit authenticated ordinary-broker preparation, never execution approval.

Mount before package_host.router (its /{plugin_id}/{kind} route is generic)::

    from app.plugin_broker_api import create_router as create_plugin_broker_router
    app.include_router(create_plugin_broker_router(package_host.initialize,
        principal_provider=ui_auth.sessions.principal))

The factory does not call its provider. Only a person's POST selects the exact
file/data requests ALREADY in args, or registers a UTF-8 HTTP body and performs
host DNS preview. No browser IPs, runtime factory, PID or compiled payload is an
input. Prepare HTTP first, explicitly copy its descriptor, THEN select objects:
selection is bound to the same ten source fields as composition, never call_id.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.model_host import HostUiPrincipal
from app.model_pending_runtime import RuntimePendingModels
from app.plugin_broker_composition import RuntimeBrokerComposition
from app.plugin_manifest import canonical_json
from app.plugin_runtime_brokers import RuntimeBrokerServices, request_contract
from app.plugin_store import PluginError, PluginManager, _fingerprint, _schema_args
from app.plugin_wire import _LIKELY_KEY, _SECRET, decode_json, encode_json
from app.ui_auth import current_principal, sessions

_SHA = r'^[0-9a-f]{64}$'
_PLUGIN = r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$'
_OBJECT_OPS = frozenset(('file.read', 'file.write', 'data.read', 'data.write'))
_SOURCE_FIELDS = ('manifest', 'tool', 'args', 'config', 'state', 'plugin_id',
                  'revision', 'digest', 'trust_fingerprint', 'dependency_binding')


def _fail(code, status=409):
    raise HTTPException(status, {'code': code, 'message':
        '具体目标准备未完成或已变化；未执行插件、未发送 HTTP、未延长运行截止时间。'})


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


class _BoundedPreparationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            if request.method == 'POST':
                if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    _fail('preparation_json_required', 415)
                length = request.headers.get('content-length')
                if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > 131072):
                    _fail('preparation_request_limit', 413)
                raw = bytearray()
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            raw.extend(chunk)
                            if len(raw) > 131072:
                                _fail('preparation_request_limit', 413)
                except TimeoutError:
                    _fail('preparation_request_timeout', 408)

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
                    _fail('preparation_json_invalid', 422)
                request._body = bytes(raw)
            return await original(request)
        return handler


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PreparationSource(_Strict):
    plugin_id: str = Field(pattern=_PLUGIN, max_length=200)
    expected_revision: int = Field(ge=1, le=2**53-1)
    expected_digest: str = Field(pattern=_SHA)
    tool: str = Field(min_length=1, max_length=101)
    args: dict
    user_confirmed: bool


class ObjectPreparation(PreparationSource):
    # Exact indices are a positive selection, NOT a permission/path enumeration.
    # select_preview_objects intentionally needs all file/data requests from this
    # same source; partial selection must be expressed by editing args beforehand.
    selected_request_indices: list[int] = Field(min_length=1, max_length=32)


class HttpPreparation(PreparationSource):
    request_index: int = Field(ge=0, le=31)
    url: str = Field(min_length=1, max_length=4000)
    method: str = Field(pattern=r'^(GET|HEAD|POST|PUT|PATCH|DELETE)$')
    body_text: str = Field(max_length=12000)
    max_response_bytes: int = Field(ge=1, le=12000)


def create_router(manager_provider, *, principal_provider=sessions.principal):
    """Lazy real-manager composition. No runtime/provider invocation at construction."""
    if not callable(manager_provider) or not callable(principal_provider):
        raise TypeError('lazy real manager and authenticated UI principal required')
    router = APIRouter(prefix='/v1/plugin-packages/preparations',
        tags=['plugin-broker-preparations'], route_class=_BoundedPreparationRoute)

    def principal(request):
        if request.query_params:
            _fail('preparation_query_not_supported', 422)
        owner = principal_provider(request)
        if type(owner) is not HostUiPrincipal or sessions.live(owner) != owner:
            _fail('preparation_authenticated_ui_owner_required', 401)
        if request.method == 'POST' and (request.headers.get('origin') != str(request.base_url).rstrip('/')
                or request.headers.get('sec-fetch-site') == 'cross-site'):
            _fail('preparation_same_origin_required', 403)
        return owner

    def actual():
        manager = manager_provider()
        if (type(manager) is not PluginManager or not manager._initialized or not manager._registry_ready
                or manager._closing or manager._lease is None or manager._authority_nonce is None):
            _fail('preparation_live_manager_required', 503)
        services = getattr(manager.runtime, 'broker_services', None)
        composition = getattr(services, 'composition', None)
        if (type(services) is not RuntimeBrokerServices or type(composition) is not RuntimeBrokerComposition
                or composition.services is not services or composition._manager() is not manager
                or services.request_contract_approved is not True):
            _fail('preparation_real_broker_composition_required', 503)
        return manager, services, composition

    def response(owner, callback):
        token = current_principal.set(owner)
        try:
            value = callback()
            if sessions.live(owner) != owner:
                _fail('preparation_authenticated_ui_owner_required', 401)
            raw = canonical_json(value).encode('utf-8')
            if len(raw) > 2*1024**2:
                _fail('preparation_response_limit_no_truncation', 413)
            return JSONResponse(value, headers={'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff'})
        except HTTPException:
            raise
        except Exception as error:
            code = getattr(error, 'code', '')
            # Never return OS exception text, raw schema paths/inputs or key data.
            if type(code) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{1,100}', code):
                code = 'preparation_operation_unconfirmed'
            status = error.status if isinstance(error, PluginError) else 409
            _fail(code, status)
        finally:
            current_principal.reset(token)

    def source(manager, body, args=None):
        """Mirror _call_snapshot's ten runtime source fields, WITHOUT preview_spec.

        Calling preview_call here would claim selection/read target contents and
        allocate a call_id. It is deliberately reserved for the later fullspec UI.
        All DB helpers use this one transaction; composition runs AFTER it exits.
        """
        args = decode_json(encode_json(body.args if args is None else args, safe=True))
        with manager._db() as db:
            row = manager._row(db, body.plugin_id, installed=True)
            manager._expected(row, body.expected_revision)
            if row['digest'] != body.expected_digest:
                _fail('preparation_digest_changed')
            if not row['enabled']:
                _fail('preparation_installed_enabled_plugin_required')
            manager._ready(row)
            manifest = json.loads(row['manifest'])
            if _fingerprint(manifest) != row['digest']:
                _fail('preparation_manifest_digest_changed')
            if not manifest.get('entrypoint'):
                _fail('preparation_ordinary_entrypoint_required')
            blocks = manager._dependency_blocks(db, manifest, enabled=True)
            if blocks:
                _fail('preparation_dependency_unavailable')
            tools = [tool for tool in manifest['tools'] if tool['name'] == body.tool]
            if len(tools) != 1:
                _fail('preparation_tool_not_declared', 422)
            _schema_args(args, tools[0].get('input_schema',
                {'type': 'object', 'properties': {}, 'additionalProperties': False}))
            requests, _ = request_contract(manifest, body.tool, args)
            # Unlike manager's path normalization, preparation accepts only the
            # exact canonical target composition will later select. No path guess.
            if any(request['op'] in _OBJECT_OPS and args[request['target_argument']] != request['target']
                   for request in requests):
                _fail('preparation_exact_absolute_object_target_required', 422)
            trust = manager._trust_binding()
            result = {'manifest': manifest, 'tool': body.tool, 'args': args,
                'config': json.loads(row['config']), 'state': json.loads(row['state']),
                'plugin_id': row['id'], 'revision': row['revision'], 'digest': row['digest'],
                'trust_fingerprint': _fingerprint(trust),
                'dependency_binding': manager._execution_dependency_binding(db, manifest)}
            if set(result) != set(_SOURCE_FIELDS):
                _fail('preparation_complete_source_required')
            return result, requests, trust

    def summary(package_input, trust):
        return {'plugin_id': package_input['plugin_id'], 'tool': package_input['tool'],
            'revision': package_input['revision'], 'digest': package_input['digest'],
            'source_sha256': _hash(package_input), 'source_fields': list(_SOURCE_FIELDS),
            'args_sha256': _hash(package_input['args']),
            'config_fingerprint': _fingerprint(package_input['config']),
            'state_fingerprint': _fingerprint(package_input['state']),
            'trust_snapshot': trust, 'trust_fingerprint': package_input['trust_fingerprint'],
            'dependency_binding': package_input['dependency_binding']}

    @router.post('/objects')
    def objects(body: ObjectPreparation, request: Request):
        owner = principal(request)

        def prepare():
            if body.user_confirmed is not True:
                _fail('preparation_explicit_object_confirmation_required', 422)
            manager, _, composition = actual()
            with manager._lock:
                package_input, requests, trust = source(manager, body)
                indices = [index for index, value in enumerate(requests) if value['op'] in _OBJECT_OPS]
                if not indices or body.selected_request_indices != indices:
                    _fail('preparation_select_exact_all_declared_objects_required', 422)
                ids = [requests[index]['params']['object_id'] for index in indices]
                if len(ids) != len(set(ids)):
                    _fail('broker_one_operation_per_object_required', 422)
                # Exactly this user-confirmed args, ONCE; never all manifest paths.
                selected = composition.select_preview_objects(**package_input)
                if len(selected) != len(indices):
                    _fail('preparation_selected_object_count_changed')
                required = [{'request_index': index, 'op': requests[index]['op'],
                    'target_argument': requests[index]['target_argument'],
                    'target': requests[index]['target'], 'params': requests[index]['params']}
                    for index in indices]
                return {'binding': summary(package_input, trust), 'selected_objects': selected,
                    'required_requests': required, 'selection_fingerprint': _hash(selected),
                    'expires_in_upper_bound': 120, 'one_preview_only': True,
                    'selected_target_content_read': False, 'sent': False,
                    'execution_authorized': False, 'full_call_approval_required': True}
        return response(owner, prepare)

    @router.post('/http')
    def http(body: HttpPreparation, request: Request):
        owner = principal(request)

        def prepare():
            if body.user_confirmed is not True:
                _fail('preparation_explicit_http_confirmation_required', 422)
            # No binary/base64/header/approved_ips/private/plaintext options exist.
            try:
                binary = body.body_text.encode('utf-8')
            except UnicodeError:
                _fail('preparation_http_utf8_body_required', 422)
            if len(binary) > 12000 or any(ord(char) < 32 and char not in '\r\n\t'
                                         or ord(char) == 127 for char in body.body_text):
                _fail('preparation_http_binary_body_forbidden', 422)
            if _SECRET.search(body.body_text) or _LIKELY_KEY.search(body.body_text):
                _fail('preparation_http_secret_body_forbidden', 422)
            if body.method in ('GET', 'HEAD') and binary:
                _fail('composition_get_head_body_must_be_empty', 422)
            manager, services, composition = actual()
            with manager._lock:
                initial, requests, trust = source(manager, body)
                # Validate the exact ten-field composition source BEFORE DNS too.
                # This checks config/state safe shape and the complete contract,
                # without creating a call_id, scope grant or runtime invocation.
                from app.plugin_broker_composition import _source
                _source(initial)
                if body.request_index >= len(requests):
                    _fail('preparation_explicit_http_request_required', 422)
                selected = requests[body.request_index]
                if selected['op'] != 'http.request' or selected['target'] != body.url:
                    _fail('preparation_exact_declared_http_target_required', 422)
                params = selected['params']
                if set(params) - {'url', 'method', 'max_response_bytes', 'request_descriptor',
                                  'body_sha256', 'allow_redirects'}:
                    _fail('preparation_http_host_only_parameters_forbidden', 422)
                if params.get('allow_redirects', False) is not False:
                    _fail('preparation_http_redirects_forbidden', 422)
                if ('method' in params and params['method'] != body.method
                        or 'max_response_bytes' in params and params['max_response_bytes'] != body.max_response_bytes):
                    _fail('preparation_http_explicit_parameters_changed', 422)
                authority = manager.ownership_authority()
            # This is the ONLY DNS entry: executed by a future explicit user POST,
            # never import/factory/UI render. No HTTP request is sent here.
            if body.method in ('GET', 'HEAD'):
                registered = composition.prepare_https_empty_request(url=body.url,
                    method=body.method, max_response_bytes=body.max_response_bytes)
            else:
                prepared_body = composition.prepare_http_body(binary)
                registered = composition.prepare_http_request(body_source_id=prepared_body['source_id'],
                    url=body.url, method=body.method, max_response_bytes=body.max_response_bytes)
            args = copy.deepcopy(initial['args'])
            args['broker_requests'][body.request_index]['params'] = registered['params']
            # Do not enter package_host's provider lock while holding manager._lock
            # (a data-root switch closes the old manager in the opposite order).
            if actual() != (manager, services, composition):
                _fail('preparation_manager_authority_changed')
            with manager._lock:
                if composition._manager() is not manager or manager.ownership_authority() != authority:
                    _fail('preparation_manager_authority_changed')
                final, _, final_trust = source(manager, body, args)
                _source(final)
                if ({key: value for key, value in initial.items() if key != 'args'}
                        != {key: value for key, value in final.items() if key != 'args'} or final_trust != trust):
                    _fail('preparation_source_changed_during_dns')
                with composition._lock:
                    entry = composition._http.get(registered['source_id'])
                    if entry is None or entry.principal != owner or entry.expires <= composition.clock():
                        _fail('preparation_http_registry_changed')
                    pinned = asdict(entry.target)
                    fingerprint = entry.fingerprint
                    remaining = entry.expires-composition.clock()
                return {'binding': summary(final, final_trust), 'request_index': body.request_index,
                    'required_request': {'op': 'http.request', 'target_argument': selected['target_argument'],
                        'params': registered['params']},
                    'descriptor': registered, 'pinned_target': pinned,
                    'target_fingerprint': fingerprint, 'body_text_utf8': body.body_text,
                    'body_bytes': len(binary), 'body_sha256': hashlib.sha256(binary).hexdigest(),
                    'args': args, 'expires_in': max(0, remaining), 'dns_preview_only': True,
                    'sent': False, 'execution_authorized': False, 'full_call_approval_required': True,
                    'object_selection_required_after_args_change': True}
        return response(owner, prepare)

    @router.get('/pending')
    def pending(request: Request):
        owner = principal(request)

        def active():
            manager, services, _ = actual()
            pending_models = services.pending_models
            if type(pending_models) is not RuntimePendingModels:
                _fail('preparation_real_pending_registry_required', 503)
            with pending_models._lock:
                # Read only matching owners BEFORE invoking any source callback.
                records = [(key, record) for key, record in pending_models._records.items()
                    if hmac.compare_digest(record.pending.owner_session_id, owner.owner_session_id)]
            values = []
            for request_id, record in records:
                try:
                    if record.execution_claimed:
                        continue
                    # Real registry revalidation, not stale serialized active flags.
                    value = pending_models.pending(request_id)
                    invocation = pending_models.live_invocation(value.binding)
                    if (value is not record.pending or invocation != record.invocation
                            or invocation is None or invocation.cancel.is_set() or record.execution_claimed):
                        continue
                    with manager._lock:
                        reservation = manager._manager_execution_bindings.get(value.binding.call_id)
                        if (reservation is None or reservation.get('mode') != 'ordinary'
                                or not reservation.get('started')):
                            continue
                        identity, snapshot = reservation['identity'], reservation['snapshot']
                        if (identity.plugin_id != value.binding.plugin_id or identity.revision != value.binding.revision
                                or identity.digest != value.binding.digest or identity.call_id != value.binding.call_id
                                or snapshot.get('id') != identity.plugin_id
                                or snapshot.get('expected_revision') != identity.revision
                                or snapshot.get('digest') != identity.digest):
                            continue
                        tool = snapshot['tool']
                    remaining = min(record.expires, invocation.deadline)-pending_models.clock()
                    if remaining <= 0:
                        continue
                    values.append({'request_id': request_id, 'plugin_id': value.binding.plugin_id,
                        'revision': value.binding.revision, 'digest': value.binding.digest, 'tool': tool,
                        'submission_sha256': value.compiled.payload_sha256,
                        'submission_fingerprint': value.compiled.fingerprint,
                        'owner_session_id_sha256': hashlib.sha256(owner.owner_session_id.encode()).hexdigest(),
                        'current_deadline_monotonic': invocation.deadline, 'expires_in': remaining,
                        'original_runtime_deadline': True, 'approval_required': True, 'sent': False})
                except Exception as error:
                    # Only known cleanup/expiry/source races disappear from the
                    # active list. An unexpected model error remains diagnostic.
                    if getattr(error, 'code', '') not in {
                            'model_runtime_pending_unavailable',
                            'model_runtime_invocation_cancelled_or_closed',
                            'model_runtime_invocation_unavailable',
                            'model_runtime_invocation_changed_or_expired',
                            'model_runtime_materials_changed',
                            'model_runtime_materials_unavailable'}:
                        raise
            if actual() != (manager, services, services.composition):
                _fail('preparation_manager_authority_changed')
            return {'items': values, 'owner_from_authenticated_principal': True,
                'original_runtime_deadline': True, 'ttl_extended': False,
                'child_kept_alive': False, 'automatic_refresh': False}
        return response(owner, active)

    return router
