"""Pure, immutable model-submission compiler; NEVER parses files, loads keys or sends.

All attachments must already have a trusted host inspection receipt. These Python
objects are contracts, not proof that a real decoder/provider was exercised. A
configured capability remains provisional; only verified evidence can make a
compiled proposal verification-ready. This module mints no execution grants.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

MIB = 1024**2
MAX_ORIGINAL_BYTES = 15 * MIB
MAX_ATTACHMENTS = 8
MAX_VIDEO_FRAMES = 16
MAX_PROCESSED_PARTS = 5000  # Matches the bounded conversion-result contract; not an attachment/frame budget.
MAX_DOCUMENT_CHARS = 100000
MAX_WIRE_BYTES = 192 * MIB  # 8 x 15 MiB originals need up to 160 MiB of base64.
PROTOCOLS = ('openai_chat', 'openai_responses', 'anthropic_messages', 'gemini_content')
KINDS = ('text', 'image', 'video', 'document')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_IMAGES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')
_DOCUMENTS = ('application/pdf', 'text/plain', 'text/markdown',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation')
_VIDEOS = ('video/mp4', 'video/webm', 'video/quicktime')


class SubmissionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise SubmissionError(code)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail('invalid_finite_json')


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _sha(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        _fail('invalid_sha256')


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        _fail('invalid_opaque_identifier')


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail('invalid_integer')


def _text(value, maximum, empty=False):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or (not empty and not value.strip())):
        _fail('invalid_text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        _fail('invalid_text')


@dataclass(frozen=True)
class HostChatScope:
    """Host-minted authenticated UI scope, deliberately NOT a BrokerSession/PID.

    The live authority provider must recheck the authenticated owner, source chat
    session and revocation state. This contract performs no OS authentication.
    """
    scope_id: str
    owner_session_id: str = field(repr=False)
    session_id: int
    authority_fingerprint: str

    def __post_init__(self):
        _id(self.scope_id); _id(self.owner_session_id)
        _integer(self.session_id, 1, 2**53-1); _sha(self.authority_fingerprint)

    def wire(self):
        return {'scope_kind':'host_chat', **{name:getattr(self, name) for name in self.__dataclass_fields__}}


@dataclass(frozen=True)
class RouteBinding:
    profile_id: str
    profile_revision: int
    provider_id: str
    protocol: str
    endpoint: str = field(repr=False)
    model: str
    credential_authority: str = field(repr=False)
    credential_generation: int
    max_output_tokens: int = 4096
    timeout_seconds: float = 90
    effort: str | None = None

    def __post_init__(self):
        for item in (self.profile_id, self.provider_id, self.credential_authority):
            _id(item)
        _integer(self.profile_revision, 1, 2**53-1)
        _integer(self.credential_generation, 1, 2**53-1)
        _integer(self.max_output_tokens, 1, 65536)
        _text(self.model, 200)
        if self.protocol not in PROTOCOLS:
            _fail('unsupported_protocol')
        _text(self.endpoint, 4000)
        url = urlsplit(self.endpoint)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment or '\\' in self.endpoint
                or any(ord(c) < 32 for c in self.endpoint)):
            _fail('invalid_exact_endpoint')
        try:
            url.port
        except ValueError:
            _fail('invalid_exact_endpoint')
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 300):
            _fail('invalid_timeout')
        if self.effort is not None:
            if self.effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
                _fail('unsupported_effort')
            if self.protocol not in ('openai_chat', 'openai_responses'):
                _fail('effort_mapping_not_implemented')

    def private_binding(self):
        return {name:getattr(self, name) for name in self.__dataclass_fields__}

    @property
    def fingerprint(self):
        return _hash(_json(self.private_binding()))


@dataclass(frozen=True)
class CapabilitiesEvidence:
    """Host-supplied exact-route evidence, not package-declared model-name guessing."""
    route_fingerprint: str
    source: str  # host_configured / verified_provider
    revision: int
    supported_kinds: tuple[str, ...]
    native_mimes: tuple[str, ...]
    receipt_fingerprint: str
    max_video_frames: int = MAX_VIDEO_FRAMES
    supported_efforts: tuple[str, ...] = ()

    def __post_init__(self):
        _sha(self.route_fingerprint); _sha(self.receipt_fingerprint)
        _integer(self.revision, 1, 2**53-1)
        if self.source not in ('host_configured', 'verified_provider'):
            _fail('unverified_capability_source')
        if (type(self.supported_kinds) is not tuple or len(set(self.supported_kinds)) != len(self.supported_kinds)
                or any(kind not in KINDS for kind in self.supported_kinds)):
            _fail('invalid_capability_kinds')
        if (type(self.native_mimes) is not tuple or len(set(self.native_mimes)) != len(self.native_mimes)
                or any(mime not in (*_IMAGES, *_DOCUMENTS, *_VIDEOS) for mime in self.native_mimes)):
            _fail('invalid_native_mimes')
        _integer(self.max_video_frames, 1, MAX_VIDEO_FRAMES)
        if (type(self.supported_efforts) is not tuple or len(set(self.supported_efforts)) != len(self.supported_efforts)
                or any(value not in ('none','minimal','low','medium','high','xhigh') for value in self.supported_efforts)):
            _fail('invalid_effort_capability')

    @property
    def fingerprint(self):
        return _hash(_json({name:getattr(self, name) for name in self.__dataclass_fields__}))


@dataclass(frozen=True)
class InspectionReceipt:
    """Attested upstream host inspection; no decoder/Office/FFmpeg is run here."""
    original_sha256: str
    original_version: int
    part_sha256: str
    inspector: str
    inspector_version: str
    settings_sha256: str
    verified: bool = False

    def __post_init__(self):
        for value in (self.original_sha256, self.part_sha256, self.settings_sha256):
            _sha(value)
        _integer(self.original_version, 1, 2**53-1)
        _text(self.inspector, 100); _text(self.inspector_version, 100)
        if type(self.verified) is not bool:
            _fail('invalid_inspection_state')

    @property
    def fingerprint(self):
        return _hash(_json({name:getattr(self, name) for name in self.__dataclass_fields__}))


@dataclass(frozen=True)
class AttachmentPart:
    opaque_id: str
    original_sha256: str
    original_version: int
    original_kind: str
    original_mime: str
    original_size: int
    kind: str
    mime: str
    content: bytes = field(repr=False)
    sha256: str
    inspection: InspectionReceipt
    delivery: str = 'native'  # native / extracted_text / video_frame
    locator: tuple[tuple[str, str], ...] = ()
    conversion_method: str = 'none'
    conversion_version: str = '1'
    conversion_settings_sha256: str = '0'*64
    loss_notes: tuple[str, ...] = ()

    def __post_init__(self):
        _id(self.opaque_id); _sha(self.original_sha256); _sha(self.sha256)
        _integer(self.original_version, 1, 2**53-1)
        _integer(self.original_size, 1, MAX_ORIGINAL_BYTES)
        if self.kind not in KINDS or self.original_kind not in KINDS:
            _fail('invalid_attachment_kind')
        allowed = {'text':('text/plain', 'text/markdown'), 'image':_IMAGES,
                   'video':_VIDEOS, 'document':_DOCUMENTS}
        if self.mime not in allowed[self.kind] or self.original_mime not in allowed[self.original_kind]:
            _fail('unsupported_mime_or_legacy_macro_encrypted_format')
        if type(self.content) is not bytes or not 0 < len(self.content) <= MAX_ORIGINAL_BYTES:
            _fail('part_bytes_limit')
        if _hash(self.content) != self.sha256:
            _fail('part_hash_mismatch')
        if type(self.inspection) is not InspectionReceipt or not self.inspection.verified:
            _fail('host_inspection_required')
        if (self.inspection.original_sha256 != self.original_sha256
                or self.inspection.original_version != self.original_version
                or self.inspection.part_sha256 != self.sha256
                or self.inspection.settings_sha256 != self.conversion_settings_sha256):
            _fail('inspection_binding_mismatch')
        _sha(self.conversion_settings_sha256)
        _text(self.conversion_method, 100); _text(self.conversion_version, 100)
        if type(self.locator) is not tuple or len(self.locator) > 5:
            _fail('invalid_locator')
        names=set()
        for item in self.locator:
            if type(item) is not tuple or len(item) != 2 or item[0] not in ('page', 'sheet', 'slide', 'timestamp_ms', 'frame', 'document', 'paragraph', 'table', 'row', 'cell') or item[0] in names:
                _fail('invalid_locator')
            names.add(item[0]); _text(item[1], 255)
            key, value = item
            if key in ('page','slide','paragraph','table','row') and (not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 1048576):
                _fail('invalid_document_locator')
            if key == 'document' and value != 'text':
                _fail('invalid_document_locator')
            if key == 'cell' and not (re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}',value) or value.isascii() and value.isdecimal() and 1 <= int(value) <= 16384):
                _fail('invalid_document_locator')
        if type(self.loss_notes) is not tuple or len(self.loss_notes) > 20:
            _fail('invalid_loss_notes')
        for note in self.loss_notes:
            _text(note, 500)
        if self.delivery == 'native':
            if (self.kind != self.original_kind or self.mime != self.original_mime
                    or self.sha256 != self.original_sha256 or len(self.content) != self.original_size
                    or self.conversion_method != 'none'):
                _fail('native_original_mismatch')
        elif self.delivery == 'extracted_text':
            if self.kind != 'text' or self.original_kind not in ('document','text') or self.conversion_method == 'none' or not self.loss_notes:
                _fail('extraction_provenance_required')
        elif self.delivery == 'video_frame':
            if (self.kind != 'image' or self.original_kind != 'video' or self.conversion_method == 'none'
                    or not self.loss_notes or not {'timestamp_ms', 'frame'} <= names):
                _fail('frame_provenance_required')
            for key,value in self.locator:
                if key in ('timestamp_ms','frame') and (not value.isascii() or not value.isdecimal()):
                    _fail('invalid_frame_locator')
        else:
            _fail('unsupported_delivery')
        if self.kind == 'text':
            try:
                self.content.decode('utf-8')
            except UnicodeError:
                _fail('text_not_utf8')

    def manifest(self):
        return {'opaque_id':self.opaque_id, 'original_sha256':self.original_sha256,
            'original_version':self.original_version, 'original_kind':self.original_kind,
            'original_mime':self.original_mime, 'original_size':self.original_size,
            'kind':self.kind, 'mime':self.mime, 'part_sha256':self.sha256,
            'part_size':len(self.content), 'delivery':self.delivery, 'locator':dict(self.locator),
            'conversion_method':self.conversion_method, 'conversion_version':self.conversion_version,
            'conversion_settings_sha256':self.conversion_settings_sha256,
            'loss_notes':list(self.loss_notes), 'inspection_fingerprint':self.inspection.fingerprint}


@dataclass(frozen=True)
class CompiledSubmission:
    route: RouteBinding = field(repr=False)
    capability_fingerprint: str
    payload_bytes: bytes = field(repr=False)
    submission_manifest_bytes: bytes = field(repr=False)
    preview_content_bytes: bytes = field(repr=False)
    ready_for_verified_adapter: bool
    estimated_text_tokens: int
    media_token_estimate: int | None
    verification_note: str

    @property
    def payload_sha256(self):
        return _hash(self.payload_bytes)

    @property
    def fingerprint(self):
        return _hash(_json({'route':self.route.fingerprint,'capability':self.capability_fingerprint,
                          'payload':self.payload_sha256,'manifest':_hash(self.submission_manifest_bytes)}))

    def audit_metadata(self):
        """Only bounded identities, hashes and counts; never the prompt/media bytes."""
        manifest=json.loads(self.submission_manifest_bytes)
        return {'profile_id':self.route.profile_id,'profile_revision':self.route.profile_revision,
            'provider_id':self.route.provider_id,'protocol':self.route.protocol,'model':self.route.model,
            'route_fingerprint':self.route.fingerprint,'payload_sha256':self.payload_sha256,
            'wire_bytes':len(self.payload_bytes),'submission_fingerprint':self.fingerprint,
            'attachment_count':manifest['attachment_count'],'part_count':manifest['part_count'],
            'text_sha256':manifest['text_sha256'],'max_output_tokens':manifest['max_output_tokens']}

    def preview_to_ui(self):
        """Host-side preview; exact text and attachment hashes, no Key/credential reference.

        The host can inspect exact immutable payload_bytes separately. Binary media
        preview widgets must be bound to these hashes; base64 is not shown as text.
        """
        manifest=json.loads(self.submission_manifest_bytes)
        return {**manifest, **json.loads(self.preview_content_bytes), 'provider_id':self.route.provider_id,'protocol':self.route.protocol,
            'model':self.route.model,'profile_id':self.route.profile_id,
            'profile_revision':self.route.profile_revision,'route_fingerprint':self.route.fingerprint,
            'capability_fingerprint':self.capability_fingerprint,'payload_sha256':self.payload_sha256,
            'wire_bytes':len(self.payload_bytes),'submission_fingerprint':self.fingerprint,
            'ready_for_verified_adapter':self.ready_for_verified_adapter,
            'verification_note':self.verification_note,'estimated_text_tokens':self.estimated_text_tokens,
            'media_token_estimate':self.media_token_estimate,'token_counts_are_estimates':True,
            'requires_separate_explicit_approval':True,'execution_grant_minted':False,
            'automatic_history_persona_memory_tools':False}


def _native_allowed(part, evidence):
    if part.kind not in evidence.supported_kinds or part.mime not in evidence.native_mimes:
        _fail('native_media_capability_not_declared')


def _block(part, protocol, evidence):
    if part.kind == 'text':
        text=part.content.decode('utf-8')
        if protocol == 'openai_responses':
            return {'type':'input_text','text':text}
        if protocol == 'gemini_content':
            return {'text':text}
        return {'type':'text','text':text}
    _native_allowed(part,evidence)
    mapping_supported = (part.kind == 'image'
        or part.kind == 'document' and part.mime == 'application/pdf' and protocol != 'openai_chat'
        or part.kind == 'video' and protocol == 'gemini_content')
    if not mapping_supported:
        _fail('native_kind_protocol_mapping_not_implemented_use_explicit_extraction')
    encoded=base64.b64encode(part.content).decode('ascii')
    if part.kind == 'image':
        if protocol == 'openai_chat':
            return {'type':'image_url','image_url':{'url':f'data:{part.mime};base64,{encoded}'}}
        if protocol == 'openai_responses':
            return {'type':'input_image','image_url':f'data:{part.mime};base64,{encoded}'}
        if protocol == 'anthropic_messages':
            return {'type':'image','source':{'type':'base64','media_type':part.mime,'data':encoded}}
        return {'inlineData':{'mimeType':part.mime,'data':encoded}}
    if part.kind == 'document' and part.mime == 'application/pdf':
        if protocol == 'openai_responses':
            return {'type':'input_file','filename':part.opaque_id+'.pdf','file_data':'data:application/pdf;base64,'+encoded}
        if protocol == 'anthropic_messages':
            return {'type':'document','source':{'type':'base64','media_type':'application/pdf','data':encoded}}
        if protocol == 'gemini_content':
            return {'inlineData':{'mimeType':part.mime,'data':encoded}}
    if part.kind == 'video' and protocol == 'gemini_content':
        return {'inlineData':{'mimeType':part.mime,'data':encoded}}
    # Declared model support cannot invent an encoder this compiler does not have.
    _fail('native_kind_protocol_mapping_not_implemented_use_explicit_extraction')


def compile_submission(route: RouteBinding, evidence: CapabilitiesEvidence, text: str,
                       parts: tuple[AttachmentPart, ...] = (), *, max_wire_bytes=MAX_WIRE_BYTES):
    if type(route) is not RouteBinding or type(evidence) is not CapabilitiesEvidence:
        _fail('trusted_route_and_capability_objects_required')
    if route.fingerprint != evidence.route_fingerprint:
        _fail('capability_route_changed')
    if 'text' not in evidence.supported_kinds:
        _fail('text_capability_not_declared')
    if route.effort is not None and route.effort not in evidence.supported_efforts:
        _fail('effort_capability_not_declared')
    _integer(max_wire_bytes, 1024, MAX_WIRE_BYTES)
    # Bound character count before allocating a potentially much larger UTF-8/JSON
    # copy. The conservative upper estimate rejects, never truncates, input.
    if type(text) is not str or len(text) > max_wire_bytes // 6:
        _fail('wire_size_estimate_exceeds_limit')
    _text(text, max_wire_bytes // 6, empty=True)
    if type(parts) is not tuple or len(parts) > MAX_PROCESSED_PARTS:
        _fail('invalid_attachment_parts')
    originals={}; frames={}; native_ids=set(); total_frames=0; document_chars=0; estimated_bytes=len(text.encode('utf-8'))*6+4096
    manifests=[]; text_parts=[]; media_count=0
    for part in parts:
        if type(part) is not AttachmentPart:
            _fail('trusted_attachment_object_required')
        identity=(part.original_sha256,part.original_version,part.original_kind,part.original_mime,part.original_size)
        if part.opaque_id in originals and originals[part.opaque_id] != identity:
            _fail('attachment_identity_changed')
        originals[part.opaque_id]=identity
        if len(originals)>MAX_ATTACHMENTS:
            _fail('attachment_count_limit')
        if part.delivery=='native':
            if part.opaque_id in native_ids or any(p['opaque_id']==part.opaque_id for p in manifests):
                _fail('duplicate_or_mixed_native_attachment')
            native_ids.add(part.opaque_id)
        elif part.opaque_id in native_ids:
            _fail('duplicate_or_mixed_native_attachment')
        if part.delivery=='video_frame':
            locator=dict(part.locator);key=(locator['frame'],locator['timestamp_ms'])
            used=frames.setdefault(part.opaque_id,set())
            if key in used:
                _fail('duplicate_video_frame')
            used.add(key)
            total_frames+=1
            if total_frames>min(MAX_VIDEO_FRAMES,evidence.max_video_frames):
                _fail('video_frame_limit')
        if part.kind=='text':
            content=part.content.decode('utf-8');text_parts.append(content)
            if part.original_kind in ('text','document'):
                document_chars+=len(content)
            estimated_bytes+=len(part.content)*6+2048  # JSON escaping worst case.
        else:
            media_count+=1
            estimated_bytes+=4*((len(part.content)+2)//3)+4096
        if document_chars>MAX_DOCUMENT_CHARS:
            _fail('document_text_limit_no_silent_truncation')
        if estimated_bytes>max_wire_bytes:
            _fail('wire_size_estimate_exceeds_limit')
        manifests.append(part.manifest())
    if not text and not parts:
        _fail('empty_submission')
    if estimated_bytes>max_wire_bytes:
        _fail('wire_size_estimate_exceeds_limit')
    if route.protocol=='openai_responses':
        blocks=([{'type':'input_text','text':text}] if text else [])+[_block(p,route.protocol,evidence) for p in parts]
        payload={'model':route.model,'input':[{'role':'user','content':blocks}],
                 'max_output_tokens':route.max_output_tokens,'stream':False,'store':False}
        if route.effort is not None:payload['reasoning']={'effort':route.effort}
    elif route.protocol=='gemini_content':
        blocks=([{'text':text}] if text else [])+[_block(p,route.protocol,evidence) for p in parts]
        payload={'contents':[{'role':'user','parts':blocks}],
                 'generationConfig':{'maxOutputTokens':route.max_output_tokens}}
    else:
        blocks=([{'type':'text','text':text}] if text else [])+[_block(p,route.protocol,evidence) for p in parts]
        payload={'model':route.model,'messages':[{'role':'user','content':blocks}],
                 'max_tokens':route.max_output_tokens,'stream':False}
        if route.protocol=='openai_chat' and route.effort is not None:payload['reasoning_effort']=route.effort
    raw=_json(payload)
    if len(raw)>max_wire_bytes:
        _fail('wire_size_limit_no_silent_truncation')
    manifest={'schema':'lingxi.model-submission.v1','text_sha256':_hash(text.encode('utf-8')),
              'text_characters':len(text),'attachments':manifests,
              'attachment_count':len(originals),'part_count':len(parts),
              'processed_text_sha256':[_hash(p.encode('utf-8')) for p in text_parts],
              'max_output_tokens':route.max_output_tokens,
              'timeout_seconds':route.timeout_seconds,'capability_source':evidence.source,
              'limits':{'max_original_bytes':MAX_ORIGINAL_BYTES,'max_attachments':MAX_ATTACHMENTS,
                        'max_video_frames':MAX_VIDEO_FRAMES,'max_document_chars':MAX_DOCUMENT_CHARS,
                        'max_wire_bytes':max_wire_bytes},'loss_notes':[note for p in parts for note in p.loss_notes]}
    ready=evidence.source=='verified_provider'
    return CompiledSubmission(route,evidence.fingerprint,raw,_json(manifest),
        _json({'text':text,'processed_text_parts':text_parts}),ready,
        math.ceil((len(text)+sum(map(len,text_parts)))/4),None if media_count else 0,
        'Host-verified wire mapping; real adapter, approval and output limits remain required.' if ready else
        'Host-configured capabilities only: not verified against the provider; execution is not ready.')
