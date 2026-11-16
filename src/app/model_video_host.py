"""Authenticated host video-material adapter; no import/construction I/O or sending.

Delegate from the existing HostModelRuntime, never create a second authority or
material store. All retained sources/plans/parts live in its real scope + registry.
Inspection and plan preview may run bounded ffprobe; only approval runs ffmpeg.
The registry calls the real extractor itself and never accepts browser results.
Native MP4/WebM remains unchanged bytes; route rejection never switches to frames.

Four integration points (do not bypass the runtime's authenticated router):
1. inspect_material: return inspect_if_video(...) when it is not None.
2. preview_material: return preview_if_video(...) when it is not None; pass the
   video_selection JSON separately from the existing document selection.
3. approve_material: return approve_if_video(...) when it is not None.
4. authenticated frame-preview endpoint: call frame_response(...), never serve a
   path or expose the original media. Existing prepare/compile/send consumes the
   scope.registry parts unchanged; it needs no parallel video payload builder.

The video_selection DTO accepts only timestamps_ms/frame_count/max_width/
max_height/stream_index/output_format; request counts are always host-derived.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
import secrets

from app.model_attachment_inspection import inspect_attachment_bytes
from app.model_document_api import DocumentSource
from app.model_input_registry import InputRegistryError
from app.model_submission import HostChatScope
from app.model_video_extract import (MAX_FRAMES, MAX_ORIGINALS, MAX_RESULT_BYTES,
    VERSION, VideoConversionPlan, VideoExtractionError, VideoSelection,
    preview_video_conversion, video_dependency_diagnostic)

_SHA = re.compile(r'^[0-9a-f]{64}$')
_PROBED = 'bounded_ffprobe_container_streams_validated'
_VIDEO_MIMES = ('video/mp4','video/webm')
_CODECS = frozenset(('h264','hevc','av1','vp8','vp9'))


class VideoHostError(ValueError):
    """Fixed metadata-only error, suitable for the existing host error mapper."""
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _fail(code, status=409):
    raise VideoHostError(code,status)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def video_selection_from_json(value):
    """Closed browser DTO -> untrusted typed selection, never a verified receipt."""
    required={'timestamps_ms','frame_count','max_width','max_height'}
    optional={'stream_index','output_format'}
    if (type(value) is not dict or not required <= value.keys()
            or not value.keys() <= required | optional
            or type(value['timestamps_ms']) is not list or len(value['timestamps_ms']) > MAX_FRAMES):
        _fail('explicit_video_selection_required',422)
    return VideoSelection(tuple(value['timestamps_ms']),value['frame_count'],value['max_width'],
        value['max_height'],value.get('stream_index',0),value.get('output_format','png'))


@dataclass(frozen=True)
class _VideoMaterialPlan:
    source: DocumentSource = field(repr=False)
    delivery: str
    selection: VideoSelection | None = field(repr=False)
    plan: object = field(repr=False)
    fingerprint: str
    expires: float
    # The adapter owns the exact full host plan; no browser result/registry token.
    registry_token: None = None


@dataclass(frozen=True)
class _VideoMaterial:
    source: DocumentSource = field(repr=False)
    descriptor: object
    delivery: str


class HostVideoMaterials:
    """Thin runtime delegate; no state outside the real authenticated host scope.

    runtime supplies its existing _lock, _scope, _read, _capacity,
    _invalidate_requests and clock. Constructing this adapter performs no probe,
    file discovery, network, media decode, registry mutation or credential access.
    All operations run under runtime._lock, including the full approved conversion;
    thus pending original/frame reservations cannot race other material requests.
    """
    def __init__(self, runtime):
        if (not all(callable(getattr(runtime,name,None)) for name in
                    ('_scope','_read','_capacity','_invalidate_requests','clock'))
                or not hasattr(runtime,'_lock')):
            raise TypeError('existing authenticated host runtime required')
        self.runtime=runtime

    def _source(self, scope, fid, expected_sha):
        if type(scope.binding) is not HostChatScope:
            _fail('model_host_chat_scope_required')
        source=self.runtime._read(scope,fid,expected_sha)
        if type(source) is not DocumentSource:
            _fail('model_material_source_changed')
        return source

    def _recheck(self, scope, source):
        if self._source(scope,source.fid,source.sha256) != source:
            _fail('model_material_source_changed')

    @staticmethod
    def _is_video(source):
        return source.mime in _VIDEO_MIMES or source.filename.rsplit('.',1)[-1].lower() in ('mp4','webm')

    @staticmethod
    def _probe_metadata(inspection):
        metadata=inspection.video_metadata
        if (inspection.kind not in ('mp4','webm') or inspection.structural_validation != _PROBED
                or inspection.ready_for_extraction is not True or inspection.delivery != 'native'
                or type(metadata) is not dict or metadata.get('structural_validation') != _PROBED
                or type(metadata.get('fingerprint')) is not str
                or not _SHA.fullmatch(metadata['fingerprint'])):
            _fail(inspection.diagnostic or 'model_video_real_probe_required',422)
        return metadata

    @staticmethod
    def _decoder_diagnostic(metadata, stream_index=None):
        """Provider declarations, never assume the minimal build decodes AV1.

        Provider may publish declared_supported_codecs at top level or under its
        ffmpeg binary record. Only approved reviewed metadata is admitted. Missing
        declarations fail closed; no system decoder, codec fallback or transcode.
        This performs metadata checks only, not a native capability test.
        """
        try:
            from app.model_media_dependencies import get_media_dependency_status
            status=get_media_dependency_status(verify_binary_hashes=False)
            binaries=status.get('binaries',{})
            ffmpeg=binaries.get('ffmpeg',{})
            ffprobe=binaries.get('ffprobe',{})
            if (ffmpeg.get('redistribution_status') != 'approved'
                    or ffprobe.get('redistribution_status') != 'approved'):
                return 'reviewed_provider_unavailable_or_redistribution_review_required'
            declared=ffmpeg.get('declared_supported_codecs',status.get('declared_supported_codecs'))
            if (type(declared) not in (tuple,list) or not declared
                    or any(type(codec) is not str or codec not in _CODECS for codec in declared)
                    or len(set(declared)) != len(declared)):
                return 'video_decoder_codec_metadata_unavailable'
            streams=metadata.get('video_streams',())
            selected=[stream for stream in streams if stream_index is None or stream.get('index') == stream_index]
            if not selected:
                return 'selected_video_stream_missing'
            if any(stream.get('codec') not in declared for stream in selected):
                return 'video_decoder_codec_not_declared_supported'
            diagnostic=video_dependency_diagnostic()
            if diagnostic.get('available') is not True:
                return diagnostic.get('diagnostic') or 'reviewed_media_dependency_unavailable'
            return None
        except Exception:
            return 'video_decoder_codec_metadata_unavailable'

    @staticmethod
    def _budget(scope, source, *, skip_token=None, replace_fid=None):
        # These are the real scope originals, including documents/images/native
        # videos and inspected-but-not-yet-approved originals. No client count.
        originals=set(scope.sources)
        originals.update(record.source.fid for record in scope.plans.values())
        originals.update(record.source.fid for record in scope.materials.values())
        originals.add(source.fid)
        if len(originals)>MAX_ORIGINALS:
            _fail('model_attachment_count_limit',422)
        frames=0
        for material_id in scope.materials:
            parts=scope.registry.preview_parts(scope.binding,material_id)
            frames+=sum(part.delivery == 'video_frame' for part in parts)
        for token,record in scope.plans.items():
            if token == skip_token or record.source.fid == replace_fid:
                continue
            if type(record) is _VideoMaterialPlan and record.delivery == 'video_frame':
                frames+=record.plan.frame_count
        if frames>MAX_FRAMES:
            _fail('request_video_frame_limit',422)
        return len(originals),frames

    def inspect_if_video(self, principal, scope_id, fid, expected_sha):
        """Return None for ordinary materials; only real ffprobe can make ready."""
        runtime=self.runtime
        with runtime._lock:
            scope=runtime._scope(principal,scope_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            source=self._source(scope,fid,expected_sha)
            if not self._is_video(source):
                return None
            held=scope.sources.get(fid)
            if held is not None and expected_sha != held['sha256']:
                _fail('model_material_expected_sha_required_after_first_inspect',422)
            if held is not None and source.metadata() != held:
                _fail('model_material_source_changed')
            self._budget(scope,source)
            inspection=inspect_attachment_bytes(source.filename,source.mime,source.content)
            self._recheck(scope,source)
            actual=inspection.to_dict()
            scope.sources[fid]=source.metadata()
            probe_ok=(inspection.structural_validation == _PROBED
                      and inspection.ready_for_extraction is True and type(inspection.video_metadata) is dict)
            code=inspection.diagnostic or 'model_video_real_probe_required'
            frame_code=self._decoder_diagnostic(inspection.video_metadata) if probe_ok else code
            return {'scope_id':scope_id,'source':source.metadata(),'inspection':actual,
                'inspection_fingerprint':_digest({'source':source.metadata(),'inspection':actual}),
                'deliveries':[{'delivery':'native','available':probe_ok,
                    'code':None if probe_ok else code,'requires_route_native_video_support':True},
                    {'delivery':'video_frame','available':probe_ok and frame_code is None,
                     'code':frame_code,'explicit_video_selection_required':True}],
                'model_sent':False,'native_provider_support_verified':False,
                'automatic_fallback':False}

    def preview_if_video(self, principal, scope_id, fid, expected_sha, delivery,
                         selection=None, *, video_selection=None):
        """Pure conversion plan preview (real probe only); no PNG is produced.

        selection is the existing document DTO, required empty for a video. Native
        accepts no video selection. Frame timestamps/scale must be explicit; no
        uniform sampling defaults, whole-video implicit frames or browser budgets.
        """
        runtime=self.runtime
        with runtime._lock:
            scope=runtime._scope(principal,scope_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            source=self._source(scope,fid,expected_sha)
            if not self._is_video(source):
                if video_selection is not None:
                    _fail('video_selection_requires_video_original',422)
                return None
            if scope.sources.get(fid) != source.metadata():
                _fail('model_material_inspect_identity_required_or_changed',422)
            if any(material.source.fid == fid for material in scope.materials.values()):
                _fail('model_duplicate_original_material')
            # Existing SelectionInput defaults can be passed by the generic host
            # endpoint; any nondefault document selection on a video is rejected.
            document_defaults={'paragraphs':[],'sheets':[],'slides':[],'include_hidden':False,
                               'max_characters':100000,'pages':[]}
            if (selection is not None and (type(selection) is not dict
                    or any(key not in document_defaults or type(value) is not type(document_defaults[key])
                           or value != document_defaults[key] for key,value in selection.items()))):
                _fail('document_selection_not_applicable_to_video',422)
            if delivery not in ('native','video_frame'):
                _fail('model_video_explicit_delivery_required',422)
            originals,other_frames=self._budget(scope,source,replace_fid=fid)
            replaced=[token for token,record in scope.plans.items()
                      if type(record) is _VideoMaterialPlan and record.source.fid == fid]
            if len(scope.plans)-len(replaced)>=16 or len(scope.materials)>=MAX_ORIGINALS:
                _fail('model_material_or_conversion_plan_capacity',429)
            runtime._capacity(2*len(source.content)+16384)
            inspection=inspect_attachment_bytes(source.filename,source.mime,source.content)
            probe=self._probe_metadata(inspection)
            if delivery == 'video_frame':
                selected=video_selection_from_json(video_selection)
                code=self._decoder_diagnostic(probe,selected.stream_index)
                if code is not None:
                    _fail(code,422)
                if other_frames+selected.frame_count>MAX_FRAMES:
                    _fail('request_video_frame_limit',422)
                plan=preview_video_conversion(source.filename,source.mime,source.content,selected,
                    original_id=fid,original_version=source.original_version,
                    request_original_count=originals,other_video_frames=other_frames)
                metadata=plan.metadata()
                notes=plan.loss_notes
                converter=plan.converter
            else:
                if video_selection is not None:
                    _fail('model_native_delivery_does_not_accept_conversion_selection',422)
                selected=None
                notes=inspection.loss_notes
                converter='none'
                plan={'original_id':source.fid,'original_version':source.original_version,
                    'original_sha256':source.sha256,'byte_length':len(source.content),
                    'inspection':probe,'request_original_count':originals,
                    'request_total_video_frames':other_frames,'delivery':'native',
                    'loss_notes':list(notes),'converter':'none','approval_required':True,
                    'sending_approved':False,'native_provider_support_verified':False}
                metadata=plan
            self._recheck(scope,source)
            fingerprint=_digest({'scope':scope.binding.wire(),'source':source.metadata(),
                'delivery':delivery,'complete_plan':metadata})
            token=secrets.token_urlsafe(32)
            while token in scope.plans:token=secrets.token_urlsafe(32)
            # Only after the new plan is fully built do we replace a same-original
            # pending plan; prior approval tokens can never authorize the new plan.
            runtime._invalidate_requests(scope)
            for old in replaced:scope.plans.pop(old)
            scope.plans[token]=_VideoMaterialPlan(source,delivery,selected,plan,fingerprint,runtime.clock()+300)
            return {'scope_id':scope_id,'token':token,'fingerprint':fingerprint,'expires_in':300,
                'source':source.metadata(),'delivery':delivery,
                'selection':asdict(selected) if selected is not None else None,
                'video_selection':asdict(selected) if selected is not None else None,
                'converter':converter,'plan':metadata,'loss_notes':list(notes),
                'conversion_performed':False,'approval_required':True,'model_sent':False,
                'sending_approved':False,'sending_requires_separate_approval':True,
                'automatic_fallback':False}

    def approve_if_video(self, principal, scope_id, token, fingerprint, user_confirmed):
        """Consume held host plan, run real conversion, store actual registry parts."""
        runtime=self.runtime
        with runtime._lock:
            scope=runtime._scope(principal,scope_id)
            if scope.handed_off:_fail('model_handoff_scope_frozen')
            record=scope.plans.get(token)
            if type(record) is not _VideoMaterialPlan:
                return None
            # Consume even malformed/expired approval, just like the host path.
            scope.plans.pop(token)
            if (runtime.clock()>=record.expires or type(fingerprint) is not str
                    or fingerprint != record.fingerprint or user_confirmed is not True):
                _fail('model_conversion_confirmation_invalid_or_consumed')
            source=self._source(scope,record.source.fid,record.source.sha256)
            if source != record.source or scope.sources.get(source.fid) != source.metadata():
                _fail('model_material_source_changed')
            if any(material.source.fid == source.fid for material in scope.materials.values()):
                _fail('model_duplicate_original_material')
            if len(scope.materials)>=MAX_ORIGINALS:
                _fail('model_attachment_count_limit',422)
            originals,other_frames=self._budget(scope,source)
            metadata=record.plan.metadata() if record.delivery == 'video_frame' else record.plan
            if _digest({'scope':scope.binding.wire(),'source':source.metadata(),
                        'delivery':record.delivery,'complete_plan':metadata}) != record.fingerprint:
                _fail('video_held_approval_plan_changed')
            runtime._invalidate_requests(scope)
            if record.delivery == 'video_frame':
                plan=record.plan
                if (type(plan) is not VideoConversionPlan
                        or originals != plan.request_original_count
                        or other_frames+plan.frame_count != plan.request_total_video_frames):
                    _fail('video_request_budget_changed_repreview_required')
                # Fresh real inspection/probe and current provider declarations must
                # still admit the chosen codec. It never substitutes another codec.
                inspection=inspect_attachment_bytes(source.filename,source.mime,source.content)
                probe=self._probe_metadata(inspection)
                code=self._decoder_diagnostic(probe,plan.selection.stream_index)
                if code is not None:_fail(code,422)
                runtime._capacity(len(source.content)+MAX_RESULT_BYTES+plan.frame_count*4096)
                descriptor=scope.registry.register_video_frames(scope.binding,source,plan,
                    expected_source_session_id=scope.binding.session_id,
                    request_original_count=originals,other_video_frames=other_frames,
                    user_confirmed_conversion=True)
            else:
                if (originals != metadata['request_original_count']
                        or other_frames != metadata['request_total_video_frames']):
                    _fail('video_request_budget_changed_repreview_required')
                runtime._capacity(2*len(source.content)+8192)
                descriptor=scope.registry.register_native_video(scope.binding,source,
                    expected_source_session_id=scope.binding.session_id,
                    expected_inspection_fingerprint=metadata['inspection']['fingerprint'],
                    user_confirmed_material=True)
            try:
                self._recheck(scope,source)
                if runtime.clock()>=record.expires:
                    _fail('model_conversion_confirmation_expired_during_operation')
                runtime._capacity()
                parts=scope.registry.preview_parts(scope.binding,descriptor.input_id)
                # Reserve+commit under the same real host lock. Validate aggregate
                # produced frame count again, never a UI-provided frame_count.
                current_originals,current_frames=self._budget(scope,source)
                if current_originals>MAX_ORIGINALS or current_frames+sum(
                        part.delivery == 'video_frame' for part in parts)>MAX_FRAMES:
                    _fail('request_video_frame_limit',422)
                value={'scope_id':scope_id,'material_id':descriptor.input_id,
                    'fingerprint':descriptor.fingerprint,'source':source.metadata(),
                    'delivery':record.delivery,
                    'selection':asdict(record.selection) if record.selection is not None else None,
                    'video_selection':asdict(record.selection) if record.selection is not None else None,
                    'parts':[part.manifest() for part in parts],'processed_text_parts':[],
                    'frames':[{'frame':int(dict(part.locator)['frame']),
                        'timestamp_ms':int(dict(part.locator)['timestamp_ms']),
                        'sha256':part.sha256,'mime':part.mime,'bytes':len(part.content)}
                        for part in parts if part.delivery == 'video_frame'],
                    'content_bytes':descriptor.content_bytes,'characters':0,
                    'frame_count':sum(part.delivery == 'video_frame' for part in parts),
                    'loss_notes':list(descriptor.loss_notes),'model_sent':False,
                    'sending_approved':False,'sending_requires_separate_approval':True,
                    'conversion_performed':record.delivery == 'video_frame',
                    'native_provider_support_verified':False,'automatic_fallback':False}
                if len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))>1024**2:
                    _fail('model_video_material_preview_response_limit_no_truncation',413)
                scope.materials[descriptor.input_id]=_VideoMaterial(source,descriptor,record.delivery)
                return value
            except Exception:
                scope.registry.invalidate(scope.binding,descriptor.input_id)
                scope.materials.pop(descriptor.input_id,None)
                raise

    def frame_response(self, principal, scope_id, material_id, fingerprint, frame, expected_sha):
        """Authenticated endpoint callback for exact scope-held safe PNG bytes.

        Mount with the same principal_provider(request) as all model-host methods.
        Never accept a path/URL, decode a browser PNG, return native video/audio,
        or send bytes to an external image service. No thumbnail re-encoding occurs;
        these are the core's bounded <=2048-pixel RGB PNGs, with original provenance.
        """
        from fastapi.responses import Response
        runtime=self.runtime
        with runtime._lock:
            scope=runtime._scope(principal,scope_id)
            material=scope.materials.get(material_id)
            if (type(material) is not _VideoMaterial or material.delivery != 'video_frame'
                    or type(frame) is not int or not 1<=frame<=MAX_FRAMES
                    or type(fingerprint) is not str or fingerprint != material.descriptor.fingerprint
                    or type(expected_sha) is not str or not _SHA.fullmatch(expected_sha)):
                _fail('model_video_frame_unknown_or_changed',404)
            scope.registry.preview_descriptor(scope.binding,material_id)
            parts=scope.registry.preview_parts(scope.binding,material_id)
            chosen=next((part for part in parts if part.delivery == 'video_frame'
                         and dict(part.locator).get('frame') == str(frame)),None)
            if (chosen is None or chosen.mime != 'image/png' or chosen.kind != 'image'
                    or chosen.original_kind != 'video' or chosen.sha256 != expected_sha
                    or hashlib.sha256(chosen.content).hexdigest() != expected_sha):
                _fail('model_video_frame_unknown_or_changed',404)
            self._recheck(scope,material.source)
            return Response(content=chosen.content,media_type='image/png',headers={
                'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                'Content-Security-Policy':"default-src 'none'; sandbox",
                'Cross-Origin-Resource-Policy':'same-origin',
                'Content-Disposition':'inline; filename="video-frame.png"',
                'X-Video-Frame-SHA256':chosen.sha256})
