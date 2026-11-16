"""Host-only document-extraction -> exact model-input registry.

No constructor/import I/O, paths, keys, HTTP or sending. A Python receipt alone is
not OS proof: explicit trusted source and live-binding providers own those checks.
Registration re-runs the real bounded parser with the held conversion selection;
serialized browser results and package-supplied verified flags are never accepted.
Conversion/material selection is NOT final model-send approval or an RPC grant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import secrets
import threading
import time

from app.model_attachment_inspection import AttachmentInspection, inspect_attachment_bytes
from app.model_document_api import DocumentSource
from app.model_document_extract import (ConversionPlan, ExtractionResult, ProcessedPart,
    preview_document_conversion, extract_document)
from app.model_submission import AttachmentPart, HostChatScope, InspectionReceipt, compile_submission
from app.plugin_proxy import BrokerSession

MAX_RECORDS = 128
MAX_BYTES = 192 * 1024**2
_TOKEN = re.compile(r'^[0-9a-f]{32}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')


class InputRegistryError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ProcessedInputDescriptor:
    input_id: str
    fingerprint: str
    source_sha256: str
    original_version: int
    conversion_plan_fingerprint: str
    converter: str
    part_count: int
    characters: int
    content_bytes: int
    loss_notes: tuple[str, ...]
    sending_approved: bool = False

    def metadata(self):
        return {name:getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class _PlanRecord:
    binding: BrokerSession | HostChatScope
    source: DocumentSource = field(repr=False)
    plan: ConversionPlan = field(repr=False)
    expires: float


@dataclass
class _InputRecord:
    binding: BrokerSession | HostChatScope
    source: DocumentSource = field(repr=False)
    descriptor: ProcessedInputDescriptor
    parts: tuple[AttachmentPart, ...] = field(repr=False)
    expires: float
    consumed: bool = False
    invalidated: bool = False


class ModelInputRegistry:
    """Explicit host service; no defaults, no client deserialization or file discovery.

    reader(fid, source_session_id) -> actual DocumentSource; live_binding(binding)
    -> exact current BrokerSession (default) or explicitly selected HostChatScope.
    Both must be bounded trusted-host operations; no fake plugin PID is constructed.
    Expired/consumed records are bounded tombstones, not automatically discarded.
    Create a new registry lifetime explicitly when capacity is exhausted.
    """
    def __init__(self, reader, live_binding, *, clock=time.monotonic, ttl=300,
                 max_records=MAX_RECORDS, max_bytes=64 * 1024**2, binding_type=BrokerSession):
        if not all(callable(value) for value in (reader, live_binding, clock)):
            raise TypeError('explicit trusted reader, binding provider and clock required')
        if (type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0 < ttl <= 300
                or type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS
                or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES):
            raise ValueError('bounded registry limits required')
        if binding_type not in (BrokerSession, HostChatScope):
            raise TypeError('explicit broker or host-chat binding required')
        self.binding_type = binding_type
        self.reader, self.live_binding, self.clock = reader, live_binding, clock
        self.ttl, self.maximum, self.byte_limit = ttl, max_records, max_bytes
        self._plans, self._inputs = {}, {}
        self._bytes = 0
        self._closed = False
        self._lock = threading.RLock()

    def _live(self, binding):
        if self._closed or type(binding) is not self.binding_type:
            raise InputRegistryError('registry_closed_or_binding_invalid')
        try:
            current = self.live_binding(binding)
        except Exception:
            raise InputRegistryError('live_binding_unavailable') from None
        if type(current) is not self.binding_type or current != binding:
            raise InputRegistryError('binding_changed')

    def _read(self, expected, binding):
        self._live(binding)
        try:
            actual = self.reader(expected.fid, expected.session_id)
        except Exception:
            raise InputRegistryError('source_unavailable') from None
        if type(actual) is not DocumentSource or actual != expected:
            raise InputRegistryError('source_identity_owner_or_content_changed')
        self._live(binding)
        return actual

    def _capacity(self, additional_bytes, additional_records=0):
        if (len(self._plans)+len(self._inputs)+additional_records > self.maximum
                or self._bytes+additional_bytes > self.byte_limit):
            raise InputRegistryError('registry_capacity')

    @staticmethod
    def _plan(source, selection):
        return preview_document_conversion(source.filename,source.mime,source.content,selection,
            original_id=source.fid,original_version=source.original_version)

    def remember_conversion_plan(self, binding, source, plan, *, expected_source_session_id):
        """Hold an actual host conversion plan; does not execute or approve sending.

        The caller must already own the document-conversion approval. This method
        is not exposed to a package or HTTP result body and mints no send grant.
        """
        with self._lock:
            if (type(source) is not DocumentSource or type(plan) is not ConversionPlan
                    or type(expected_source_session_id) is not int
                    or expected_source_session_id <= 0 or source.session_id != expected_source_session_id):
                raise InputRegistryError('host_source_session_and_plan_required')
            self._read(source,binding)
            self._capacity(len(source.content),1)
            fresh = self._plan(source,plan.selection)
            if (plan.approval_required is not True or plan.sending_approved is not False
                    or type(plan.original_version) is not int or fresh != plan):
                raise InputRegistryError('conversion_plan_changed')
            self._read(source,binding)
            token=secrets.token_hex(16)
            while token in self._plans or token in self._inputs:token=secrets.token_hex(16)
            self._plans[token]=_PlanRecord(binding,source,plan,self.clock()+self.ttl)
            self._bytes+=len(source.content)
            return token

    def register_document_result(self, source, result, *, plan_token, binding):
        """Consume a held plan and verify a real extraction, never trust verified JSON."""
        with self._lock:
            self._live(binding)
            record=self._plans.get(plan_token) if type(plan_token) is str else None
            if record is None or record.binding != binding:
                raise InputRegistryError('conversion_plan_unknown_or_wrong_binding')
            # Consume even a malformed result. Keep a bounded tombstone so replay
            # and capacity accounting cannot be defeated with forged registrations.
            self._plans[plan_token]=None
            if self.clock() >= record.expires:
                raise InputRegistryError('conversion_plan_expired')
            if type(source) is not DocumentSource or source != record.source or type(result) is not ExtractionResult:
                raise InputRegistryError('typed_conversion_result_required')
            self._read(source,binding)
            plan=self._plan(source,record.plan.selection)
            if plan != record.plan:
                raise InputRegistryError('conversion_plan_changed')
            actual=extract_document(source.filename,source.mime,source.content,plan.selection,
                expected_raw_sha=source.sha256,expected_inspection_fingerprint=plan.inspection_fingerprint,
                expected_plan_fingerprint=plan.fingerprint,user_confirmed_conversion=True,
                original_id=source.fid,original_version=source.original_version)
            if (type(result.parts) is not tuple or any(type(part) is not ProcessedPart for part in result.parts)
                    or type(result.characters) is not int or type(result.skipped_hidden) is not int
                    or type(result.loss_notes) is not tuple
                    or type(plan.converter) is not str or not 1 <= len(plan.converter) <= 100
                    or result.converter != plan.converter or actual.converter != plan.converter
                    or result.original_sha256 != source.sha256
                    or result.inspection_fingerprint != plan.inspection_fingerprint
                    or result.conversion_plan_fingerprint != plan.fingerprint
                    or result.conversion_settings_sha256 != plan.selection_fingerprint
                    or any(type(part.original_version) is not int or type(part.content) is not bytes
                        or part.converter != plan.converter
                        or part.original_id != source.fid or part.original_sha256 != source.sha256
                        or part.original_version != source.original_version
                        or part.conversion_settings_sha256 != plan.selection_fingerprint
                        or hashlib.sha256(part.content).hexdigest() != part.sha256 for part in result.parts)
                    or actual != result or result.sending_approved is not False
                    or result.requires_final_content_preview_and_send_approval is not True):
                raise InputRegistryError('conversion_result_or_selected_parts_changed')
            if not actual.parts or len(actual.parts)>100000 or actual.characters>plan.selection.max_characters:
                raise InputRegistryError('empty_or_excess_document_result')
            if plan.kind == 'pdf':
                pages = plan.selection.pages
                if not pages or pages != tuple(sorted(set(pages))):
                    raise InputRegistryError('pdf_explicit_ordered_pages_required')
                previous_page = 0
                for part in actual.parts:
                    if (len(part.locator) != 1 or part.locator[0][0] != 'page'
                            or type(part.locator[0][1]) is not str
                            or not re.fullmatch(r'[1-9][0-9]{0,2}', part.locator[0][1])):
                        raise InputRegistryError('pdf_selected_part_locator_changed')
                    page = int(part.locator[0][1])
                    if (page not in pages or page <= previous_page or part.value_type != 'text'
                            or part.formula is not None or part.cached_value is not None):
                        raise InputRegistryError('pdf_selected_parts_changed')
                    previous_page = page
            total=sum(len(part.content) for part in actual.parts)
            # Bound metadata/object growth before allocating AttachmentPart wrappers.
            # This is conservative admission, never a truncated conversion result.
            estimated_metadata=sum(1536+sum(len(key)+6*len(value) for key,value in part.locator)
                                   for part in actual.parts)
            self._capacity(total+estimated_metadata)
            original_kind='text' if plan.kind in ('txt','md') else 'document'
            parts=tuple(AttachmentPart(opaque_id=source.fid,original_sha256=source.sha256,
                original_version=source.original_version,original_kind=original_kind,
                original_mime=source.mime,original_size=len(source.content),kind='text',mime='text/plain',
                content=part.content,sha256=part.sha256,
                # A verified receipt is minted only after this registry reproduced
                # the exact real extraction above. It is not provider verification.
                inspection=InspectionReceipt(source.sha256,source.original_version,part.sha256,
                    'lingxi-bounded-document-extractor',part.converter,plan.selection_fingerprint,True),
                delivery='extracted_text',locator=part.locator,conversion_method=part.converter,
                conversion_version=part.converter,conversion_settings_sha256=plan.selection_fingerprint,
                loss_notes=actual.loss_notes) for part in actual.parts)
            self._read(source,binding)
            if self.clock() >= record.expires:
                raise InputRegistryError('conversion_plan_expired')
            input_id=secrets.token_hex(16)
            while input_id in self._plans or input_id in self._inputs:input_id=secrets.token_hex(16)
            body={'input_id':input_id,'binding':binding.wire(),'source':source.metadata(),'conversion_plan':plan.fingerprint,
                  'parts':[part.manifest() for part in parts],'characters':actual.characters,
                  'loss_notes':actual.loss_notes}
            accounted_bytes=total+len(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8'))
            self._capacity(accounted_bytes)
            descriptor=ProcessedInputDescriptor(input_id,_digest(body),source.sha256,source.original_version,
                plan.fingerprint,plan.converter,len(parts),actual.characters,total,actual.loss_notes)
            # Replace consumed plan ownership by one input slot (and a token tombstone).
            self._plans.pop(plan_token)
            self._inputs[input_id]=_InputRecord(binding,source,descriptor,parts,
                                               min(record.expires,self.clock()+self.ttl))
            self._bytes+=accounted_bytes
            return descriptor

    def register_native_image(self, binding, source, inspection):
        """Hold actual original image bytes after the real bounded decoder gate.

        Caller separately owns material registration approval; this is not a model
        send grant. No magic-only image/PDF/video receipt can use this path.
        """
        with self._lock:
            if type(source) is not DocumentSource or type(inspection) is not AttachmentInspection:
                raise InputRegistryError('typed_native_source_and_inspection_required')
            self._read(source,binding)
            actual=inspect_attachment_bytes(source.filename,source.mime,source.content)
            formats={'png':'image/png','jpeg':'image/jpeg','webp':'image/webp'}
            if (actual != inspection or actual.kind not in formats
                    or actual.structural_validation not in ('bounded_static_png_decoded','bounded_pinned_pillow_decoded')
                    or source.mime != formats[actual.kind]):
                raise InputRegistryError('native_decoder_verification_required')
            inspector = ('stdlib-static-png-v1' if actual.kind == 'png' else 'pinned-pillow-image-v1')
            settings=_digest({'inspector':inspector,'inspection':actual.to_dict()})
            part=AttachmentPart(source.fid,source.sha256,source.original_version,'image',source.mime,
                len(source.content),'image',source.mime,source.content,source.sha256,
                InspectionReceipt(source.sha256,source.original_version,source.sha256,
                    inspector,'1',settings,True),
                conversion_settings_sha256=settings,loss_notes=actual.loss_notes)
            input_id=secrets.token_hex(16)
            while input_id in self._plans or input_id in self._inputs:input_id=secrets.token_hex(16)
            body={'input_id':input_id,'binding':binding.wire(),'source':source.metadata(),
                  'parts':[part.manifest()],'inspection':settings}
            size=len(source.content)+len(json.dumps(body,ensure_ascii=False).encode('utf-8'))
            self._capacity(size,1)
            descriptor=ProcessedInputDescriptor(input_id,_digest(body),source.sha256,source.original_version,
                settings,inspector,1,0,len(source.content),actual.loss_notes)
            self._read(source,binding)
            self._inputs[input_id]=_InputRecord(binding,source,descriptor,(part,),self.clock()+self.ttl)
            self._bytes+=size
            return descriptor

    def register_video_frames(self, binding, source, plan, *, expected_source_session_id,
                              request_original_count, other_video_frames,
                              user_confirmed_conversion):
        """Execute and register actual approved core output, never a supplied result.

        The caller owns the whole host request lock/reservation and derives both
        counts from its real pending originals/materials, not browser claims. This
        method never accepts a serialized VideoExtractionResult or verified flag;
        only the direct real extraction return can mint these frame receipts.
        """
        from app.model_video_extract import (MAX_RESULT_BYTES, VERSION, VideoConversionPlan,
            VideoExtractionResult, VideoProcessedPart, extract_video_frames,
            preview_video_conversion, validate_video_request_budget)
        with self._lock:
            if (type(source) is not DocumentSource or type(plan) is not VideoConversionPlan
                    or type(binding) is not HostChatScope or binding.session_id != source.session_id
                    or type(expected_source_session_id) is not int
                    or expected_source_session_id != source.session_id
                    or user_confirmed_conversion is not True):
                raise InputRegistryError('host_video_source_plan_and_approval_required')
            self._read(source,binding)
            validate_video_request_budget(plan.selection,request_original_count=request_original_count,
                                          other_video_frames=other_video_frames)
            self._capacity(len(source.content)+MAX_RESULT_BYTES+plan.frame_count*4096,1)
            fresh=preview_video_conversion(source.filename,source.mime,source.content,plan.selection,
                original_id=source.fid,original_version=source.original_version,
                request_original_count=request_original_count,other_video_frames=other_video_frames)
            if (fresh != plan or plan.approval_required is not True
                    or plan.sending_approved is not False or plan.converter != VERSION):
                raise InputRegistryError('video_conversion_plan_changed')
            self._read(source,binding)
            actual=extract_video_frames(source.filename,source.mime,source.content,plan.selection,
                original_id=source.fid,original_version=source.original_version,
                request_original_count=request_original_count,other_video_frames=other_video_frames,
                expected_raw_sha=source.sha256,
                expected_inspection_fingerprint=plan.inspection_fingerprint,
                expected_plan_fingerprint=plan.fingerprint,user_confirmed_conversion=True)
            # This object was returned directly above, never reconstructed by the
            # browser. Core already performed the exact PNG/CRC/deflate/Pillow gate;
            # do not send frames through the unrelated old 8-MiB static-PNG gate.
            if (type(actual) is not VideoExtractionResult or type(actual.parts) is not tuple
                    or actual.frame_count != plan.frame_count or len(actual.parts) != plan.frame_count
                    or actual.original_id != source.fid or actual.original_version != source.original_version
                    or actual.original_sha256 != source.sha256
                    or actual.inspection_fingerprint != plan.inspection_fingerprint
                    or actual.conversion_plan_fingerprint != plan.fingerprint
                    or actual.conversion_settings_sha256 != plan.conversion_settings_sha256
                    or actual.loss_notes != plan.loss_notes or actual.sending_approved is not False
                    or actual.requires_final_content_preview_and_send_approval is not True):
                raise InputRegistryError('actual_video_result_binding_changed')
            for index,(part,timestamp) in enumerate(zip(actual.parts,plan.selection.timestamps_ms),1):
                if (type(part) is not VideoProcessedPart or type(part.content) is not bytes
                        or part.original_id != source.fid or part.original_version != source.original_version
                        or part.original_sha256 != source.sha256 or part.mime != 'image/png'
                        or part.width != plan.output_width or part.height != plan.output_height
                        or part.converter != VERSION
                        or part.conversion_settings_sha256 != plan.conversion_settings_sha256
                        or part.locator != (('timestamp_ms',str(timestamp)),('frame',str(index)))
                        or hashlib.sha256(part.content).hexdigest() != part.sha256):
                    raise InputRegistryError('actual_video_frame_binding_changed')
            if sum(len(part.content) for part in actual.parts) != actual.content_bytes:
                raise InputRegistryError('actual_video_result_byte_count_changed')
            parts=tuple(AttachmentPart(opaque_id=source.fid,original_sha256=source.sha256,
                original_version=source.original_version,original_kind='video',original_mime=source.mime,
                original_size=len(source.content),kind='image',mime='image/png',content=part.content,
                sha256=part.sha256,
                inspection=InspectionReceipt(source.sha256,source.original_version,part.sha256,
                    'lingxi-bounded-video-extractor',VERSION,plan.conversion_settings_sha256,True),
                delivery='video_frame',locator=part.locator,conversion_method=VERSION,
                conversion_version=VERSION,conversion_settings_sha256=plan.conversion_settings_sha256,
                loss_notes=actual.loss_notes) for part in actual.parts)
            self._read(source,binding)
            return self._store_actual_video_parts(binding,source,parts,plan.fingerprint,VERSION,
                                                  actual.loss_notes)

    def register_native_video(self, binding, source, *, expected_source_session_id,
                              expected_inspection_fingerprint, user_confirmed_material):
        """Real bounded re-probe of exact raw bytes; no native capability/send grant.

        The compiler must still require exact-route declared video MIME + implemented
        native mapping. It must never silently substitute frames for this original.
        """
        from app.model_video_extract import VERSION, native_video_attachment
        with self._lock:
            if (type(source) is not DocumentSource or type(binding) is not HostChatScope
                    or binding.session_id != source.session_id
                    or type(expected_source_session_id) is not int
                    or expected_source_session_id != source.session_id
                    or user_confirmed_material is not True
                    or type(expected_inspection_fingerprint) is not str
                    or not _SHA.fullmatch(expected_inspection_fingerprint)):
                raise InputRegistryError('host_native_video_source_and_approval_required')
            self._read(source,binding)
            self._capacity(2*len(source.content)+8192,1)
            # The helper itself invokes probe_video on these held bytes. A browser
            # dataclass, magic-only inspection or caller verified=True never enters.
            part=native_video_attachment(source.filename,source.mime,source.content,
                original_id=source.fid,original_version=source.original_version,
                expected_inspection_fingerprint=expected_inspection_fingerprint)
            if (type(part) is not AttachmentPart or part.delivery != 'native'
                    or part.original_kind != 'video' or part.kind != 'video'
                    or part.opaque_id != source.fid or part.content != source.content
                    or part.sha256 != source.sha256 or part.original_version != source.original_version
                    or part.inspection.inspector != VERSION):
                raise InputRegistryError('actual_native_video_binding_changed')
            self._read(source,binding)
            return self._store_actual_video_parts(binding,source,(part,),
                expected_inspection_fingerprint,VERSION,part.loss_notes)

    def _store_actual_video_parts(self, binding, source, parts, plan_fingerprint, converter, notes):
        """Private lock-held sink, only after a direct real core return above."""
        input_id=secrets.token_hex(16)
        while input_id in self._plans or input_id in self._inputs:input_id=secrets.token_hex(16)
        body={'input_id':input_id,'binding':binding.wire(),'source':source.metadata(),
              'conversion_plan':plan_fingerprint,'parts':[part.manifest() for part in parts],
              'loss_notes':notes,'actual_core_result':True}
        total=sum(len(part.content) for part in parts)
        size=len(source.content)+total+len(json.dumps(body,ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode('utf-8'))
        self._capacity(size,1)
        self._read(source,binding)
        descriptor=ProcessedInputDescriptor(input_id,_digest(body),source.sha256,source.original_version,
            plan_fingerprint,converter,len(parts),0,total,notes)
        self._inputs[input_id]=_InputRecord(binding,source,descriptor,parts,self.clock()+self.ttl)
        self._bytes+=size
        return descriptor

    def preview_parts(self, binding, input_id):
        """Trusted host-only exact preview; never accept a browser result as input."""
        with self._lock:
            return self._record(binding,input_id).parts

    def _record(self, binding, input_id):
        self._live(binding)
        if type(input_id) is not str or not _TOKEN.fullmatch(input_id):
            raise InputRegistryError('input_unknown')
        record=self._inputs.get(input_id)
        if record is None or record.binding != binding:
            raise InputRegistryError('input_unknown_or_wrong_binding')
        if record.invalidated or self.clock()>=record.expires:
            raise InputRegistryError('input_expired_or_invalidated')
        try:
            self._read(record.source,binding)
            for part in record.parts:
                if hashlib.sha256(part.content).hexdigest()!=part.sha256:
                    raise InputRegistryError('processed_part_changed')
                if part.delivery == 'extracted_text' and (
                        part.conversion_method != record.descriptor.converter
                        or part.conversion_version != record.descriptor.converter
                        or part.inspection.inspector != 'lingxi-bounded-document-extractor'
                        or part.inspection.inspector_version != record.descriptor.converter
                        or part.inspection.settings_sha256 != part.conversion_settings_sha256
                        or part.inspection.verified is not True):
                    raise InputRegistryError('processed_part_conversion_receipt_changed')
            if self.clock()>=record.expires:
                raise InputRegistryError('input_expired_or_invalidated')
        except InputRegistryError:
            record.invalidated=True
            raise
        return record

    def preview_descriptor(self, binding, input_id):
        with self._lock:
            record=self._record(binding,input_id)
            self._read(record.source,binding)
            return record.descriptor

    def consume_for_compilation(self, binding, input_id, fingerprint):
        """One host material claim, NOT a UI confirmation or model-send approval.

        Call revalidate_for_submission after compilation, or use compile_input()
        which performs both checks itself. Do not send previously returned parts
        without the exact live source/binding recheck at the next approval boundary.
        """
        with self._lock:
            record=self._record(binding,input_id)
            if record.consumed:raise InputRegistryError('input_already_consumed')
            record.consumed=True
            if type(fingerprint) is not str or not _SHA.fullmatch(fingerprint) or fingerprint!=record.descriptor.fingerprint:
                raise InputRegistryError('input_fingerprint_changed')
            self._read(record.source,binding)
            return record.parts

    def revalidate_for_submission(self, binding, input_id, fingerprint):
        with self._lock:
            record=self._record(binding,input_id)
            if fingerprint!=record.descriptor.fingerprint or not record.consumed:
                raise InputRegistryError('compilation_claim_required')
            self._read(record.source,binding)
            return True

    def compile_input(self, binding, input_id, fingerprint, route, evidence, text=''):
        """Actual model_submission compiler with source/binding CAS before and after."""
        with self._lock:
            parts=self.consume_for_compilation(binding,input_id,fingerprint)
            compiled=compile_submission(route,evidence,text,parts)
            self.revalidate_for_submission(binding,input_id,fingerprint)
            return compiled

    def audit_metadata(self, binding, input_id):
        with self._lock:
            record=self._record(binding,input_id)
            identity = ({'plugin_id':binding.plugin_id,'call_id':binding.call_id}
                        if type(binding) is BrokerSession else
                        {'scope_kind':'host_chat','scope_id':binding.scope_id})
            return {'input_id':input_id,'fingerprint':record.descriptor.fingerprint, **identity,
                    'part_count':len(record.parts),'characters':record.descriptor.characters,
                    'content_bytes':record.descriptor.content_bytes,'sending_approved':False,
                    'consumed_for_compilation':record.consumed}

    def invalidate(self, binding, input_id):
        with self._lock:
            self._live(binding)
            record=self._inputs.get(input_id)
            if record is None or record.binding!=binding:
                raise InputRegistryError('input_unknown_or_wrong_binding')
            record.invalidated=True

    @property
    def retained_bytes(self):
        """Conservative host capacity accounting; contains no payload or source path."""
        with self._lock:
            return self._bytes

    def close(self):
        """Explicitly discard this in-memory lifetime; never writes/deletes any file."""
        with self._lock:
            self._closed=True
            self._plans.clear();self._inputs.clear();self._bytes=0
