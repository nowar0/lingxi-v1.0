"""Session-owned attachment conversion, separate from any model-send authorization.

Import/construction performs no I/O. Default reads are lazy and limited to the
existing media ownership API; browser input never selects a filesystem path.
OOXML/text parsing is bounded by the inspection/extraction cores. PDF uses a
separate fixed ordinary host document-safety Windows Job worker, never the
plugin sandbox/AppContainer. Conversion and model sending remain separate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.model_attachment_inspection import InspectionError
from app.model_document_extract import (DocumentSelection, SheetSelection, ExtractionError,
    preview_document_conversion, extract_document)

MAX_SOURCE_BYTES = 15 * 1024**2
MAX_RESPONSE_BYTES = 1024**2
MAX_PENDING = 128
_SHA = re.compile(r'^[0-9a-f]{64}$')
_FID = re.compile(r'^[0-9a-f]{32}$')


class DocumentApiError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > MAX_RESPONSE_BYTES:
        raise DocumentApiError('conversion_response_limit_no_truncation', 413)
    return value


@dataclass(frozen=True)
class DocumentSource:
    """Trusted reader receipt, never constructed from an HTTP document body.

    Immutable media IDs have object version 1, NOT attachment-family history.
    identity_fingerprint binds the reader's actual object/metadata observation.
    """
    fid: str
    session_id: int
    filename: str
    mime: str
    content: bytes = field(repr=False)
    sha256: str
    identity_fingerprint: str
    original_version: int = 1

    def __post_init__(self):
        if (type(self.fid) is not str or not _FID.fullmatch(self.fid)
                or type(self.session_id) is not int or self.session_id <= 0
                or type(self.filename) is not str or not 1 <= len(self.filename) <= 200
                or self.filename in ('.', '..') or any(c in self.filename for c in '/\\:')
                or any(ord(c) < 32 for c in self.filename)
                or type(self.mime) is not str or not 1 <= len(self.mime) <= 200
                or type(self.content) is not bytes or not 0 < len(self.content) <= MAX_SOURCE_BYTES
                or type(self.sha256) is not str or not _SHA.fullmatch(self.sha256)
                or hashlib.sha256(self.content).hexdigest() != self.sha256
                or type(self.identity_fingerprint) is not str or not _SHA.fullmatch(self.identity_fingerprint)
                or type(self.original_version) is not int or self.original_version != 1):
            raise DocumentApiError('invalid_trusted_document_source', 422)

    def metadata(self):
        return {'fid':self.fid, 'session_id':self.session_id, 'filename':self.filename,
                'mime':self.mime, 'size':len(self.content), 'sha256':self.sha256,
                'original_version':1, 'identity_fingerprint':self.identity_fingerprint,
                'identity_semantics':'immutable_media_object_not_version_history'}


def read_media_source(fid, session_id):
    """Read one owned immutable media object, without looking up arbitrary paths."""
    from app import media
    row, path = media.read(fid, session_id)
    required = {'id','session_id','mime','size','sha256','name'}
    if type(row) is not dict or set(row) != required or row['id'] != fid or row['session_id'] != session_id:
        raise DocumentApiError('source_owner_or_metadata_changed')
    if type(row['size']) is not int or not 0 < row['size'] <= MAX_SOURCE_BYTES:
        raise DocumentApiError('source_size_limit', 413)
    path = Path(path)
    if (not path.is_absolute() or str(path).startswith(('\\\\','//')) or '..' in path.parts
            or any(':' in part for part in path.parts[1:])):
        raise DocumentApiError('source_local_object_required')
    for current in (path, *path.parents):
        if current.is_symlink() or current.is_junction() or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise DocumentApiError('source_link_refused')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise DocumentApiError('source_file_refused')
    identity = lambda info: [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        content = stream.read(MAX_SOURCE_BYTES+1)
        after = os.fstat(stream.fileno())
    if identity(before) != identity(opened) or identity(before) != identity(after) or identity(before) != identity(path.stat()):
        raise DocumentApiError('source_changed_during_read')
    second, second_path = media.read(fid, session_id)
    if second != row or Path(second_path) != path or len(content) != row['size']:
        raise DocumentApiError('source_owner_or_metadata_changed')
    return DocumentSource(fid, session_id, row['name'], row['mime'], content, row['sha256'],
                          _digest({'metadata':row,'identity':identity(before)}))


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SheetInput(Strict):
    name: str = Field(min_length=1, max_length=255)
    cell_range: str | None = Field(default=None, max_length=32)


class SelectionInput(Strict):
    paragraphs: list[int] = Field(default_factory=list, max_length=1000)
    sheets: list[SheetInput] = Field(default_factory=list, max_length=100)
    slides: list[int] = Field(default_factory=list, max_length=200)
    include_hidden: bool = False
    max_characters: int = Field(default=100000, ge=1, le=100000)
    pages: list[int] = Field(default_factory=list, max_length=200)

    def typed(self):
        return DocumentSelection(tuple(self.paragraphs), tuple(SheetSelection(s.name,s.cell_range) for s in self.sheets),
                                 tuple(self.slides), self.include_hidden, self.max_characters, tuple(self.pages))


class PreviewInput(Strict):
    fid: str = Field(pattern=r'^[0-9a-f]{32}$')
    session_id: int = Field(gt=0)
    expected_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    selection: SelectionInput = Field(default_factory=SelectionInput)


class Confirmation(Strict):
    token: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    session_id: int = Field(gt=0)
    user_confirmed: bool


class Cancellation(Strict):
    token: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    session_id: int = Field(gt=0)


@dataclass(frozen=True)
class _Pending:
    fingerprint: str
    source: dict = field(repr=False)
    selection: DocumentSelection = field(repr=False)
    plan: object = field(repr=False)
    expires: float


class HostDocumentConversions:
    def __init__(self, reader:Callable|None=None, *, clock=time.monotonic, ttl=300, max_pending=MAX_PENDING):
        if reader is not None and not callable(reader):
            raise TypeError('trusted reader required')
        if (not callable(clock) or type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0 < ttl <= 300
                or type(max_pending) is not int or not 1 <= max_pending <= MAX_PENDING):
            raise ValueError('bounded conversion authorization required')
        self.reader, self.clock, self.ttl, self.maximum = reader, clock, ttl, max_pending
        self._pending = {}
        self._lock = threading.RLock()
        self._active = 0
        # Conservative per-session lifetime ceilings, including failed/consumed
        # approvals. No eviction permits bypass by cycling original IDs/tokens.
        self._originals = {}
        self._delivered_characters = {}

    def _read(self, fid, session_id):
        receipt = (self.reader or read_media_source)(fid,session_id)
        if type(receipt) is not DocumentSource or receipt.fid != fid or receipt.session_id != session_id:
            raise DocumentApiError('source_owner_mismatch',403)
        return receipt

    def _purge(self):
        now=self.clock()
        self._pending={key:value for key,value in self._pending.items() if value.expires>now}

    def preview(self, fid, session_id, expected_sha256, selection=None):
        body=PreviewInput.model_validate({'fid':fid,'session_id':session_id,'expected_sha256':expected_sha256,
                                         'selection':{} if selection is None else selection})
        selected=body.selection.typed()
        with self._lock:
            self._purge()
            if len(self._pending)+self._active >= self.maximum:
                raise DocumentApiError('conversion_approval_capacity',429)
            originals = self._originals.get(session_id, set())
            if fid not in originals and len(originals) >= 8:
                raise DocumentApiError('document_original_count_limit', 413)
            if session_id not in self._originals and len(self._originals) >= MAX_PENDING:
                raise DocumentApiError('document_session_capacity', 429)
            source=self._read(fid,session_id)
            if source.sha256 != expected_sha256:
                raise DocumentApiError('source_sha256_changed')
            plan=preview_document_conversion(source.filename,source.mime,source.content,selected,
                                             original_id=fid,original_version=source.original_version)
            snapshot={'source':source.metadata(),'conversion_plan_fingerprint':plan.fingerprint,
                      'inspection_fingerprint':plan.inspection_fingerprint,'selection':asdict(selected),
                      'converter':plan.converter,'loss_notes':list(plan.loss_notes)}
            digest=_digest(snapshot);token=secrets.token_urlsafe(32)
            result=_bounded({'token':token,'fingerprint':digest,'expires_in':self.ttl,'snapshot':snapshot,
                'approval_required':True,'conversion_performed':False,'model_sent':False,
                'sending_requires_separate_approval':True,'read_only':True,
                'limits':{'source_bytes':MAX_SOURCE_BYTES,'characters':100000,'paragraph_selection_count':1000,
                          'page_selection_count':200,'originals':8,
                          'total_characters':100000,'response_bytes':MAX_RESPONSE_BYTES}})
            self._originals.setdefault(session_id, set()).add(fid)
            self._pending[token]=_Pending(digest,source.metadata(),selected,plan,self.clock()+self.ttl)
            return result

    def _take(self, token, fingerprint, session_id, *, activate=False):
        with self._lock:
            record=self._pending.pop(token,None)
            if (record is None or type(session_id) is not int or session_id != record.source['session_id']
                    or fingerprint != record.fingerprint or self.clock() >= record.expires):
                raise DocumentApiError('conversion_approval_invalid_or_consumed')
            if activate:self._active+=1
            return record

    def run(self, token, fingerprint, session_id, user_confirmed):
        if user_confirmed is not True:
            self._take(token,fingerprint,session_id)
            raise DocumentApiError('conversion_confirmation_required')
        record=self._take(token,fingerprint,session_id,activate=True)
        try:
            source=self._read(record.source['fid'],session_id)
            if source.metadata() != record.source:
                raise DocumentApiError('source_changed_after_preview')
            result=extract_document(source.filename,source.mime,source.content,record.selection,
                expected_raw_sha=source.sha256, expected_inspection_fingerprint=record.plan.inspection_fingerprint,
                expected_plan_fingerprint=record.plan.fingerprint, user_confirmed_conversion=True,
                original_id=source.fid, original_version=source.original_version)
            if self._read(source.fid,session_id).metadata() != record.source:
                raise DocumentApiError('source_changed_during_conversion')
            parts=[]
            for part in result.parts:
                content=part.content.decode('utf-8')
                parts.append({'text':content,'sha256':part.sha256,'bytes':len(part.content),
                    'locator':dict(part.locator),'original_id':part.original_id,
                    'original_version':part.original_version,'original_sha256':part.original_sha256,
                    'conversion_settings_sha256':part.conversion_settings_sha256,'converter':part.converter,
                    'formula':part.formula,'cached_value':part.cached_value,'value_type':part.value_type})
            response = _bounded({'source':record.source,'conversion_plan_fingerprint':result.conversion_plan_fingerprint,
                'inspection_fingerprint':result.inspection_fingerprint,
                'conversion_settings_sha256':result.conversion_settings_sha256,'converter':record.plan.converter,
                'parts':parts,'characters':result.characters,'skipped_hidden':result.skipped_hidden,
                'loss_notes':list(result.loss_notes),'conversion_performed':True,'model_sent':False,
                'sending_approved':False,'sending_requires_separate_approval':True,
                'no_model_or_network_call':True})
            with self._lock:
                total = self._delivered_characters.get(session_id, 0)+result.characters
                if total > 100000:
                    raise DocumentApiError('document_total_character_limit_no_truncation', 413)
                self._delivered_characters[session_id] = total
            return response
        finally:
            with self._lock:self._active-=1

    def cancel(self, token, fingerprint, session_id):
        self._take(token,fingerprint,session_id)
        return {'cancelled':True,'conversion_performed':False,'model_sent':False,
                'files_deleted':False,'sending_requires_separate_approval':True}


def create_router(service=None):
    # Construction is inert; even the default media import happens only on a request.
    conversions=service if service is not None else HostDocumentConversions()
    if type(conversions) is not HostDocumentConversions:
        raise TypeError('host conversion service required')
    router=APIRouter(prefix='/v11/model-documents',tags=['model-documents'])
    def invoke(method,*args):
        try:
            return getattr(conversions,method)(*args)
        except DocumentApiError as error:
            raise HTTPException(error.status,{'code':error.code,'message':'文档转换未完成或批准已失效；未发送模型，不自动重试'}) from None
        except (ExtractionError,InspectionError) as error:
            raise HTTPException(422,{'code':error.code,'message':'文档类型、结构、选区或限额不满足要求；未交付部分结果、截断或发送模型，不自动重试'}) from None
        except HTTPException:
            raise
        except (ValueError,OSError,TypeError,UnicodeError):
            raise HTTPException(422,{'code':'document_conversion_invalid','message':'文档转换请求或原件不可用；未发送模型，不自动重试'}) from None
        except Exception:
            # Parser/reader implementation failures must not leak document text in
            # tracebacks or cause the already-consumed conversion to be retried.
            raise HTTPException(503,{'code':'document_conversion_unconfirmed','message':'文档转换结果未确认；未发送模型，旧批准不能重试'}) from None
    @router.post('/preview')
    def preview(body:PreviewInput):
        return JSONResponse(invoke('preview',body.fid,body.session_id,body.expected_sha256,body.selection.model_dump()),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @router.post('/run')
    def run(body:Confirmation):
        return JSONResponse(invoke('run',body.token,body.fingerprint,body.session_id,body.user_confirmed),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @router.post('/cancel')
    def cancel(body:Cancellation):
        return JSONResponse(invoke('cancel',body.token,body.fingerprint,body.session_id),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    return router
