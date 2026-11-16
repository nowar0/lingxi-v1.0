"""Immutable attachment objects with verified deduplication and session aliases."""
import hashlib
import os
import re
from fastapi import HTTPException
from app import config

OOXML_TYPES={
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'.docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'.xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation':'.pptx',
}
VIDEO_TYPES={'video/mp4':'.mp4','video/webm':'.webm'}
MODEL_INPUT_TYPES={**OOXML_TYPES,**VIDEO_TYPES}
DOCUMENT_TYPES={'text/plain':'.txt','text/markdown':'.md','text/csv':'.csv','application/json':'.json',
                'text/html':'.html','application/pdf':'.pdf','application/zip':'.zip','application/octet-stream':'.bin',
                **OOXML_TYPES}


def verify_model_input(data,mime,name=None):
    """Preflight new originals only; this never grants extraction or provider use.

    Pure internal MIME checks may use a host-generated canonical name. Storage must
    pass the actual explicit upload name, so an unnamed or mismatched original is
    never silently relabelled. Inspection is not an extraction/send approval.
    A missing reviewed video tool may retain an original with a diagnostic, but a
    malformed/unsupported container or a failed probe must never become safe merely
    because the inspection report is not ready for extraction.
    """
    if mime not in MODEL_INPUT_TYPES:return None
    from app.model_attachment_inspection import InspectionError, inspect_attachment_bytes
    filename=('attachment'+MODEL_INPUT_TYPES[mime]) if name is None else name
    try:
        if mime in VIDEO_TYPES:
            # This pure byte/header gate must run before optional tool lookup and
            # cannot be bypassed by an unavailable-tool diagnostic (or fake probe).
            from app.model_video_extract import VideoExtractionError, _source
            try:
                _source(filename,mime,data)
            except VideoExtractionError as error:
                raise InspectionError(error.code) from None
        report=inspect_attachment_bytes(filename,mime,data)
        if mime in VIDEO_TYPES and not report.ready_for_extraction:
            # Missing deployment prerequisites are different from a bad container,
            # rejected stream, failed parser/worker, timeout or unknown exception.
            # Preserve the former as a reference-only original, never the latter.
            if report.diagnostic not in ('reviewed_media_dependency_unavailable',
                                         'bounded_video_worker_requires_windows'):
                raise InspectionError(report.diagnostic or 'video_inspection_failed')
        return report
    except InspectionError as error:
        raise HTTPException(422,{'code':'attachment_inspection_failed','reason':error.code,
            'message':'附件类型或安全结构校验未通过；未保存、提取或发送模型'}) from None


def model_input_status(mime):
    """Additive truthful metadata, not a decoder/parser capability attestation."""
    if mime not in MODEL_INPUT_TYPES:return {}
    return {'attachment_model_input':{'route':'reference_only','native_capability_verified':False,
            'conversion_available':False,'content_sent_to_model':False,
            'note':'仅保存已预检原件；提取/抽帧与模型输入需另行核验能力、预览并批准'}}


def verify_document(data,mime,name=None):
    if mime not in DOCUMENT_TYPES:return False
    if mime in OOXML_TYPES:
        verify_model_input(data,mime,name)
        return True
    if mime.startswith('text/') or mime=='application/json':
        try:text=data.decode('utf-8-sig')
        except UnicodeError:raise HTTPException(422,'文字附件须为UTF-8；请转换编码后上传') from None
        if '\0' in text:raise HTTPException(422,'文字附件含二进制内容，请按原件保留')
    elif mime=='application/pdf' and not data.startswith(b'%PDF-'):raise HTTPException(422,'内容不是受支持的PDF文件')
    elif mime=='application/zip' and data[:4] not in (b'PK\x03\x04',b'PK\x05\x06'):raise HTTPException(422,'内容不是普通ZIP文件，暂不支持ZIP64或加密归档')
    return True


def folder():
    path=config.DATA_DIR/'attachments'
    if path.is_symlink() or path.is_junction():raise HTTPException(409,'附件目录不能是链接')
    path.mkdir(parents=True,exist_ok=True)
    return path


def verified(path,digest,size):
    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size!=size:return False
    count=0;sha=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(128*1024):
            count+=len(chunk)
            if count>size:return False
            sha.update(chunk)
    return count==size and sha.hexdigest()==digest


def store(db,fid,extension,mime,data):
    if not re.fullmatch('[a-f0-9]{32}',fid):raise ValueError('无效附件编号')
    home=folder();path=home/(fid+extension);digest=hashlib.sha256(data).hexdigest()
    if path.exists() or path.is_symlink():raise HTTPException(409,'附件编号已存在，请重新上传')
    owned=False
    try:
        for row in db.execute('SELECT id FROM media_files WHERE sha256=? AND mime=? AND size=? LIMIT 100',(digest,mime,len(data))):
            if not re.fullmatch('[a-f0-9]{32}',row['id']):continue
            source=home/(row['id']+extension)
            try:
                if not verified(source,digest,len(data)):continue
                os.link(source,path,follow_symlinks=False);owned=True
                if verified(path,digest,len(data)):return path
                path.unlink();owned=False
            except FileExistsError:raise HTTPException(409,'附件编号已存在，请重新上传') from None
            except (OSError,NotImplementedError):
                if owned:path.unlink(missing_ok=True);owned=False
                break  # Filesystems without hard links use an immutable full copy.
        with path.open('xb') as stream:
            owned=True;stream.write(data)
        return path
    except BaseException:
        if owned:path.unlink(missing_ok=True)
        raise


def read_path(row,extension):
    path=folder()/(row['id']+extension)
    try:
        if not path.exists() and not path.is_symlink():raise HTTPException(404,'附件文件已丢失')
        if not verified(path,row['sha256'],row['size']):raise HTTPException(409,'附件原件已改变或丢失，请重新提供；未使用不一致的内容')
    except OSError:raise HTTPException(409,'附件暂不可读，请检查文件权限') from None
    return path
