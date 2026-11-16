"""Bounded read-only text extraction after the existing OOXML inspection gate.

Bytes only: no Office, macros, formula evaluation, external-link execution,
rendering or network. PDF parsing runs only in a bounded ordinary host Job
worker (not the plugin sandbox), with text extraction after explicit page approval. Conversion approval is NOT approval to send to a model.
Synthetic inspection receipts and these Python dataclasses are not OS evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import io
import json
import math
import re
import zipfile

from app.model_attachment_inspection import (InspectionPolicy, inspect_attachment_bytes,
    _xml, _resolve_target, _rel_source)

VERSION = 'stdlib-ooxml-text-v1'
MAX_CHARS = 100000
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_SHA = re.compile(r'^[0-9a-f]{64}$')
_CELL = re.compile(r'^([A-Z]{1,3})([1-9][0-9]{0,6})$')


class ExtractionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _cell(value):
    match = _CELL.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ExtractionError('invalid_cell_reference')
    column = 0
    for char in match[1]:
        column = column*26+ord(char)-64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ExtractionError('invalid_cell_reference')
    return column, row


@dataclass(frozen=True)
class SheetSelection:
    name: str
    cell_range: str | None = None

    def __post_init__(self):
        if type(self.name) is not str or not 1 <= len(self.name) <= 255 or any(ord(c)<32 for c in self.name):
            raise ExtractionError('invalid_sheet_selection')
        if self.cell_range is not None:
            if type(self.cell_range) is not str or self.cell_range.count(':') > 1:
                raise ExtractionError('invalid_cell_range')
            bounds = self.cell_range.split(':')
            first, last = _cell(bounds[0]), _cell(bounds[-1])
            if first[0] > last[0] or first[1] > last[1] or (last[0]-first[0]+1)*(last[1]-first[1]+1) > 100000:
                raise ExtractionError('cell_range_limit_or_order')


@dataclass(frozen=True)
class DocumentSelection:
    # Inclusive, 1-based paragraph or slide indices. Empty means all eligible parts.
    paragraphs: tuple[int, ...] = ()
    sheets: tuple[SheetSelection, ...] = ()
    slides: tuple[int, ...] = ()
    include_hidden: bool = False
    max_characters: int = MAX_CHARS
    # PDF never interprets empty as all. Appended to preserve the existing
    # positional OOXML/text selection constructor contract.
    pages: tuple[int, ...] = ()

    def __post_init__(self):
        for values, maximum in ((self.paragraphs, 50000), (self.slides, 200)):
            if type(values) is not tuple or len(values) > maximum or len(set(values)) != len(values) or any(type(v) is not int or not 1 <= v <= maximum for v in values):
                raise ExtractionError('invalid_index_selection')
        if (type(self.pages) is not tuple or len(self.pages)>200 or len(set(self.pages)) != len(self.pages)
                or any(type(page) is not int or not 1 <= page <= 200 for page in self.pages)
                or self.pages != tuple(sorted(self.pages))):
            raise ExtractionError('pdf_explicit_ordered_pages_required')
        if type(self.sheets) is not tuple or len(self.sheets)>100 or any(type(s) is not SheetSelection for s in self.sheets) or len({s.name for s in self.sheets}) != len(self.sheets):
            raise ExtractionError('invalid_sheet_selection')
        if type(self.include_hidden) is not bool or type(self.max_characters) is not int or not 1 <= self.max_characters <= MAX_CHARS:
            raise ExtractionError('invalid_selection_budget')


@dataclass(frozen=True)
class ConversionPlan:
    original_sha256: str
    inspection_fingerprint: str
    selection_fingerprint: str
    fingerprint: str
    filename: str
    kind: str
    selection: DocumentSelection
    loss_notes: tuple[str, ...]
    original_id: str
    original_version: int
    converter: str = VERSION
    approval_required: bool = True
    sending_approved: bool = False


@dataclass(frozen=True)
class ProcessedPart:
    content: bytes = field(repr=False)
    sha256: str
    locator: tuple[tuple[str, str], ...]
    original_sha256: str
    original_version: int
    original_id: str
    conversion_settings_sha256: str
    converter: str = VERSION
    formula: str | None = field(default=None, repr=False)
    cached_value: str | None = field(default=None, repr=False)
    value_type: str = 'text'


@dataclass(frozen=True)
class ExtractionResult:
    parts: tuple[ProcessedPart, ...] = field(repr=False)
    original_sha256: str
    inspection_fingerprint: str
    conversion_plan_fingerprint: str
    conversion_settings_sha256: str
    characters: int
    skipped_hidden: int
    loss_notes: tuple[str, ...]
    sending_approved: bool = False
    requires_final_content_preview_and_send_approval: bool = True
    converter: str = VERSION


def _inspect(filename, mime, raw, selection):
    if type(selection) is not DocumentSelection:
        raise ExtractionError('typed_selection_required')
    inspection = inspect_attachment_bytes(filename, mime, raw)
    if inspection.kind not in ('txt', 'md', 'docx', 'xlsx', 'pptx', 'pdf') or not inspection.ready_for_extraction:
        raise ExtractionError(inspection.diagnostic or 'unsupported_extractor')
    kind = inspection.kind
    if ((kind != 'docx' and selection.paragraphs) or (kind != 'xlsx' and selection.sheets)
            or (kind != 'pptx' and selection.slides) or (kind != 'pdf' and selection.pages)
            or (kind in ('txt','md','docx','pdf') and selection.include_hidden)):
        raise ExtractionError('selection_kind_mismatch')
    if kind == 'pdf':
        if not selection.pages:
            raise ExtractionError('pdf_explicit_ordered_pages_required')
        if any(page > inspection.pages for page in selection.pages):
            raise ExtractionError('pdf_selected_page_missing')
    digest = _hash(raw)
    inspected = _digest({'filename':filename, 'mime':mime, 'raw_sha256':digest, 'inspection':inspection.to_dict()})
    return inspection, digest, inspected


def preview_document_conversion(filename, mime, raw, selection, *, original_id, original_version):
    if type(original_version) is not int or not 1 <= original_version <= 2**53-1 or type(original_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', original_id):
        raise ExtractionError('host_original_identity_required')
    inspection, digest, inspected = _inspect(filename, mime, raw, selection)
    notes = {
        'txt': ('纯UTF-8文本；不执行链接、HTML或脚本。',),
        'md': ('Markdown作为纯文本；不执行代码或获取链接。',),
        'docx': ('仅正文直接段落和表格文字；段落选区编号不包括表格内段落。复杂内容控件、文本框、页眉页脚、批注、图片、样式和分页布局未提取。',
                 '仅按已验证的docDefaults、默认/显式段落与字符样式及直接run隐藏属性跳过隐藏文字；未知隐藏样式语义拒绝提取。修订插入/删除内容默认跳过，不声称已处理修订语义。'),
        'xlsx': ('仅已选工作表现存单元格；未列出空白格，不计算公式，缓存可能缺失或过期；共享或数组公式尚未适配时明确拒绝。',
                 '默认不取隐藏工作表；不包含图像、图表视觉效果、数据连接和渲染样式。'),
        'pptx': ('按演示文稿关系顺序提取已选幻灯片文本与表格；不含备注、图片、图表、动画、音视频和布局。',
                 '隐藏幻灯片默认不取；没有OCR或完整视觉理解。'),
        'pdf': inspection.loss_notes,
    }[inspection.kind]
    converter = inspection.parser_version if inspection.kind == 'pdf' else VERSION
    settings = _digest({'converter':converter, 'selection':asdict(selection)})
    fingerprint = _digest({'original':digest, 'inspection':inspected, 'settings':settings,
                           'kind':inspection.kind, 'loss_notes':notes,
                           'original_id':original_id,'original_version':original_version})
    return ConversionPlan(digest, inspected, settings, fingerprint, filename, inspection.kind,
                          selection, notes, original_id, original_version, converter=converter)


def _parts(raw):
    # Inspection has already validated every central/local header, path, CRC,
    # relationship, XML size/node/depth, compression ratio and aggregate budget.
    # Re-reading is still bounded and uses the SAME XML gate, not a weaker parser.
    policy = InspectionPolicy()
    xml, aggregate, total = {}, [0], 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            if item.is_dir() or not item.filename.endswith(('.xml','.rels')):
                continue
            with archive.open(item) as stream:
                data = stream.read(policy.max_xml_bytes+1)
            total += len(data)
            if len(data)>policy.max_xml_bytes or total>policy.max_uncompressed_bytes:
                raise ExtractionError('extraction_xml_budget')
            xml[item.filename] = _xml(data, policy, aggregate)
    relations = {}
    for name, element in xml.items():
        if name.endswith('.rels'):
            source = _rel_source(name)
            relations[source] = {node.attrib['Id']: (node.attrib['Type'], _resolve_target(source, node.attrib['Target'])) for node in element}
    return xml, relations


def _word_on_off(value):
    if value in ('1', 'true', 'on'):
        return True
    if value in ('0', 'false', 'off'):
        return False
    raise ExtractionError('unsupported_word_boolean')


def _word_hidden_properties(properties):
    # Keep two independent tri-state properties: an absent flag inherits, and an
    # explicit false can clear that same flag without clearing the other one.
    values = {}
    if properties is None:
        return values
    for name in ('vanish', 'webHidden'):
        flags = properties.findall('{'+_W+'}'+name)
        if len(flags) > 1:
            raise ExtractionError('ambiguous_word_hidden_property')
        if flags:
            values[name] = _word_on_off(flags[0].attrib.get('{'+_W+'}val', 'true'))
    return values


def _word_style_gate(xml):
    styles, default_styles, document_defaults = {}, {}, {}
    containers = [element for element in xml.values() if element.tag == '{'+_W+'}styles']
    if len(containers) > 1:
        raise ExtractionError('ambiguous_word_style')
    for element in containers:
        defaults = element.findall('{'+_W+'}docDefaults')
        if len(defaults) > 1:
            raise ExtractionError('ambiguous_word_defaults')
        if defaults:
            run_defaults = defaults[0].findall('{'+_W+'}rPrDefault/{'+_W+'}rPr')
            if len(run_defaults) > 1:
                raise ExtractionError('ambiguous_word_defaults')
            document_defaults = _word_hidden_properties(run_defaults[0] if run_defaults else None)
            supported = {id(flag) for props in run_defaults for flag in props
                         if flag.tag in ('{'+_W+'}vanish', '{'+_W+'}webHidden')}
            if any(id(flag) not in supported for name in ('vanish', 'webHidden')
                   for flag in defaults[0].iter('{'+_W+'}'+name)):
                raise ExtractionError('unsupported_word_hidden_context')
        for style in element.findall('{'+_W+'}style'):
            ident = style.attrib.get('{'+_W+'}styleId')
            kind = style.attrib.get('{'+_W+'}type')
            if not ident or ident in styles:
                raise ExtractionError('ambiguous_word_style')
            if kind not in (None, 'paragraph', 'character', 'table', 'numbering'):
                raise ExtractionError('unsupported_word_style_type')
            default = style.attrib.get('{'+_W+'}default')
            if default is not None and _word_on_off(default):
                if kind not in ('paragraph', 'character') or kind in default_styles:
                    raise ExtractionError('ambiguous_word_default_style')
                default_styles[kind] = ident
            bases = style.findall('{'+_W+'}basedOn')
            properties = style.findall('{'+_W+'}rPr')
            if len(bases) > 1 or len(properties) > 1:
                raise ExtractionError('ambiguous_word_style')
            base = bases[0].attrib.get('{'+_W+'}val') if bases else None
            if bases and not base:
                raise ExtractionError('unresolved_word_style')
            own = _word_hidden_properties(properties[0] if properties else None)
            supported = {id(flag) for props in properties for flag in props
                         if flag.tag in ('{'+_W+'}vanish', '{'+_W+'}webHidden')}
            if any(id(flag) not in supported for name in ('vanish', 'webHidden')
                   for flag in style.iter('{'+_W+'}'+name)):
                raise ExtractionError('unsupported_word_hidden_context')
            styles[ident] = (kind, base, own)
    resolved = {}
    def inherited(ident, seen):
        if ident in seen or len(seen) > 64:
            raise ExtractionError('word_style_cycle_or_depth')
        if ident not in styles:
            raise ExtractionError('unresolved_word_style')
        kind, parent, own = styles[ident]
        values = {}
        if parent is not None:
            if parent not in styles:
                raise ExtractionError('unresolved_word_style')
            if kind is not None and styles[parent][0] not in (None, kind):
                raise ExtractionError('unsupported_word_style_inheritance')
            values.update(inherited(parent, seen | {ident}))
        values.update(own)
        return values
    for ident in styles:
        resolved[ident] = inherited(ident, set())
    return styles, resolved, default_styles, document_defaults


def _word_text(node, skipped, style_gate, inherited_hidden=None):
    if node.tag in tuple('{'+_W+'}'+name for name in ('drawing','pict','object','sdt','altChunk','txbxContent')):
        return ''
    if node.tag in ('{'+_W+'}del','{'+_W+'}ins','{'+_W+'}moveFrom','{'+_W+'}moveTo'):
        skipped['revision'] += 1
        return ''
    if node.tag in ('{'+_W+'}p','{'+_W+'}r'):
        styles, resolved, defaults, document_defaults = style_gate
        paragraph = node.tag == '{'+_W+'}p'
        kind = 'paragraph' if paragraph else 'character'
        properties = node.findall('{'+_W+'}'+('pPr' if paragraph else 'rPr'))
        if len(properties) > 1:
            raise ExtractionError('ambiguous_word_properties')
        props = properties[0] if properties else None
        references = props.findall('{'+_W+'}'+('pStyle' if paragraph else 'rStyle')) if props is not None else []
        if len(references) > 1:
            raise ExtractionError('ambiguous_word_style')
        name = references[0].attrib.get('{'+_W+'}val') if references else defaults.get(kind)
        if references and not name:
            raise ExtractionError('unresolved_word_style')
        values = dict(document_defaults if paragraph or inherited_hidden is None else inherited_hidden)
        if name is not None:
            if name not in styles:
                raise ExtractionError('unresolved_word_style')
            if styles[name][0] not in (None, kind):
                raise ExtractionError('unsupported_word_style_type')
            values.update(resolved[name])
        if paragraph:
            # Paragraph-mark rPr is not a whole-paragraph visibility setting.
            # Its hidden semantics cannot safely be projected onto every run.
            if props is not None and any(True for flag in ('vanish', 'webHidden')
                                         for _ in props.iter('{'+_W+'}'+flag)):
                raise ExtractionError('unsupported_word_hidden_context')
            return ''.join(_word_text(child, skipped, style_gate, values) for child in node)
        values.update(_word_hidden_properties(props))
        if any(values.values()):
            skipped['hidden'] += 1
            return ''
    if node.tag == '{'+_W+'}t':
        return node.text or ''
    if node.tag == '{'+_W+'}tab':
        return '\t'
    if node.tag in ('{'+_W+'}br','{'+_W+'}cr'):
        return '\n'
    return ''.join(_word_text(child, skipped, style_gate, inherited_hidden) for child in node)


def extract_document(filename, mime, raw, selection, *, expected_raw_sha,
                     expected_inspection_fingerprint, expected_plan_fingerprint,
                     user_confirmed_conversion, original_id, original_version):
    plan = preview_document_conversion(filename, mime, raw, selection,
                                       original_id=original_id, original_version=original_version)
    if (user_confirmed_conversion is not True or expected_raw_sha != plan.original_sha256
            or expected_inspection_fingerprint != plan.inspection_fingerprint
            or expected_plan_fingerprint != plan.fingerprint):
        raise ExtractionError('conversion_confirmation_changed')
    if type(original_version) is not int or not 1 <= original_version <= 2**53-1 or type(original_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', original_id):
        raise ExtractionError('host_original_identity_required')
    parts, characters, hidden = [], 0, 0
    extra_notes = []
    def add(value, locator, *, formula=None, cached_value=None, value_type='text'):
        nonlocal characters
        if not value:
            return
        characters += len(value)
        if characters > selection.max_characters:
            raise ExtractionError('document_character_limit_no_truncation')
        if len(parts) >= 100000:
            raise ExtractionError('document_part_limit')
        content=value.encode('utf-8')
        parts.append(ProcessedPart(content,_hash(content),tuple(locator),plan.original_sha256,
            original_version,original_id,plan.selection_fingerprint,converter=plan.converter,formula=formula,
            cached_value=cached_value,value_type=value_type))
    if plan.kind in ('txt','md'):
        add(raw.decode('utf-8-sig'),(('document','text'),))
    elif plan.kind == 'pdf':
        from app.model_pdf_extract import PdfExtractionError, inspect_pdf_bytes, extract_pdf_pages
        try:
            # Successful metadata receipts are reused by hash. This never invokes
            # text extraction during inspection/preview. Exact approved extracts
            # are also reused for registry verification, including empty results.
            metadata = inspect_pdf_bytes(raw)
            extracted = extract_pdf_pages(raw, selection.pages, selection.max_characters,
                                          expected_metadata=metadata)
        except PdfExtractionError as error:
            raise ExtractionError(error.code) from None
        for page in extracted['pages']:
            add(page['text'], (('page',str(page['page'])),))
        empty = [str(page['page']) for page in extracted['pages'] if not page['text']]
        if empty:
            # Preserve every empty-page locator without exceeding the submission
            # loss-note bound. Splitting notes is not truncation or extra extraction.
            for offset in range(0,len(empty),50):
                extra_notes.append('以下已选页未返回已有文字层：'+','.join(empty[offset:offset+50])+'；可能为扫描图、图形、空白或不可识别编码；未做OCR，未补造文字。')
        if characters != extracted['characters']:
            raise ExtractionError('pdf_character_count_changed')
    else:
        xml, relations = _parts(raw)
        if plan.kind == 'docx':
            body=xml['word/document.xml'].find('{'+_W+'}body')
            paragraph_index=0;table_index=0;selected=set(selection.paragraphs)
            skipped={'hidden':0,'revision':0};style_gate=_word_style_gate(xml)
            for block in body:
                if block.tag=='{'+_W+'}p':
                    paragraph_index+=1
                    if not selected or paragraph_index in selected:
                        add(_word_text(block,skipped,style_gate),(('paragraph',str(paragraph_index)),))
                elif block.tag=='{'+_W+'}tbl':
                    table_index+=1
                    # A paragraph-only selection does not silently include all tables.
                    if selected:
                        continue
                    for row_index,row in enumerate(block.findall('{'+_W+'}tr'),1):
                        for column,cell in enumerate(row.findall('{'+_W+'}tc'),1):
                            texts=[_word_text(p,skipped,style_gate) for p in cell.findall('{'+_W+'}p')]
                            add('\n'.join(texts),(('table',str(table_index)),('row',str(row_index)),('cell',str(column))))
            if selected and not selected <= set(range(1,paragraph_index+1)):
                raise ExtractionError('selected_paragraph_missing')
            hidden=skipped['hidden']
            if skipped['revision']:
                extra_notes.append('已跳过修订插入/删除或移动内容块：'+str(skipped['revision']))
        elif plan.kind == 'xlsx':
            workbook=xml['xl/workbook.xml'];sheets=workbook.find('{'+_X+'}sheets')
            chosen={item.name:item for item in selection.sheets};seen_names=set()
            shared=[]
            shared_targets=[target for typ,target in relations.get('xl/workbook.xml',{}).values() if typ==_R+'/sharedStrings']
            if len(shared_targets)>1:
                raise ExtractionError('ambiguous_shared_strings')
            if shared_targets:
                for item in xml[shared_targets[0]].findall('{'+_X+'}si'):
                    shared.append(''.join(node.text or '' for child in item if child.tag!='{'+_X+'}rPh' for node in child.iter('{'+_X+'}t')))
            for sheet in sheets:
                name=sheet.attrib.get('name','')
                if not name or name in seen_names:
                    raise ExtractionError('ambiguous_sheet_name')
                seen_names.add(name)
                if chosen and name not in chosen:
                    continue
                if sheet.attrib.get('state','visible')!='visible' and not selection.include_hidden:
                    hidden+=1
                    if name in chosen:
                        raise ExtractionError('hidden_sheet_requires_explicit_selection')
                    continue
                rid=sheet.attrib['{'+_R+'}id'];target=relations['xl/workbook.xml'][rid][1]
                bound=chosen.get(name)
                bounds=bound.cell_range.split(':') if bound and bound.cell_range else None
                first,last=(_cell(bounds[0]),_cell(bounds[-1])) if bounds else (None,None)
                visited=set()
                for cell in xml[target].iter('{'+_X+'}c'):
                    ref=cell.attrib.get('r','');column,row=_cell(ref)
                    if ref in visited:
                        raise ExtractionError('duplicate_cell_reference')
                    visited.add(ref)
                    if bounds and not (first[0]<=column<=last[0] and first[1]<=row<=last[1]):
                        continue
                    kind=cell.attrib.get('t','n');v=cell.find('{'+_X+'}v');cache=(v.text or '') if v is not None else None
                    formula=cell.find('{'+_X+'}f')
                    if formula is not None:
                        if formula.attrib.get('t','normal') != 'normal' or not formula.text:
                            raise ExtractionError('unsupported_shared_or_array_formula_no_invented_expansion')
                        if kind not in ('n', 'str', 'b', 'e', 'd'):
                            raise ExtractionError('unsupported_formula_cache_type')
                        if cache is not None:
                            if kind == 'n' and (not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', cache)
                                                or not math.isfinite(float(cache))):
                                raise ExtractionError('invalid_numeric_formula_cache')
                            if kind == 'b' and cache not in ('0', '1'):
                                raise ExtractionError('invalid_boolean_formula_cache')
                            if kind == 'e' and cache not in ('#NULL!', '#DIV/0!', '#VALUE!', '#REF!', '#NAME?', '#NUM!', '#N/A', '#GETTING_DATA', '#SPILL!', '#CALC!'):
                                raise ExtractionError('invalid_error_formula_cache')
                            if kind == 'd':
                                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?', cache):
                                    raise ExtractionError('invalid_date_formula_cache')
                                try:
                                    datetime.fromisoformat(cache)
                                except ValueError:
                                    raise ExtractionError('invalid_date_formula_cache') from None
                        expression=formula.text
                        displayed = '未知（缺少缓存）' if cache is None else '空字符串' if cache == '' else cache
                        value='公式：'+expression+'\n缓存值（未经计算，可能过期）：'+displayed
                        add(value,(('sheet',name),('cell',ref)),formula=expression,cached_value=cache,value_type='formula')
                    elif kind=='s':
                        if cache is None or not cache.isascii() or not cache.isdecimal() or int(cache)>=len(shared):
                            raise ExtractionError('invalid_shared_string_index')
                        add(shared[int(cache)],(('sheet',name),('cell',ref)),value_type='shared_string')
                    elif kind=='inlineStr':
                        inline=cell.find('{'+_X+'}is')
                        value=''.join(node.text or '' for child in inline if child.tag!='{'+_X+'}rPh' for node in child.iter('{'+_X+'}t')) if inline is not None else ''
                        add(value,(('sheet',name),('cell',ref)),value_type='inline_string')
                    elif kind=='b':
                        if cache not in ('0','1'):
                            raise ExtractionError('invalid_boolean_cell')
                        add('TRUE' if cache=='1' else 'FALSE',(('sheet',name),('cell',ref)),value_type='boolean')
                    elif kind in ('n','str','e','d'):
                        if cache is not None:
                            add(cache,(('sheet',name),('cell',ref)),value_type={'n':'numeric_unformatted','str':'string','e':'error','d':'iso_date'}[kind])
                    else:
                        raise ExtractionError('unsupported_cell_type')
            if chosen.keys()-seen_names:
                raise ExtractionError('selected_sheet_missing')
        else:
            listing=xml['ppt/presentation.xml'].find('{'+_P+'}sldIdLst');selected=set(selection.slides)
            if selected and not selected<=set(range(1,len(listing)+1)):
                raise ExtractionError('selected_slide_missing')
            for index,slide_id in enumerate(listing,1):
                if selected and index not in selected:
                    continue
                target=relations['ppt/presentation.xml'][slide_id.attrib['{'+_R+'}id']][1];slide=xml[target]
                if slide.attrib.get('show','1') in ('0','false') and not selection.include_hidden:
                    hidden+=1
                    if index in selected:
                        raise ExtractionError('hidden_slide_requires_explicit_selection')
                    continue
                tree=slide.find('{'+_P+'}cSld/{'+_P+'}spTree')
                if tree is None:
                    continue
                # XML order is retained. Tables have their cell/paragraph provenance,
                # not presentation-order filenames or a guessed visual reading order.
                paragraph=0;table=0
                for shape in tree.iter():
                    if shape.tag=='{'+_A+'}tbl':
                        table+=1
                        for row_index,row in enumerate(shape.findall('{'+_A+'}tr'),1):
                            for col,cell in enumerate(row.findall('{'+_A+'}tc'),1):
                                texts=[''.join(t.text or '' for t in p.iter('{'+_A+'}t')) for p in cell.iter('{'+_A+'}p')]
                                add('\n'.join(texts),(('slide',str(index)),('table',str(table)),('row',str(row_index)),('cell',str(col))))
                    elif shape.tag=='{'+_P+'}sp':
                        for p in shape.iter('{'+_A+'}p'):
                            paragraph+=1
                            add(''.join(t.text or '' for t in p.iter('{'+_A+'}t')),(('slide',str(index)),('paragraph',str(paragraph))))
    return ExtractionResult(tuple(parts),plan.original_sha256,plan.inspection_fingerprint,
        plan.fingerprint,plan.selection_fingerprint,characters,hidden,plan.loss_notes+tuple(extra_notes),converter=plan.converter)
