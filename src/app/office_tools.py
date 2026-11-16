"""Approved workspace Office output and offline Windows OCR."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import advanced_approval, artifacts, catalog, config, media, workspace

_lock = Lock()
_EXT = {'word': '.docx', 'excel': '.xlsx', 'powerpoint': '.pptx'}
_COM = {'word': 'Word.Application', 'excel': 'Excel.Application', 'powerpoint': 'PowerPoint.Application'}
_ROOT = {'word': 'word/document.xml', 'excel': 'xl/workbook.xml', 'powerpoint': 'ppt/presentation.xml'}
_FUNCTIONS = {'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'COUNTA', 'ROUND', 'IF', 'AND', 'OR', 'ABS', 'IFERROR'}
_FORMULA = re.compile(r'=[0-9A-Z_.,()<>:=+*/^% \-]{1,380}', re.I)
_DANGEROUS_FORMULA = re.compile(r'\b(?:WEBSERVICE|RTD|DDE|DDEAUTO|CALL|EXEC|REGISTER\.ID|STOCKHISTORY|IMAGE)\s*\(', re.I)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Cell(Strict):
    address: str = Field(pattern=r'^[A-Z]{1,3}[1-9][0-9]{0,6}$')
    value: str | int | float | bool | None
    formula: bool = False


class Slide(Strict):
    title: str = Field(max_length=220)
    body: str = Field(max_length=8000)


class Fields(Strict):
    title: str = Field(default='', max_length=220)
    paragraphs: list[str] = Field(default_factory=list, max_length=150)
    find: str = Field(default='', max_length=200)
    replace: str = Field(default='', max_length=200)
    sheet: str = Field(default='', max_length=31)
    cells: list[Cell] = Field(default_factory=list, max_length=500)
    slides: list[Slide] = Field(default_factory=list, max_length=30)
    slide: int = Field(default=1, ge=1, le=1000)


class Operation(Strict):
    session_id: int = Field(gt=0)
    kind: Literal['word', 'excel', 'powerpoint', 'ocr']
    action: Literal['create', 'edit', 'render', 'recognize']
    source: str = Field(default='', max_length=2000)
    attachment_id: str = Field(default='', max_length=100)
    output: str = Field(default='', max_length=2000)
    fields: Fields = Field(default_factory=Fields)
    language: str = Field(default='zh-Hans-CN', pattern=r'^[A-Za-z0-9-]{2,35}$')
    pages: list[int] = Field(default_factory=lambda: [1], min_length=1, max_length=8)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_bounded(path):
    if not path.is_file() or path.stat().st_size > media.LIMIT:
        raise HTTPException(422, '文件不存在或超过 15 MiB')
    with path.open('rb') as stream:
        raw = stream.read(media.LIMIT + 1)
    if len(raw) > media.LIMIT:
        raise HTTPException(413, '文件超过 15 MiB')
    return raw


def check_office(data, kind):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            parts = bundle.infolist()
            names = {part.filename.casefold() for part in parts}
            if (len(parts) > 5000 or len(names) != len(parts) or _ROOT[kind].casefold() not in names
                    or sum(part.file_size for part in parts) > 80 * 1024 * 1024):
                raise ValueError('文件结构或解包大小超过 Office 处理边界')
            for part in parts:
                name = part.filename.casefold()
                if part.flag_bits & 1 or any(pattern in name for pattern in
                        ('vbaproject', '/activex/', '/embeddings/', '/externallinks/', 'connections.xml', '/querytables/')):
                    raise ValueError('文件包含加密、宏、外部数据或嵌入对象')
                if name.endswith('.rels') or name.startswith('xl/worksheets/') and name.endswith('.xml'):
                    if part.file_size > 12 * 1024 * 1024:
                        raise ValueError('Office XML 部件超过 12 MiB')
                    raw = bundle.read(part)
                    if re.search(rb'<!\s*(?:DOCTYPE|ENTITY)', raw, flags=re.I):
                        raise ValueError('Office 文件包含外部 XML 定义')
                    xml = ET.fromstring(raw)
                    for element in xml.iter():
                        if element.get('TargetMode', '').lower() == 'external':
                            raise ValueError('Office 工作副本含外部关系')
                        if element.tag.rsplit('}', 1)[-1] == 'f':
                            formula = element.text or ''
                            if _DANGEROUS_FORMULA.search(formula) or '|' in formula or re.search(r'\[[^\]]+\][^!]*!', formula):
                                raise ValueError('表格包含可能访问外部服务或工作簿的公式')
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as error:
        raise ValueError('Office 文件结构无效') from error


def check_fields(body):
    encoded = json.dumps(body.fields.model_dump(), ensure_ascii=False)
    if len(encoded) > 95000:
        raise HTTPException(422, '编辑数据超过 95000 字符')
    if body.kind == 'ocr':
        if body.action != 'recognize' or body.output or any(not 1 <= p <= 100000 for p in body.pages):
            raise HTTPException(422, 'OCR 仅支持图片或 PDF 指定页面')
    elif body.action == 'recognize' or body.action == 'render' and body.output:
        raise HTTPException(422, '请使用 Office 生成、编辑或 PDF 预览')
    if body.action == 'create' and (body.source or body.attachment_id):
        raise HTTPException(422, '新建文档不需要源文件')
    if body.action != 'create' and bool(body.source) == bool(body.attachment_id):
        raise HTTPException(422, '请选择一个项目文件或会话附件')
    if body.kind == 'word':
        if body.action == 'create' and not (body.fields.title or body.fields.paragraphs):
            raise HTTPException(422, '请填写 Word 标题或正文')
        if body.action == 'edit' and (not body.fields.find or '^' in body.fields.find or '^' in body.fields.replace):
            raise HTTPException(422, '请填写要查找和替换的普通文字')
    if body.kind == 'excel' and body.action in ('create', 'edit'):
        if not body.fields.cells or len({c.address for c in body.fields.cells}) != len(body.fields.cells):
            raise HTTPException(422, '请填写单元格，地址不能重复')
        for cell in body.fields.cells:
            if cell.formula and (not isinstance(cell.value, str) or not _FORMULA.fullmatch(cell.value)
                    or not set(re.findall(r'([A-Z_][A-Z_0-9.]*)\s*\(', cell.value.upper())) <= _FUNCTIONS):
                raise HTTPException(422, '仅接受工作表内引用和常用本地公式')
    if body.kind == 'powerpoint' and (
            body.action == 'create' and not body.fields.slides or body.action == 'edit' and not body.fields.title):
        raise HTTPException(422, '请填写幻灯片或要修改的标题')


def selection(body, bound):
    source = None
    if body.attachment_id:
        row, path = media.read(body.attachment_id, body.session_id)
        source = (row['name'], read_bounded(path))
    elif body.source:
        _, path = workspace.resolve(bound['id'], body.session_id, body.source)
        source = (path.name, read_bounded(path))
    if source:
        suffix = Path(source[0]).suffix.lower()
        if body.kind == 'ocr':
            if suffix not in ('.png', '.jpg', '.jpeg', '.bmp', '.pdf'):
                raise HTTPException(422, 'OCR 支持 PNG、JPEG、BMP 或 PDF')
        elif suffix != _EXT[body.kind]:
            raise HTTPException(422, '源文件格式与 Office 应用不一致')
        else:
            try:
                check_office(source[1], body.kind)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
    target = None
    if body.output:
        _, target = workspace.resolve(bound['id'], body.session_id, body.output)
        if target.suffix.lower() != _EXT[body.kind] or not target.parent.is_dir() or target.is_dir():
            raise HTTPException(422, '请在现有项目目录中选择正确扩展名的文件')
    return source, target


def _fingerprints(source, target):
    return {'source_sha256': sha(source[1]) if source else None,
            'output_sha256': sha(read_bounded(target)) if target and target.exists() else 'new'}


def staged_call(root, fields, ocr=False):
    payload = json.dumps(fields, ensure_ascii=False).encode('utf-8')
    if ocr:
        request = root/'request.json'
        request.write_bytes(payload)
        powershell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        args = [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
                str(config.BASE_DIR/'app/static/workers/windows_ocr.ps1'), str(request)]
    else:
        args = ([sys.executable, '--office-worker'] if getattr(sys, 'frozen', False) else
                [sys.executable, '-B', str(config.BASE_DIR/'launch.py'), '--office-worker'])
    try:
        p = subprocess.run(args, input=payload, cwd=root, capture_output=True,
            timeout=150 if ocr else 95, creationflags=subprocess.CREATE_NO_WINDOW)
        if p.returncode:
            raise ValueError(p.stderr.decode('utf-8', 'replace')[-350:] or '本地处理进程退出')
        result = json.loads(p.stdout)
        if not ocr:
            if not result.get('ok'):
                raise ValueError(result.get('error', 'Office 处理失败'))
            return result['result']
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(502, str(exc)[:600]) from exc


@catalog.router.get('/office/status')
async def status():
    if os.name != 'nt':
        return {'office': dict.fromkeys(_COM, False), 'ocr_languages': []}
    import winreg
    office = {}
    for kind, progid in _COM.items():
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid+'\\CLSID'):
                office[kind] = True
        except OSError:
            office[kind] = False
    try:
        with TemporaryDirectory(prefix='lingxi-ocr-') as path:
            languages = (await asyncio.to_thread(staged_call, Path(path), {'action': 'languages'}, True))['languages']
    except HTTPException:
        languages = []
    return {'office': office, 'ocr_languages': languages}


@catalog.router.post('/office/preview')
def preview(body: Operation):
    if os.name != 'nt':
        raise HTTPException(409, '本地 Office/OCR 需要 Windows')
    bound = advanced_approval.workspace(body.session_id)
    check_fields(body)
    source, target = selection(body, bound)
    fingerprints = _fingerprints(source, target)
    return advanced_approval.prepare('office', body.session_id, (bound['id'], bound['revision']),
        (body, fingerprints), {'operation': body.model_dump(), 'workspace': bound['path'],
                               'source_name': source[0] if source else None, **fingerprints})


def run(body, fingerprints, bound):
    from app.git_transition import assert_unclaimed
    with catalog.read_connection() as db:
        assert_unclaimed(db, body.session_id)
    source, target = selection(body, bound)
    if _fingerprints(source, target) != fingerprints:
        raise HTTPException(409, '文件已变化，请重新预览')
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, '另一个 Office/OCR 操作尚未结束')
    try:
        with TemporaryDirectory(prefix='lingxi-office-') as name:
            root = Path(name)
            source_path = root/('input'+Path(source[0]).suffix.lower()) if source else None
            if source_path:
                source_path.write_bytes(source[1])
            if body.kind == 'ocr':
                result = staged_call(root, {'path': str(source_path), 'language': body.language, 'pages': body.pages}, True)
                text = '\n\n'.join('第 '+str(page['page'])+' 页\n'+'\n'.join(line['text'] for line in page['lines'])
                                      for page in result['pages'])
                artifact = artifacts.store(text.encode('utf-8'), 'text/plain', body.session_id, 'OCR 识别结果.txt',
                    {'kind': 'ocr', 'source': source[0], 'source_sha256': fingerprints['source_sha256']})
                return {'artifact': artifact, 'text': text[:120000], 'pages': result['pages']}
            ext = '.pdf' if body.action == 'render' else _EXT[body.kind]
            output = root/('output'+ext)
            info = staged_call(root, {'stage': str(root), 'kind': body.kind, 'action': body.action,
                'source': source_path.name if source_path else '', 'output': output.name, 'fields': body.fields.model_dump()})
            data = read_bounded(output)
            mime = artifacts.file_mime(output, data)
            artifact_name = Path(body.output).name if body.output else ((Path(source[0]).stem if source else body.fields.title or 'Office 产物')+ext)
            artifact = artifacts.store(data, mime, body.session_id, artifact_name,
                {'kind': 'office', 'operation': body.action, 'source_sha256': fingerprints['source_sha256']})
            if target:
                current = advanced_approval.workspace(body.session_id)
                if current != bound:
                    return {'artifact': artifact, 'result': info, 'workspace_error': '工作目录已变化，成果已保留在会话产物'}
                _, path = workspace.resolve(bound['id'], body.session_id, body.output)
                with workspace._file_locks_guard:
                    lock = workspace._file_locks.setdefault(os.path.normcase(str(path)), __import__('threading').RLock())
                with lock:
                    if _fingerprints(None, path)['output_sha256'] != fingerprints['output_sha256']:
                        return {'artifact': artifact, 'result': info, 'workspace_error': '目标已变化，成果已保留在会话产物'}
                    temp = path.with_name('.lingxi-office-'+uuid4().hex+ext)
                    try:
                        temp.write_bytes(data)
                        if fingerprints['output_sha256'] == 'new':
                            os.link(temp, path)  # File creation never overwrites a competing writer.
                        else:
                            os.replace(temp, path)
                    finally:
                        temp.unlink(missing_ok=True)
            return {'artifact': artifact, 'result': info, 'workspace_path': body.output or None}
    finally:
        _lock.release()


@catalog.router.post('/office/commit')
async def commit(body: advanced_approval.Commit):
    bound = advanced_approval.workspace(body.session_id)
    action, fingerprints = advanced_approval.take('office', body, (bound['id'], bound['revision']))
    return await asyncio.to_thread(run, action, fingerprints, bound)


async def execute_tool(session_id, args):
    bound = advanced_approval.workspace(session_id)
    action = Operation(session_id=session_id, **args)
    check_fields(action)
    source, target = selection(action, bound)
    return await asyncio.to_thread(run, action, _fingerprints(source, target), bound)
