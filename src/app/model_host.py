"""Authenticated host-chat material -> immutable payload -> explicit HTTP submission.

No import/construction I/O. No BrokerSession, plugin PID, implicit chat context,
credential discovery, fallback, retry, or data migration. Mount only with a real
host authentication provider; loopback/Origin alone is NOT authentication.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.model_attachment_inspection import InspectionError, inspect_attachment_bytes
from app.model_document_api import DocumentApiError, DocumentSource, SelectionInput, read_media_source
from app.model_document_extract import ExtractionError, extract_document, preview_document_conversion
from app.model_input_registry import InputRegistryError, ModelInputRegistry
from app.model_video_host import HostVideoMaterials, VideoHostError
from app.model_video_extract import VideoExtractionError
from app.model_submission import (CapabilitiesEvidence, HostChatScope, RouteBinding,
    SubmissionError, compile_submission)
from app.plugin_model_proxy import (HttpxModelTransport, ModelProxyError, ModelProxyService)

MAX_BODY = 1024 * 1024
MAX_HOST_BYTES = 512 * 1024**2
MAX_SCOPES = 32
SHA = r'^[0-9a-f]{64}$'
TOKEN = r'^[A-Za-z0-9_-]{32,128}$'
NOTICE = '能力来自当前主机配置，尚未针对该提供方实际验证；只尝试你明确选择的模型和交付，不自动重试或切换。'
# Exact compiler + proxy public DTO; never expose serialized provider payloads
# or newly added internal service fields through the host's final approval page.
_FINAL_PREVIEW_FIELDS = (
    'schema','text_sha256','text_characters','attachments','attachment_count','part_count',
    'processed_text_sha256','max_output_tokens','timeout_seconds','capability_source','limits',
    'loss_notes','text','processed_text_parts','provider_id','protocol','model','profile_id',
    'profile_revision','route_fingerprint','capability_fingerprint','payload_sha256','wire_bytes',
    'submission_fingerprint','ready_for_verified_adapter','verification_note','estimated_text_tokens',
    'media_token_estimate','token_counts_are_estimates','requires_separate_explicit_approval',
    'execution_grant_minted','automatic_history_persona_memory_tools','endpoint','max_response_bytes','plain_http',
)


class ModelHostError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def _fail(code, status=409):
    raise ModelHostError(code, status)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class HostUiPrincipal:
    """Trusted authentication receipt. NEVER deserialize this from request JSON.

    owner_session_id must identify the authenticated UI session (not a tab/chat ID).
    authority_fingerprint must change on logout, credential/session rotation or
    permission changes. live_principal must revalidate against the real host auth.
    """
    owner_session_id: str = field(repr=False)
    authority_fingerprint: str

    def __post_init__(self):
        if (type(self.owner_session_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{15,127}', self.owner_session_id)
                or type(self.authority_fingerprint) is not str
                or not re.fullmatch(SHA, self.authority_fingerprint)):
            raise ValueError('authenticated host UI principal required')


@dataclass(frozen=True)
class _Route:
    route: RouteBinding
    evidence: CapabilitiesEvidence
    credential_ref: str = field(repr=False)
    configuration_fingerprint: str
    name: str


class CatalogModelRoutes:
    """Real saved profile + linked key metadata + safe credential generation bridge.

    No key is read by listing, resolving, compilation or preview. Legacy credentials
    without a ready safe generation are diagnosed; no generation is fabricated.
    """
    def __init__(self, *, profile_reader=None, profile_lister=None, key_reader=None,
                 generation_reader=None, key_loader=None):
        self.profile_reader, self.profile_lister = profile_reader, profile_lister
        self.key_reader, self.generation_reader, self.key_loader = key_reader, generation_reader, key_loader

    def _profile(self, profile_id):
        if self.profile_reader is not None:
            return self.profile_reader(profile_id)
        from app import catalog
        return catalog.get('model-profile', profile_id)

    def _key(self, key_id):
        if self.key_reader is not None:
            return self.key_reader(key_id)
        from app import catalog
        return catalog.get('model-key', key_id)

    def _generation(self, reference):
        if self.generation_reader is not None:
            return self.generation_reader(reference)
        from app.credential_generation import snapshot_for_reference
        return snapshot_for_reference(reference)

    @staticmethod
    def _address(profile):
        protocol = {'openai':'openai_chat', 'anthropic':'anthropic_messages',
                    'gemini':'gemini_content'}.get(profile.get('protocol', 'openai'))
        if protocol is None:
            _fail('model_protocol_mapping_not_implemented', 422)
        base = profile.get('base_url', '').rstrip('/')
        if protocol == 'anthropic_messages':
            from app.llm.anthropic_bridge import endpoint
            address = endpoint(base, 'messages')
        elif protocol == 'gemini_content':
            # Follow the existing native Gemini adapter's exact documented mapping,
            # not a fallback to another provider/model or compatibility protocol.
            if base.endswith('/openai'):
                base = base[:-7]
            model = profile.get('model', '').removeprefix('models/')
            address = base + '/models/' + quote(model, safe='-._') + ':generateContent'
        else:
            address = base + '/chat/completions'
        return protocol, address

    def resolve(self, profile_id, *, max_output_tokens=4096, effort=None):
        profile = self._profile(profile_id)
        if (type(profile) is not dict or profile.get('deleted') or profile.get('enabled') is not True
                or profile.get('id') != profile_id or 'text' not in profile.get('capabilities', ())):
            _fail('model_profile_disabled_or_text_capability_missing')
        reference = profile.get('credential_ref')
        if not reference:
            _fail('model_credential_reference_required_local_anonymous_not_implemented')
        # Public metadata must be an opaque store reference, never a secret or its hash.
        if (type(reference) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', reference)
                or reference.startswith(('sk-', 'AIza', 'Bearer'))):
            _fail('model_safe_credential_reference_required')
        generation = self._generation(reference)
        if (type(generation) is not dict
                or set(generation) != {'authority','generation','status','metadata_fingerprint'}
                or generation.get('status') != 'ready'
                or type(generation.get('authority')) is not str
                or not re.fullmatch(SHA, generation['authority'])
                or type(generation.get('generation')) is not int
                or not 1 <= generation['generation'] <= 2**53-1
                or type(generation.get('metadata_fingerprint')) is not str
                or not re.fullmatch(SHA, generation['metadata_fingerprint'])):
            _fail('model_credential_generation_not_ready')
        key = self._key(profile['key_id']) if profile.get('key_id') else None
        if key is not None and (type(key) is not dict or key.get('deleted')
                or key.get('credential_ref') != reference or key.get('base_url') != profile.get('base_url')
                or key.get('provider_id', 'custom') != profile.get('provider_id', 'custom')
                or key.get('protocol', 'openai') != profile.get('protocol', 'openai')):
            _fail('model_profile_key_binding_changed')
        protocol, address = self._address(profile)
        model = profile.get('model', '')
        metadata = next((item for item in (key or {}).get('models', ())
                         if type(item) is dict and item.get('id') == model), {})
        if key is not None and not metadata:
            _fail('model_not_in_current_key_catalog')
        kinds = ['text']
        # These are explicit configuration declarations, never name-based evidence.
        vision = 'vision' in profile.get('capabilities', ())
        if key is not None:
            vision = vision and ('image' in metadata.get('input_modalities', ())
                                or 'vision' in metadata.get('capabilities', ()))
        if vision:
            kinds.append('image')
        # Only the implemented Gemini wire adapter accepts native video. This is
        # the saved profile's explicit declaration, never a model-name inference
        # or proof that the provider has accepted this particular video.
        native_video = protocol == 'gemini_content' and 'video' in profile.get('capabilities', ())
        if native_video:
            kinds.append('video')
        native_mimes = (('image/png','image/jpeg','image/webp') if vision else ()) + (
            ('video/mp4','video/webm') if native_video else ())
        reasoning = metadata.get('reasoning', {})
        # Saved discovery may contain model-name rules/legacy guesses. Only
        # service/manual declarations using our actual wire parameter are exposed.
        efforts = tuple(dict.fromkeys(value for value in reasoning.get('levels', ())
                        if value in ('none','minimal','low','medium','high','xhigh'))) if (
                            type(reasoning) is dict and reasoning.get('source') in ('service','manual')
                            and reasoning.get('parameter', 'effort') == 'effort'
                            and protocol in ('openai_chat','openai_responses')) else ()
        if effort is not None and effort not in efforts:
            _fail('model_effort_not_declared')
        if effort is not None and protocol not in ('openai_chat','openai_responses'):
            _fail('model_effort_mapping_not_implemented')
        route = RouteBinding(profile_id, profile['revision'], profile.get('provider_id', 'custom'),
            protocol, address, model, generation['authority'], generation['generation'],
            max_output_tokens, min(300, profile.get('timeout', 90)), effort)
        configuration = _digest({'profile':profile, 'key':key})
        evidence = CapabilitiesEvidence(route.fingerprint, 'host_configured', profile['revision'],
            tuple(kinds), native_mimes, configuration,
            supported_efforts=efforts)
        return _Route(route, evidence, reference, configuration, str(profile.get('name') or model))

    def snapshot(self, route):
        held = self.resolve(route.profile_id, max_output_tokens=route.max_output_tokens, effort=route.effort)
        if held.route != route:
            _fail('model_route_changed')
        return self._generation(held.credential_ref)

    def load_key(self, route):
        # Called exclusively inside ModelProxyService.execute, after confirmation.
        held = self.resolve(route.profile_id, max_output_tokens=route.max_output_tokens, effort=route.effort)
        if held.route != route:
            _fail('model_route_changed')
        if self.key_loader is not None:
            return self.key_loader(held.credential_ref)
        from app import config
        return config.load_key(held.credential_ref)

    @staticmethod
    def public_metadata(held):
        route, evidence = held.route, held.evidence
        return {'profile_id':route.profile_id, 'profile_revision':route.profile_revision,
            'name':held.name, 'provider_id':route.provider_id, 'model':route.model,
            'protocol':route.protocol, 'endpoint':route.endpoint,
            'credential_ref':held.credential_ref, 'credential_authority':route.credential_authority,
            'credential_generation':route.credential_generation, 'credential_ready':True,
            'available':True, 'capability_source':evidence.source,
            'supported_kinds':list(evidence.supported_kinds), 'native_mimes':list(evidence.native_mimes),
            'supported_efforts':list(evidence.supported_efforts),
            'max_output_tokens':route.max_output_tokens, 'timeout_seconds':route.timeout_seconds,
            'diagnostics':[]}

    def listing(self):
        if self.profile_lister is not None:
            profiles = self.profile_lister()
        else:
            from app import catalog
            profiles = catalog.listing('model-profile')
        items = []
        for profile in profiles:
            if profile.get('deleted'):
                continue
            # A disabled/unsupported/legacy profile remains visible with fixed safe
            # diagnostics. Unavailable values are null, not invented identities.
            public = {'profile_id':profile['id'], 'profile_revision':profile['revision'],
                'name':str(profile.get('name') or profile.get('model', '') or profile['id']),
                'provider_id':profile.get('provider_id', 'custom'), 'model':profile.get('model', ''),
                'protocol':None, 'endpoint':None, 'credential_ref':None,
                'credential_authority':None, 'credential_generation':None,
                'capability_source':'host_configured', 'credential_ready':False, 'available':False,
                'supported_kinds':[], 'native_mimes':[], 'supported_efforts':[],
                'max_output_tokens':4096, 'timeout_seconds':min(300, profile.get('timeout', 90)),
                'diagnostics':[]}
            try:
                public = self.public_metadata(self.resolve(profile['id']))
            except Exception as error:
                code = getattr(error, 'code', None)
                public['diagnostics'] = [code if type(code) is str and re.fullmatch(r'[a-z0-9_]{1,120}', code)
                    else 'model_profile_or_safe_credential_generation_unavailable']
            items.append(public)
        return {'items':items, 'capability_notice':NOTICE, 'automatic_fallback':False}


@dataclass
class _MaterialPlan:
    source: DocumentSource = field(repr=False)
    delivery: str
    selection: object = field(repr=False)
    plan: object = field(repr=False)
    fingerprint: str
    expires: float
    registry_token: str | None = None


@dataclass
class _Material:
    source: DocumentSource = field(repr=False)
    descriptor: object
    delivery: str


@dataclass
class _Submission:
    compiled: object = field(repr=False)
    route: _Route = field(repr=False)
    materials: tuple
    revision: int
    expires: float
    token: str | None = field(default=None, repr=False)
    fingerprint: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    started: bool = False
    finished: bool = False
    invalidated: bool = False


@dataclass
class _Scope:
    binding: HostChatScope = field(repr=False)
    principal: HostUiPrincipal = field(repr=False)
    expires: float
    registry: ModelInputRegistry = field(repr=False)
    revision: int = 1
    sources: dict = field(default_factory=dict, repr=False)
    plans: dict = field(default_factory=dict, repr=False)
    materials: dict = field(default_factory=dict, repr=False)
    requests: dict = field(default_factory=dict, repr=False)
    revoked: bool = False
    handed_off: bool = False


class HostModelRuntime:
    """Real in-memory host registry. No browser-built identity or serialized payload.

    live_principal(principal) -> same authenticated principal, session_authority
    (principal, chat_session_id) -> True are mandatory trusted host callbacks.
    Source bytes are always read from the existing owned immutable media store.
    """
    def __init__(self, *, live_principal, session_authority, routes=None, reader=None,
                 clock=time.monotonic, allow_plain_http=False):
        if (not all(callable(item) for item in (live_principal, session_authority, clock))
                or reader is not None and not callable(reader)):
            raise TypeError('real live authentication, source reader and session authority required')
        if type(allow_plain_http) is not bool:
            raise TypeError('explicit HTTP policy required')
        self.live_principal, self.session_authority, self.clock = live_principal, session_authority, clock
        self.routes = routes if routes is not None else CatalogModelRoutes()
        if type(self.routes) is not CatalogModelRoutes:
            raise TypeError('host catalog route bridge required')
        self.reader = reader or read_media_source
        self._scopes = {}
        self._active = {}
        self._lock = threading.RLock()
        self.service = ModelProxyService(route_provider=self._live_route,
            credential_snapshot_provider=self.routes.snapshot, credential_loader=self.routes.load_key,
            authority_provider=self._live_binding, binding_type=HostChatScope,
            transport=HttpxModelTransport(clock=clock, allow_plain_http=allow_plain_http),
            clock=clock, allow_plain_http=allow_plain_http)

    def _principal(self, principal):
        try:
            valid = type(principal) is HostUiPrincipal and self.live_principal(principal) == principal
        except Exception:
            valid = False
        if not valid:
            _fail('model_authenticated_ui_session_required', 401)

    def _scope(self, principal, scope_id):
        self._principal(principal)
        scope = self._scopes.get(scope_id)
        if (scope is None or scope.revoked or self.clock() >= scope.expires
                or scope.principal != principal
                or self.session_authority(principal, scope.binding.session_id) is not True):
            if scope is not None and scope.principal == principal:
                scope.revoked = True
                for record in scope.requests.values():
                    record.invalidated = True
                    record.cancel.set()
            _fail('model_host_scope_expired_revoked_or_wrong_owner', 404)
        return scope

    def _live_binding(self, binding):
        if type(binding) is not HostChatScope:
            _fail('model_host_chat_scope_required')
        with self._lock:
            scope = self._scopes.get(binding.scope_id)
            if scope is None:
                _fail('model_host_scope_unavailable')
            return self._scope(scope.principal, binding.scope_id).binding

    def _live_route(self, binding, profile_id):
        with self._lock:
            self._live_binding(binding)
            scope = self._scopes[binding.scope_id]
            candidates = [record for record in scope.requests.values()
                if not record.invalidated and record.route.route.profile_id == profile_id]
            if len(candidates) != 1:
                _fail('model_host_pending_route_unavailable')
            record = candidates[0]
            self._recheck(scope, record, check_materials=False)
            return record.route.route

    def _read(self, scope, fid, expected_sha=None):
        self._scope(scope.principal, scope.binding.scope_id)
        source = self.reader(fid, scope.binding.session_id)
        if (type(source) is not DocumentSource or source.fid != fid
                or source.session_id != scope.binding.session_id
                or expected_sha is not None and source.sha256 != expected_sha):
            _fail('model_material_source_changed')
        return source

    def _invalidate_requests(self, scope):
        if scope.handed_off and not scope.revoked:
            _fail('model_handoff_scope_frozen')
        for record in scope.requests.values():
            record.invalidated = True
            record.cancel.set()
            if record.token is not None:
                self.service.cancel_approval(record.token)
        scope.requests.clear()
        scope.revision += 1

    def _held_bytes(self):
        # Explicitly handed-off sources keep only their original short expiry.
        # Capacity checks retire expired inactive scopes instead of retaining
        # source bytes until another UI tab happens to open a new scope.
        for scope_id,scope in tuple(self._scopes.items()):
            if self.clock()>=scope.expires and not any(record.started and not record.finished for record in scope.requests.values()):
                self._dispose(scope);self._scopes.pop(scope_id,None)
        held = sum(scope.registry.retained_bytes + sum(len(plan.source.content) for plan in scope.plans.values())
            + sum(len(record.compiled.payload_bytes)
            + len(record.compiled.submission_manifest_bytes)+len(record.compiled.preview_content_bytes)
            for record in scope.requests.values()) for scope in self._scopes.values())
        current = {id(record) for scope in self._scopes.values() for record in scope.requests.values()}
        held += sum(len(record.compiled.payload_bytes)+len(record.compiled.submission_manifest_bytes)
            +len(record.compiled.preview_content_bytes) for record in self._active.values()
            if id(record) not in current)
        return held

    def _capacity(self, extra=0):
        if self._held_bytes()+extra > MAX_HOST_BYTES:
            _fail('model_host_memory_capacity', 429)

    def _dispose(self, scope):
        scope.revoked = True
        self._invalidate_requests(scope)
        scope.registry.close()
        scope.sources.clear(); scope.plans.clear(); scope.materials.clear()

    def open_scope(self, principal, session_id):
        with self._lock:
            self._principal(principal)
            if self.session_authority(principal, session_id) is not True:
                _fail('model_chat_session_not_owned', 403)
            for scope_id, scope in tuple(self._scopes.items()):
                if scope.revoked or self.clock() >= scope.expires:
                    self._dispose(scope); self._scopes.pop(scope_id)
            if len(self._scopes) >= MAX_SCOPES:
                _fail('model_host_scope_capacity', 429)
            scope_id = secrets.token_hex(32)
            binding = HostChatScope(scope_id, principal.owner_session_id, session_id,
                                    principal.authority_fingerprint)
            registry = ModelInputRegistry(self.reader, self._live_binding, clock=self.clock,
                max_bytes=192*1024**2, binding_type=HostChatScope)
            self._scopes[scope_id] = _Scope(binding, principal, self.clock()+300, registry)
            return {'scope_id':scope_id, 'session_id':session_id, 'scope_kind':'host_chat',
                'expires_in':300, 'revision':1}

    @staticmethod
    def _deliveries(inspection):
        if inspection.kind in ('txt','md','docx','xlsx','pptx'):
            return [{'delivery':'extracted_text', 'available':True, 'code':None}]
        if (inspection.kind in ('png','jpeg','webp') and inspection.structural_validation in
                ('bounded_static_png_decoded','bounded_pinned_pillow_decoded')):
            return [{'delivery':'native', 'available':True, 'code':None}]
        if inspection.kind == 'pdf':
            # Metadata inspection is not conversion or provider/native support.
            # Only the core's successful explicit-text gate can expose this path.
            if inspection.delivery == 'extracted_text' and inspection.ready_for_extraction is True:
                return [{'delivery':'extracted_text', 'available':True, 'code':None}]
            return [{'delivery':'extracted_text', 'available':False,
                     'code':inspection.diagnostic or 'model_pdf_extraction_unavailable'}]
        code = (
                'model_video_tools_and_frame_selection_not_implemented' if inspection.kind in ('mp4','webm') else
                'model_reviewed_image_decoder_dependency_unavailable')
        return [{'delivery':'native', 'available':False, 'code':code},
                {'delivery':'extracted_text', 'available':False, 'code':code}]

    @staticmethod
    def _public_inspection(actual):
        # One explicit common projection; unrelated PDF/video fields never leak
        # into the original PNG/OOXML contracts. Keep the full real video probe.
        report = {key:actual[key] for key in ('kind','byte_length','structural_validation',
            'ready_for_extraction','converter_required','native_provider_support_verified',
            'text_characters','archive_entries','uncompressed_bytes','xml_nodes','sheets',
            'cells','formulas','slides','loss_notes','unsupported_features','context_notes')}
        report['limits'] = {key:actual['limits'][key] for key in ('max_bytes','max_entries',
            'max_entry_bytes','max_uncompressed_bytes','max_ratio','max_xml_bytes','max_xml_nodes',
            'max_xml_depth','max_sheets','max_cells','max_slides','max_text_characters')}
        if actual['kind'] == 'pdf':
            report.update({key:actual[key] for key in ('pages','pdf_objects','pdf_nodes',
                'pdf_decoded_work_bytes','pdf_image_objects','parser_version','delivery','diagnostic')})
            report['limits'].update({key:actual['limits'][key] for key in ('max_pdf_pages',
                'max_pdf_objects','max_pdf_nodes','max_pdf_depth','max_pdf_stream_bytes',
                'max_pdf_decoded_work_bytes','max_pdf_characters','max_pdf_memory_bytes',
                'max_pdf_user_cpu_seconds','max_pdf_processes','max_pdf_wall_seconds','max_pdf_output_bytes')})
        elif actual['kind'] in ('mp4','webm'):
            report.update({key:actual[key] for key in ('delivery','diagnostic','video_metadata')})
        return report

    @staticmethod
    def _public_video_material(value, *, approved=False):
        # Do not reinterpret the core's selection as a document selection. The
        # default document DTO (including pages:[]) stays unchanged and separate.
        keys = (('scope_id','material_id','fingerprint','source','delivery','parts',
            'processed_text_parts','content_bytes','characters','frame_count','loss_notes',
            'model_sent','sending_approved','sending_requires_separate_approval','frames',
            'conversion_performed') if approved else
            ('scope_id','token','fingerprint','expires_in','source','delivery','converter',
             'plan','loss_notes','conversion_performed','approval_required','model_sent',
             'sending_requires_separate_approval'))
        public = {key:value[key] for key in keys}
        public['selection'] = SelectionInput().model_dump()
        public['video_selection'] = value['video_selection']
        return public

    def inspect_material(self, principal, scope_id, fid, expected_sha):
        video = HostVideoMaterials(self).inspect_if_video(principal, scope_id, fid, expected_sha)
        if video is not None:
            return {**{key:video[key] for key in ('scope_id','source','inspection_fingerprint','model_sent')},
                'inspection':self._public_inspection(video['inspection']),
                'deliveries':[{key:item[key] for key in ('delivery','available','code')}
                    for item in video['deliveries']]}
        with self._lock:
            scope = self._scope(principal, scope_id)
            held = scope.sources.get(fid)
            if held is not None and expected_sha != held['sha256']:
                _fail('model_material_expected_sha_required_after_first_inspect', 422)
            if held is None and len(scope.sources) >= 8:
                _fail('model_attachment_count_limit', 422)
            source = self._read(scope, fid, expected_sha)
            if held is not None and source.metadata() != held:
                _fail('model_material_source_changed')
            inspection = inspect_attachment_bytes(source.filename, source.mime, source.content)
            if self._read(scope, fid, source.sha256) != source:
                _fail('model_material_source_changed')
            scope.sources[fid] = source.metadata()
            actual = inspection.to_dict()
            report = self._public_inspection(actual)
            return {'scope_id':scope_id, 'source':source.metadata(), 'inspection':report,
                'inspection_fingerprint':_digest({'source':source.metadata(), 'inspection':actual}),
                'deliveries':self._deliveries(inspection), 'model_sent':False}

    def preview_material(self, principal, scope_id, fid, expected_sha, delivery, selection, *, video_selection=None):
        # Close all six keys here before the core adapter; no browser budgets,
        # commands, optional stream defaults or extra conversion settings enter.
        if video_selection is not None and (type(video_selection) is not dict or set(video_selection) != {
                'timestamps_ms','frame_count','max_width','max_height','stream_index','output_format'}):
            _fail('explicit_video_selection_required', 422)
        video = HostVideoMaterials(self).preview_if_video(principal, scope_id, fid, expected_sha,
            delivery, selection, video_selection=video_selection)
        if video is not None:
            return self._public_video_material(video)
        with self._lock:
            scope = self._scope(principal, scope_id)
            self._invalidate_requests(scope)
            if len(scope.materials) >= 8 or len(scope.plans) >= 16:
                _fail('model_material_or_conversion_plan_capacity', 429)
            if len({plan.source.fid for plan in scope.plans.values()} |
                   {material.source.fid for material in scope.materials.values()} | {fid}) > 8:
                _fail('model_attachment_count_limit', 422)
            source = self._read(scope, fid, expected_sha)
            if scope.sources.get(fid) != source.metadata():
                _fail('model_material_inspect_identity_required_or_changed', 422)
            self._capacity(2*len(source.content))
            inspection = inspect_attachment_bytes(source.filename, source.mime, source.content)
            if not any(item['available'] and item['delivery'] == delivery for item in self._deliveries(inspection)):
                _fail(self._deliveries(inspection)[0].get('code') or 'model_delivery_not_implemented', 422)
            selected = SelectionInput.model_validate(selection).typed()
            if delivery == 'extracted_text':
                plan = preview_document_conversion(source.filename, source.mime, source.content, selected,
                    original_id=fid, original_version=source.original_version)
                self._capacity(len(source.content))
                registry_token = scope.registry.remember_conversion_plan(scope.binding, source, plan,
                    expected_source_session_id=scope.binding.session_id)
                notes = plan.loss_notes
                plan_fingerprint = plan.fingerprint
            else:
                if selected != SelectionInput().typed():
                    _fail('model_native_delivery_does_not_accept_conversion_selection', 422)
                plan = inspection
                registry_token = None
                notes = inspection.loss_notes
                plan_fingerprint = _digest(inspection.to_dict())
            if self._read(scope, fid, source.sha256) != source:
                _fail('model_material_source_changed')
            token = secrets.token_urlsafe(32)
            fingerprint = _digest({'scope':scope.binding.wire(), 'source':source.metadata(),
                'delivery':delivery, 'plan':plan_fingerprint, 'selection':asdict(selected)})
            scope.plans[token] = _MaterialPlan(source, delivery, selected, plan, fingerprint,
                                              self.clock()+300, registry_token)
            return {'scope_id':scope_id, 'token':token, 'fingerprint':fingerprint, 'expires_in':300,
                'source':source.metadata(), 'delivery':delivery, 'selection':asdict(selected),
                'converter':plan.converter if delivery == 'extracted_text' else 'none',
                'loss_notes':list(notes), 'conversion_performed':False, 'approval_required':True,
                'model_sent':False, 'sending_requires_separate_approval':True}

    def approve_material(self, principal, scope_id, token, fingerprint, user_confirmed):
        # Delegate BEFORE the ordinary path consumes its token. The adapter owns
        # the lock, atomic whole-scope budgets and actual registry-produced bytes.
        video = HostVideoMaterials(self).approve_if_video(principal, scope_id, token, fingerprint, user_confirmed)
        if video is not None:
            return self._public_video_material(video, approved=True)
        with self._lock:
            scope = self._scope(principal, scope_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            record = scope.plans.pop(token, None)
            if (record is None or self.clock() >= record.expires
                    or fingerprint != record.fingerprint or user_confirmed is not True):
                _fail('model_conversion_confirmation_invalid_or_consumed')
            source = self._read(scope, record.source.fid, record.source.sha256)
            if source != record.source:
                _fail('model_material_source_changed')
            if any(material.source.fid == source.fid for material in scope.materials.values()):
                _fail('model_duplicate_original_material')
            if len(scope.materials) >= 8:
                _fail('model_attachment_count_limit', 422)
            self._invalidate_requests(scope)
            if record.delivery == 'extracted_text':
                result = extract_document(source.filename, source.mime, source.content, record.selection,
                    expected_raw_sha=source.sha256,
                    expected_inspection_fingerprint=record.plan.inspection_fingerprint,
                    expected_plan_fingerprint=record.plan.fingerprint, user_confirmed_conversion=True,
                    original_id=source.fid, original_version=source.original_version)
                if sum(item.descriptor.characters for item in scope.materials.values())+result.characters > 100000:
                    _fail('model_total_document_character_limit_no_truncation', 413)
                self._capacity(sum(len(part.content)+2048 for part in result.parts))
                descriptor = scope.registry.register_document_result(source, result,
                    plan_token=record.registry_token, binding=scope.binding)
            else:
                self._capacity(len(source.content)+4096)
                descriptor = scope.registry.register_native_image(scope.binding, source, record.plan)
            try:
                self._capacity()
            except ModelHostError:
                scope.registry.invalidate(scope.binding, descriptor.input_id)
                raise
            scope.materials[descriptor.input_id] = _Material(source, descriptor, record.delivery)
            parts = scope.registry.preview_parts(scope.binding, descriptor.input_id)
            value = {'scope_id':scope_id, 'material_id':descriptor.input_id,
                'fingerprint':descriptor.fingerprint, 'source':source.metadata(),
                'delivery':record.delivery, 'selection':asdict(record.selection),
                'parts':[part.manifest() for part in parts],
                'processed_text_parts':[part.content.decode('utf-8') for part in parts if part.kind == 'text'],
                'content_bytes':sum(len(part.content) for part in parts),
                'characters':descriptor.characters,
                'frame_count':sum(part.delivery == 'video_frame' for part in parts),
                'loss_notes':list(descriptor.loss_notes), 'model_sent':False,
                'sending_approved':False, 'sending_requires_separate_approval':True}
            if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 8*1024**2:
                scope.registry.invalidate(scope.binding, descriptor.input_id)
                scope.materials.pop(descriptor.input_id)
                _fail('model_material_preview_response_limit_no_truncation', 413)
            return value

    def revoke_material(self, principal, scope_id, material_id):
        with self._lock:
            scope = self._scope(principal, scope_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            material = scope.materials.pop(material_id, None)
            if material is None:
                _fail('model_material_unknown', 404)
            scope.registry.invalidate(scope.binding, material_id)
            self._invalidate_requests(scope)
            return {'revoked':True, 'model_sent':False, 'external_effect_undo_guaranteed':False}

    def prepare(self, principal, scope_id, profile_id, revision, text, materials, output_tokens, effort):
        with self._lock:
            scope = self._scope(principal, scope_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            self._invalidate_requests(scope)
            selected = self.routes.resolve(profile_id, max_output_tokens=output_tokens, effort=effort)
            if selected.route.profile_revision != revision:
                _fail('model_profile_revision_changed')
            ids = [item['material_id'] for item in materials]
            if len(set(ids)) != len(ids) or len(ids) > 8:
                _fail('model_duplicate_or_excess_materials')
            parts = []
            held = []
            preflight_bytes = len(text.encode('utf-8'))*6+4096
            for item in materials:
                material = scope.materials.get(item['material_id'])
                if material is None or material.descriptor.fingerprint != item['fingerprint']:
                    _fail('model_material_fingerprint_changed')
                scope.registry.preview_descriptor(scope.binding, item['material_id'])
                # The material claim stays host-owned. A new prepare invalidates the
                # prior immutable request; the registry never accepts browser bytes.
                material_parts = scope.registry.preview_parts(scope.binding, item['material_id'])
                parts.extend(material_parts)
                preflight_bytes += sum(len(part.content)*6+2048 if part.kind == 'text'
                    else 4*((len(part.content)+2)//3)+4096 for part in material_parts)
                held.append((item['material_id'], item['fingerprint']))
            # Payload+manifest+preview copies are bounded before base64/JSON allocation.
            self._capacity(2*preflight_bytes+len(parts)*4096)
            if len(text)+sum(len(part.content.decode('utf-8')) for part in parts if part.kind == 'text') > 100000:
                _fail('model_total_text_character_limit_no_truncation', 413)
            compiled = compile_submission(selected.route, selected.evidence, text, tuple(parts))
            self._capacity(len(compiled.payload_bytes)+len(compiled.submission_manifest_bytes)
                           + len(compiled.preview_content_bytes))
            request_id = secrets.token_urlsafe(32)
            record = _Submission(compiled, selected, tuple(held), scope.revision, self.clock()+60)
            scope.requests[request_id] = record
            try:
                self._recheck(scope, record)
            except Exception:
                record.invalidated = True
                scope.requests.pop(request_id)
                raise
            return {'scope_id':scope_id, 'request_id':request_id, 'submission_sha256':compiled.payload_sha256,
                'submission_fingerprint':compiled.fingerprint, 'scope_revision':scope.revision,
                'approval_required':True, 'sent':False}

    def _recheck(self, scope, record, *, check_materials=True):
        self._scope(scope.principal, scope.binding.scope_id)
        if (record.invalidated or record.cancel.is_set() or record.revision != scope.revision
                or not record.started and self.clock() >= record.expires):
            _fail('model_submission_expired_or_invalidated')
        try:
            current = self.routes.resolve(record.route.route.profile_id,
                max_output_tokens=record.route.route.max_output_tokens, effort=record.route.route.effort)
        except Exception:
            record.invalidated = True
            raise
        if current != record.route:
            record.invalidated = True
            _fail('model_profile_capability_or_credential_changed')
        if check_materials:
            try:
                for material_id, fingerprint in record.materials:
                    descriptor = scope.registry.preview_descriptor(scope.binding, material_id)
                    if descriptor.fingerprint != fingerprint:
                        _fail('model_material_changed')
            except Exception:
                record.invalidated = True
                raise
        return True

    def preview_submission(self, principal, scope_id, request_id, expected_sha):
        with self._lock:
            scope = self._scope(principal, scope_id)
            record = scope.requests.get(request_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            if record is None or record.started or record.token is not None:
                _fail('model_pending_unknown_or_preview_consumed')
            try:
                self._recheck(scope, record)
            except Exception:
                record.invalidated = True
                raise
            if not hmac.compare_digest(record.compiled.payload_sha256, expected_sha):
                _fail('model_submission_changed')
            proposed = self.service.preview_compiled(scope.binding, record.compiled,
                                                     allow_host_configured_attempt=True)
            if proposed.get('token') is None or proposed.get('approval_required') is not True:
                record.invalidated = True
                _fail('model_submission_adapter_or_transport_unavailable')
            record.token, record.fingerprint = proposed['token'], proposed['fingerprint']
            # Compare the service DTO to the actual immutable compiler output and
            # project only its explicit public fields; never return raw internals.
            try:
                public = proposed.get('preview')
                expected = {**record.compiled.preview_to_ui(), 'endpoint':record.compiled.route.endpoint,
                    'max_response_bytes':self.service.max_response_bytes,
                    'plain_http':urlsplit(record.compiled.route.endpoint).scheme == 'http'}
                if (type(public) is not dict or set(public) != set(_FINAL_PREVIEW_FIELDS)
                        or set(expected) != set(_FINAL_PREVIEW_FIELDS) or _digest(public) != _digest(expected)):
                    _fail('model_submission_preview_contract_invalid')
                preview = {key:public[key] for key in _FINAL_PREVIEW_FIELDS}
            except Exception:
                record.invalidated = True
                self.service.cancel_approval(record.token)
                raise
            # Safe credential references stay on the outer route, never Key bytes
            # or Key hashes, browser-built identities or provider payload blocks.
            value = {'scope_id':scope_id, 'request_id':request_id,
                'submission_sha256':record.compiled.payload_sha256,
                'token':record.token, 'fingerprint':record.fingerprint,
                'expires_in':min(proposed['expires_in'], max(0, record.expires-self.clock())),
                'route':self.routes.public_metadata(record.route), 'effort':record.route.route.effort,
                'materials':[{'material_id':material_id, 'fingerprint':fingerprint}
                    for material_id, fingerprint in record.materials], 'preview':preview,
                'plain_http':preview['plain_http'],
                'requires_unverified_capabilities_acceptance':not record.compiled.ready_for_verified_adapter,
                'capability_notice':NOTICE, 'approval_required':True, 'sent':False}
            try:
                self._recheck(scope, record)
                if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 8*1024**2:
                    _fail('model_submission_preview_response_limit_no_truncation', 413)
            except Exception:
                record.invalidated = True
                self.service.cancel_approval(record.token)
                raise
            return value

    def _approval(self, scope, token, fingerprint):
        for record in scope.requests.values():
            if record.token == token and record.fingerprint == fingerprint:
                return record
        _fail('model_send_confirmation_unknown', 404)

    async def execute(self, principal, scope_id, token, fingerprint, confirmed, accept_unverified, allow_http):
        with self._lock:
            scope = self._scope(principal, scope_id)
            record = self._approval(scope, token, fingerprint)
            request_id = next(key for key, value in scope.requests.items() if value is record)
            if record.started:
                _fail('model_send_confirmation_consumed')
            if len(self._active) >= 8:
                _fail('model_host_active_execution_capacity', 429)
            try:
                self._recheck(scope, record)
            except Exception:
                record.invalidated = True
                self.service.cancel_approval(token)
                raise
            record.started = True
            self._active[token] = record
        def revalidate(material_boundary):
            with self._lock:
                # Ordinary polling checks scope/config/credential only; expensive
                # original-byte rechecks occur before wire IO and after completion.
                return self._recheck(scope, record, check_materials=material_boundary)
        try:
            result = await self.service.execute(token, fingerprint, user_confirmed=confirmed,
                deadline=self.clock()+record.compiled.route.timeout_seconds, cancel=record.cancel,
                accept_unverified_capabilities=accept_unverified, allow_plain_http=allow_http,
                revalidate=revalidate)
            # The service already strips provider internals/reasoning. Do not return
            # a raw provider response or guess provider-specific completion fields.
            if (type(result) is not dict or set(result) != {'answer','usage','finish'}
                    or type(result['answer']) is not str or '\x00' in result['answer']
                    or len(result['answer'].encode('utf-8')) > self.service.max_response_bytes
                    or type(result['usage']) is not dict
                    or set(result['usage']) != {'input_tokens','output_tokens','total_tokens'}
                    or any(value is not None and (type(value) is not int or not 0 <= value <= 2**53-1)
                        for value in result['usage'].values())
                    or result['finish'] not in ('completed','length','tool_requested','blocked','refused','incomplete')):
                _fail('model_service_result_contract_invalid')
            with self._lock:
                self._recheck(scope, record)
                if scope.requests.get(request_id) is not record:
                    _fail('model_submission_invalidated_result_unknown')
            return {'scope_id':scope_id, 'request_id':request_id,
                'submission_sha256':record.compiled.payload_sha256,
                'answer':result['answer'], 'usage':result['usage'], 'finish':result['finish']}
        finally:
            with self._lock:
                record.finished = True
                record.invalidated = True
                self.service.cancel_approval(token)
                self._active.pop(token, None)

    def cancel_submission(self, principal, scope_id, request_id):
        with self._lock:
            scope = self._scope(principal, scope_id)
            record = scope.requests.get(request_id)
            if record is None:
                _fail('model_pending_unknown', 404)
            record.cancel.set(); record.invalidated = True
            if record.token is not None:
                self.service.cancel_approval(record.token)
            return {'scope_id':scope_id, 'request_id':request_id, 'cancel_requested':True,
                'active_execution':record.started and not record.finished,
                'outcome':'not_sent' if not record.started else 'unknown' if record.finished else 'cancel_requested',
                'sent_undo_guaranteed':False, 'automatic_retry':False}

    def handoff_submission(self,principal,scope_id,request_id,expected_sha,confirmed):
        with self._lock:
            scope=self._scope(principal,scope_id)
            record=scope.requests.get(request_id)
            if (confirmed is not True or record is None or record.started or record.finished
                    or record.token is not None or record.invalidated or scope.handed_off
                    or record.compiled.payload_sha256!=expected_sha or len(scope.requests)!=1):
                _fail('model_handoff_exact_unpreviewed_request_required')
            self._recheck(scope,record)
            remaining=min(scope.expires,record.expires)-self.clock()
            if remaining<=0:_fail('model_handoff_source_expired')
            # The handoff owns the original actual registry/request and its original
            # expiry, never a reconstructed payload or a refreshed TTL. Route,
            # credential, source and principal checks continue on every boundary.
            scope.handed_off=True
            scope.expires=min(scope.expires,record.expires)
            return {'scope_id':scope_id,'request_id':request_id,'submission_sha256':expected_sha,
                'expires_in':remaining,'source_kind':'host_model_request',
                'handoff_complete':True,'sent':False,'send_approval_created':False,
                'runtime_deadline_extended':False}

    def revoke_scope(self, principal, scope_id):
        with self._lock:
            scope = self._scope(principal, scope_id)
            if scope.handed_off:
                return {'revoked':False,'sent_undo_guaranteed':False,'automatic_retry':False}
            self._dispose(scope)
            self._scopes.pop(scope_id)
            return {'revoked':True, 'sent_undo_guaranteed':False, 'automatic_retry':False}

    def close(self):
        with self._lock:
            for scope in self._scopes.values():
                self._dispose(scope)
            self._scopes.clear()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class OpenScope(Strict):
    session_id: int = Field(gt=0)


class ScopeInput(Strict):
    scope_id: str = Field(pattern=TOKEN)


class MaterialInput(ScopeInput):
    fid: str = Field(pattern=r'^[0-9a-f]{32}$')
    expected_sha256: str | None = Field(default=None, pattern=SHA)


class MaterialPreview(MaterialInput):
    expected_sha256: str = Field(pattern=SHA)
    delivery: Literal['extracted_text','native','video_frame']
    selection: SelectionInput = Field(default_factory=SelectionInput)
    video_selection: dict | None = None


class Confirmation(ScopeInput):
    token: str = Field(pattern=TOKEN)
    fingerprint: str = Field(pattern=SHA)


class MaterialApproval(Confirmation):
    user_confirmed: bool


class MaterialRevoke(ScopeInput):
    material_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class VideoFrameInput(MaterialRevoke):
    fingerprint: str = Field(pattern=SHA)
    frame: int = Field(ge=1, le=16)
    expected_sha256: str = Field(pattern=SHA)


class MaterialReference(Strict):
    material_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    fingerprint: str = Field(pattern=SHA)


class Prepare(ScopeInput):
    profile_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
    expected_profile_revision: int = Field(ge=1)
    text: str = Field(default='', max_length=100000)
    materials: list[MaterialReference] = Field(default_factory=list, max_length=8)
    max_output_tokens: int = Field(default=4096, ge=1, le=65536)
    effort: Literal['none','minimal','low','medium','high','xhigh'] | None = None


class SubmissionPreview(ScopeInput):
    request_id: str = Field(pattern=TOKEN)
    expected_submission_sha: str = Field(pattern=SHA)


class SubmissionHandoff(SubmissionPreview):
    user_confirmed: bool


class SubmissionCancel(ScopeInput):
    request_id: str = Field(pattern=TOKEN)


class Execute(Confirmation):
    user_confirmed: bool
    accept_unverified_capabilities: bool = False
    allow_plain_http: bool = False


class _BoundedRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            if request.method == 'POST':
                if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    raise HTTPException(415, {'code':'model_json_required'})
                declared = request.headers.get('content-length')
                if declared is not None and (not declared.isascii() or not declared.isdecimal() or int(declared)>MAX_BODY):
                    raise HTTPException(413, {'code':'model_request_limit'})
                raw = bytearray()
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            if len(raw)+len(chunk)>MAX_BODY:
                                raise HTTPException(413, {'code':'model_request_limit'})
                            raw.extend(chunk)
                except TimeoutError:
                    raise HTTPException(408, {'code':'model_request_timeout'}) from None
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate')
                        result[key] = value
                    return result
                try:
                    value = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
                    if type(value) is not dict:
                        raise ValueError('object required')
                except (ValueError, UnicodeError, RecursionError):
                    raise HTTPException(422, {'code':'model_json_invalid'}) from None
                request._body = bytes(raw)
            return await original(request)
        return handler


def create_router(runtime, *, principal_provider):
    """Mount inside authenticated local UI. No insecure/default authentication.

    principal_provider(Request)->HostUiPrincipal MUST authenticate the actual request
    using the host's existing protected session. Never derive it from body/query,
    a browser-supplied owner identity, arbitrary headers, PID or chat session ID.
    """
    if type(runtime) is not HostModelRuntime or not callable(principal_provider):
        raise TypeError('explicit host runtime and authenticated principal provider required')
    router = APIRouter(prefix='/v11/model-host', tags=['model-host'], route_class=_BoundedRoute)
    def principal(request):
        try:
            value = principal_provider(request)
            runtime._principal(value)
            return value
        except Exception:
            raise HTTPException(401, {'code':'model_authenticated_ui_session_required',
                'message':'需要真实已认证的本地UI会话；未登记资料或发送模型'}) from None
    def response(callback, *, binary=False):
        try:
            value = callback()
            if binary:
                if not isinstance(value, Response) or value.media_type != 'image/png':
                    _fail('model_video_frame_response_invalid', 503)
                return value
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(raw)>8*1024**2:
                _fail('model_preview_response_limit_no_truncation', 413)
            return JSONResponse(value, headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except (ModelHostError, ModelProxyError, InputRegistryError, SubmissionError,
                DocumentApiError, InspectionError, ExtractionError, VideoHostError, VideoExtractionError) as error:
            raise HTTPException(getattr(error, 'status', 409), {'code':error.code,
                'message':'资料、模型、凭据代际或批准已变化/不可用；未自动截断、重试、切换或交付。发送中断时外部结果可能未知。'}) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, {'code':'model_host_operation_unconfirmed',
                'message':'主机操作未确认；不自动重试或换模型，旧批准不能继续使用'}) from None
    @router.get('/routes')
    def routes(request:Request):
        principal(request)
        return response(runtime.routes.listing)
    @router.post('/scopes')
    def scopes(body:OpenScope, request:Request):
        owner = principal(request)
        return response(lambda:runtime.open_scope(owner, body.session_id))
    @router.post('/scopes/revoke')
    def revoke_scope(body:ScopeInput, request:Request):
        owner = principal(request)
        return response(lambda:runtime.revoke_scope(owner, body.scope_id))
    @router.post('/materials/inspect')
    def inspect(body:MaterialInput, request:Request):
        owner = principal(request)
        return response(lambda:runtime.inspect_material(owner, body.scope_id, body.fid, body.expected_sha256))
    @router.post('/materials/preview')
    def preview_material(body:MaterialPreview, request:Request):
        owner = principal(request)
        return response(lambda:runtime.preview_material(owner, body.scope_id, body.fid, body.expected_sha256,
            body.delivery, body.selection.model_dump(), video_selection=body.video_selection))
    @router.post('/materials/approve')
    def approve_material(body:MaterialApproval, request:Request):
        owner = principal(request)
        return response(lambda:runtime.approve_material(owner, body.scope_id, body.token, body.fingerprint,
            body.user_confirmed))
    @router.post('/materials/video-frame')
    def video_frame(body:VideoFrameInput, request:Request):
        owner = principal(request)
        return response(lambda:HostVideoMaterials(runtime).frame_response(owner, body.scope_id,
            body.material_id, body.fingerprint, body.frame, body.expected_sha256), binary=True)
    @router.post('/materials/revoke')
    def revoke_material(body:MaterialRevoke, request:Request):
        owner = principal(request)
        return response(lambda:runtime.revoke_material(owner, body.scope_id, body.material_id))
    @router.post('/submissions/prepare')
    def prepare(body:Prepare, request:Request):
        owner = principal(request)
        return response(lambda:runtime.prepare(owner, body.scope_id, body.profile_id,
            body.expected_profile_revision, body.text, [item.model_dump() for item in body.materials],
            body.max_output_tokens, body.effort))
    @router.post('/submissions/preview')
    def preview_submission(body:SubmissionPreview, request:Request):
        owner = principal(request)
        return response(lambda:runtime.preview_submission(owner, body.scope_id, body.request_id,
            body.expected_submission_sha))
    @router.post('/submissions/handoff')
    def handoff(body:SubmissionHandoff,request:Request):
        owner=principal(request)
        return response(lambda:runtime.handoff_submission(owner,body.scope_id,body.request_id,
            body.expected_submission_sha,body.user_confirmed))

    @router.post('/submissions/cancel')
    def cancel(body:SubmissionCancel, request:Request):
        owner = principal(request)
        return response(lambda:runtime.cancel_submission(owner, body.scope_id, body.request_id))
    @router.post('/submissions/execute')
    async def execute(body:Execute, request:Request):
        owner = principal(request)
        # Transport independently observes scope revocation, deadline and cancel.
        task = asyncio.create_task(runtime.execute(owner, body.scope_id, body.token, body.fingerprint,
            body.user_confirmed, body.accept_unverified_capabilities, body.allow_plain_http))
        try:
            while not task.done():
                if await request.is_disconnected():
                    try:
                        with runtime._lock:
                            scope = runtime._scope(owner, body.scope_id)
                            record = runtime._approval(scope, body.token, body.fingerprint)
                            record.cancel.set(); record.invalidated = True
                            runtime.service.cancel_approval(body.token)
                    except Exception:
                        pass
                    task.cancel()
                    raise HTTPException(409, {'code':'model_ui_disconnected_result_unknown'})
                await asyncio.wait((task,), timeout=.1)
            try:
                value = await task
            except Exception as error:
                return response(lambda: (_ for _ in ()).throw(error))
            return response(lambda:value)
        finally:
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=.25)
            except BaseException:
                pass
    return router
