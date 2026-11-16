"""Bounded, standard-library attachment preflight, never a converter or provider probe.

Only caller-supplied bytes are inspected. The static PNG path actually decompresses
and unfilters bounded scanlines; JPEG/WebP use the reviewed pinned Pillow provider
when available and really decode it. PDF uses the fixed host Job parser for
metadata only; conversion remains a separately approved explicit-page operation.
MP4/WebM require a real bounded host ffprobe; failure stays explicitly unverified.
No file paths, Office applications, network, formulas, links, macros or embedded
objects are executed. Successful OOXML validation
means its bounded structure is eligible for a separate extractor, NOT that extraction
or a model provider's native attachment support has been implemented or verified.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import io
import posixpath
import re
import stat
import struct
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile
import zlib


class InspectionError(ValueError):
    """Metadata-only rejection; never contains attachment body or parser diagnostics."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _reject(code):
    raise InspectionError(code)


@dataclass(frozen=True)
class InspectionPolicy:
    max_bytes: int = 15 * 1024**2
    max_entries: int = 1000
    max_entry_bytes: int = 8 * 1024**2
    max_uncompressed_bytes: int = 64 * 1024**2
    max_ratio: int = 100
    max_xml_bytes: int = 4 * 1024**2
    max_xml_nodes: int = 50000
    max_xml_depth: int = 64
    max_sheets: int = 100
    max_cells: int = 100000
    max_slides: int = 200
    max_text_characters: int = 100000

    def __post_init__(self):
        ceilings = {
            'max_bytes': 15 * 1024**2, 'max_entries': 1000,
            'max_entry_bytes': 8 * 1024**2, 'max_uncompressed_bytes': 64 * 1024**2,
            'max_ratio': 100, 'max_xml_bytes': 4 * 1024**2,
            'max_xml_nodes': 50000, 'max_xml_depth': 64, 'max_sheets': 100,
            'max_cells': 100000, 'max_slides': 200, 'max_text_characters': 100000,
        }
        for key, ceiling in ceilings.items():
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError('invalid bounded inspection policy: ' + key)
        if self.max_xml_bytes > self.max_entry_bytes or self.max_entry_bytes > self.max_uncompressed_bytes:
            raise ValueError('inconsistent inspection byte budgets')


@dataclass(frozen=True)
class AttachmentInspection:
    kind: str
    byte_length: int
    structural_validation: str
    ready_for_extraction: bool
    converter_required: bool
    native_provider_support_verified: bool = False
    text_characters: int | None = None
    archive_entries: int = 0
    uncompressed_bytes: int = 0
    xml_nodes: int = 0
    sheets: int = 0
    cells: int = 0
    formulas: int = 0
    slides: int = 0
    pages: int = 0
    pdf_objects: int = 0
    pdf_nodes: int = 0
    pdf_decoded_work_bytes: int = 0
    pdf_image_objects: int = 0
    parser_version: str | None = None
    delivery: str = 'unavailable'
    diagnostic: str | None = None
    video_metadata: dict | None = None
    loss_notes: tuple[str, ...] = ()
    unsupported_features: tuple[str, ...] = ()
    limits: tuple[tuple[str, int], ...] = ()
    context_notes: tuple[str, ...] = (
        'A separate request-level gate must enforce 8 attachments and the 16-frame video budget.',
        'Document extraction and the request gate must enforce 100000 total characters across at most 8 originals; never silently truncate.',
        'This report is metadata only, not extracted document text or provider capability evidence.',
    )

    def to_dict(self):
        value = asdict(self)
        value['limits'] = dict(self.limits)
        if self.kind != 'pdf':
            # PDF resource fields must not leak into unrelated wire contracts.
            for key in ('pages','pdf_objects','pdf_nodes','pdf_decoded_work_bytes',
                        'pdf_image_objects','parser_version'):
                value.pop(key)
        if self.kind not in ('pdf','mp4','webm'):
            value.pop('delivery'); value.pop('diagnostic')
        if self.kind not in ('mp4','webm'):
            value.pop('video_metadata')
        return value


_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
_OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
_DOC_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
_XLS_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
_PPT_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
_MAIN = {
    'docx': ('word/document.xml', _DOC_MIME + '.main+xml', '{' + _W + '}document'),
    'xlsx': ('xl/workbook.xml', _XLS_MIME + '.main+xml', '{' + _X + '}workbook'),
    'pptx': ('ppt/presentation.xml', _PPT_MIME + '.main+xml', '{' + _P + '}presentation'),
}
_MIMES = {
    'txt': {'text/plain'}, 'md': {'text/markdown', 'text/x-markdown', 'text/plain'},
    'docx': {_DOC_MIME}, 'xlsx': {_XLS_MIME}, 'pptx': {_PPT_MIME},
    'pdf': {'application/pdf'}, 'png': {'image/png'}, 'jpg': {'image/jpeg'},
    'jpeg': {'image/jpeg'}, 'webp': {'image/webp'}, 'mp4': {'video/mp4'}, 'webm': {'video/webm'},
}
_RESERVED = re.compile(r'(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$')
_DANGEROUS_TYPE = re.compile(r'macroenabled|vbaproject|oleobject|activex|digital-signature', re.I)
_DANGEROUS_REL = re.compile(r'/(?:oleObject|package|control|vbaProject|attachedTemplate|externalLink|externalLinkPath|aFChunk|digital-signature[^/]*)$', re.I)


def _name(value, *, directory=False):
    if not isinstance(value, str) or not value or len(value) > 1024:
        _reject('unsafe_archive_path')
    if directory and value.endswith('/'):
        value = value[:-1]
    if (not value or value.startswith('/') or '\\' in value or ':' in value or '%' in value
            or any(ord(c) < 32 or c in '<>"|?*' for c in value)):
        _reject('unsafe_archive_path')
    if any(part in ('', '.', '..') or part.endswith((' ', '.')) or _RESERVED.fullmatch(part)
           for part in value.split('/')):
        _reject('unsafe_archive_path')
    return value


def _extra(extra):
    index = 0
    while index < len(extra):
        if len(extra) - index < 4:
            _reject('malformed_zip_extra')
        ident, size = struct.unpack_from('<HH', extra, index)
        index += 4
        if index + size > len(extra):
            _reject('malformed_zip_extra')
        if ident == 1:
            _reject('zip64_unsupported')
        index += size


def _xml(raw, policy, aggregate):
    if len(raw) > policy.max_xml_bytes:
        _reject('xml_byte_limit')
    # Removing NULs recognizes UTF-16/32 declaration tokens before any XML parser
    # can expand them. No DTD or entity definitions are needed for OOXML parts.
    lexical = raw.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in lexical or b'<!ENTITY' in lexical:
        _reject('xml_dtd_or_entity_forbidden')
    depth = 0
    try:
        parser = ET.XMLPullParser(events=('start', 'end'))
        for index in range(0, len(raw), 4096):
            parser.feed(raw[index:index + 4096])
            for event, _ in parser.read_events():
                if event == 'start':
                    depth += 1
                    aggregate[0] += 1
                    if depth > policy.max_xml_depth or aggregate[0] > policy.max_xml_nodes:
                        _reject('xml_structure_limit')
                else:
                    depth -= 1
        parser.close()
        return ET.fromstring(raw)
    except InspectionError:
        raise
    except (ET.ParseError, ValueError, LookupError):
        _reject('invalid_xml')


def _rel_source(path):
    if path == '_rels/.rels':
        return ''
    parent, filename = posixpath.split(path)
    if not parent.endswith('/_rels') or not filename.endswith('.rels'):
        _reject('invalid_relationship_part')
    source = posixpath.join(parent[:-len('/_rels')], filename[:-len('.rels')])
    return source


def _resolve_target(source, raw):
    if not isinstance(raw, str) or not raw or len(raw) > 2048 or '\\' in raw:
        _reject('invalid_relationship_target')
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query:
        _reject('external_relationship_forbidden')
    target = unquote(parsed.path, errors='strict')
    if '%' in target or '\\' in target or ':' in target:
        _reject('invalid_relationship_target')
    if not target:
        if not source:
            _reject('invalid_relationship_target')
        result = source
    elif target.startswith('/'):
        result = posixpath.normpath(target[1:])
    else:
        result = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    return _name(result)


def _inspect_ooxml(kind, raw, policy):
    main_path, main_content_type, main_tag = _MAIN[kind]
    if not raw.startswith(b'PK\x03\x04'):
        _reject('file_magic_mismatch')
    aggregate = [0]
    # ZIP64 end records and multi-disk layouts are unsupported even if the reader
    # could transparently accept them. Validate the actual EOCD, not body substrings.
    end = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        _reject('invalid_ooxml_archive')
    footer = struct.unpack_from('<4s4H2IH', raw, end)
    if end + 22 + footer[7] != len(raw):
        _reject('invalid_zip_footer')
    if footer[1] or footer[2] or footer[3] != footer[4]:
        _reject('multidisk_zip_unsupported')
    if (footer[3] == 0xffff or footer[4] == 0xffff or footer[5] == 0xffffffff
            or footer[6] == 0xffffffff or raw[max(0, end-20):end].startswith(b'PK\x06\x07')):
        _reject('zip64_unsupported')
    try:
        stream = io.BytesIO(raw)
        with zipfile.ZipFile(stream, 'r', allowZip64=False) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= policy.max_entries:
                _reject('archive_entry_limit')
            total = 0
            names, paths, directories, spellings = {}, {}, set(), {}
            for info in infos:
                name = _name(info.filename, directory=info.is_dir())
                folded = name.casefold()
                if folded in names:
                    _reject('duplicate_archive_path')
                names[folded] = name
                pieces = name.split('/')
                for index in range(1, len(pieces) + 1):
                    prefix = '/'.join(pieces[:index])
                    key = prefix.casefold()
                    if key in spellings and spellings[key] != prefix:
                        _reject('archive_case_collision')
                    spellings[key] = prefix
                for index in range(1, len(pieces)):
                    prefix = '/'.join(pieces[:index])
                    key = prefix.casefold()
                    if key in paths and not paths[key]:
                        _reject('archive_path_collision')
                    if key in names and names[key] != prefix:
                        _reject('archive_case_collision')
                    directories.add(key)
                if not info.is_dir() and folded in directories:
                    _reject('archive_path_collision')
                paths[folded] = info.is_dir()
                mode = info.external_attr >> 16
                if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    _reject('archive_special_file')
                if (info.is_dir() and info.file_size != 0
                        or stat.S_IFMT(mode) == stat.S_IFDIR and not info.is_dir()
                        or stat.S_IFMT(mode) == stat.S_IFREG and info.is_dir()):
                    _reject('archive_entry_kind_mismatch')
                if info.flag_bits & (1 | 0x40):
                    _reject('encrypted_zip_unsupported')
                if info.extract_version >= 45 or info.file_size >= 0xffffffff or info.compress_size >= 0xffffffff:
                    _reject('zip64_unsupported')
                _extra(info.extra)
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    _reject('zip_compression_unsupported')
                if info.file_size > policy.max_entry_bytes:
                    _reject('archive_entry_byte_limit')
                total += info.file_size
                if total > policy.max_uncompressed_bytes:
                    _reject('archive_total_limit')
                if info.file_size and info.file_size > max(1, info.compress_size) * policy.max_ratio:
                    _reject('archive_ratio_limit')
                if (any(part.casefold() in ('embeddings', 'activex', '_xmlsignatures') for part in pieces)
                        or any('vbaproject' in part.casefold() for part in pieces)
                        or name.lower().endswith(('.bin', '.vml'))):
                    _reject('active_or_embedded_content_forbidden')
            # Validate local headers too: central metadata alone can hide encryption,
            # ZIP64 or unsupported flags until a particular entry is opened.
            for info in infos:
                offset = info.header_offset
                if offset < 0 or offset + 30 > len(raw) or raw[offset:offset+4] != b'PK\x03\x04':
                    _reject('invalid_local_zip_header')
                fields = struct.unpack_from('<4s5H3I2H', raw, offset)
                if fields[2] & (1 | 0x40):
                    _reject('encrypted_zip_unsupported')
                if fields[1] >= 45 or fields[7] == 0xffffffff or fields[8] == 0xffffffff:
                    _reject('zip64_unsupported')
                local_extra_start = offset + 30 + fields[9]
                local_data_start = local_extra_start + fields[10]
                if local_data_start > len(raw) or local_data_start + info.compress_size > end:
                    _reject('invalid_local_zip_header')
                _extra(raw[local_extra_start:local_data_start])
            parts = {}
            observed_total = 0
            for info in infos:
                if info.is_dir():
                    continue
                data = bytearray()
                with archive.open(info) as entry:
                    while True:
                        block = entry.read(min(65536, policy.max_entry_bytes - len(data) + 1))
                        if not block:
                            break
                        data.extend(block)
                        observed_total += len(block)
                        if len(data) > policy.max_entry_bytes or observed_total > policy.max_uncompressed_bytes:
                            _reject('archive_stream_limit')
                if len(data) != info.file_size:
                    _reject('archive_size_mismatch')
                if data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
                    _reject('embedded_ole_magic_forbidden')
                parts[info.filename] = bytes(data)
            if '[Content_Types].xml' not in parts or '_rels/.rels' not in parts or main_path not in parts:
                _reject('missing_required_parts')
            xml = {}
            for name, data in parts.items():
                if name.endswith(('.xml', '.rels')):
                    xml[name] = _xml(data, policy, aggregate)
            types = xml['[Content_Types].xml']
            if types.tag != '{' + _CT + '}Types':
                _reject('invalid_content_types')
            overrides, defaults = {}, {}
            for item in types:
                content_type = item.attrib.get('ContentType', '')
                if not content_type or _DANGEROUS_TYPE.search(content_type):
                    _reject('active_content_type_forbidden')
                if item.tag == '{' + _CT + '}Override':
                    part = item.attrib.get('PartName', '')
                    if not part.startswith('/'):
                        _reject('invalid_content_types')
                    part = _name(part[1:])
                    if part in overrides or part not in parts:
                        _reject('invalid_content_types')
                    overrides[part] = content_type
                elif item.tag == '{' + _CT + '}Default':
                    extension = item.attrib.get('Extension', '')
                    if not extension or extension.casefold() in defaults or '/' in extension or '.' in extension:
                        _reject('invalid_content_types')
                    defaults[extension.casefold()] = content_type
                else:
                    _reject('invalid_content_types')
            if overrides.get(main_path) != main_content_type or xml[main_path].tag != main_tag:
                _reject('document_type_mismatch')
            for path in parts:
                if path == '[Content_Types].xml':
                    continue
                if path not in overrides and path.rsplit('.', 1)[-1].casefold() not in defaults:
                    _reject('undeclared_part_type')
            relationships = {}
            for name, element in xml.items():
                if not name.endswith('.rels'):
                    continue
                if element.tag != '{' + _REL + '}Relationships':
                    _reject('invalid_relationship_part')
                source = _rel_source(name)
                if source and source not in parts:
                    _reject('orphan_relationship_part')
                relations = {}
                for item in element:
                    ident = item.attrib.get('Id', '')
                    relation_type = item.attrib.get('Type', '')
                    if item.tag != '{' + _REL + '}Relationship' or not ident or ident in relations or not relation_type:
                        _reject('invalid_relationship_part')
                    if item.attrib.get('TargetMode', 'Internal') != 'Internal' or _DANGEROUS_REL.search(relation_type):
                        _reject('external_or_active_relationship_forbidden')
                    target = _resolve_target(source, item.attrib.get('Target'))
                    if target not in parts:
                        _reject('missing_relationship_target')
                    relations[ident] = (relation_type, target)
                relationships[source] = relations
            office_documents = [target for typ, target in relationships.get('', {}).values()
                                if typ == _OFFICE_REL + '/officeDocument']
            if office_documents != [main_path]:
                _reject('invalid_office_document_relationship')
            sheets = cells = formulas = slides = 0
            notes = ['OOXML structures were validated; text, styles, media and metadata were not extracted.',
                     'Embedded images, comments, notes, headers and layout may require separate bounded extraction.']
            if kind == 'docx':
                if xml[main_path].find('{' + _W + '}body') is None:
                    _reject('missing_document_body')
            elif kind == 'xlsx':
                sheet_nodes = xml[main_path].find('{' + _X + '}sheets')
                if sheet_nodes is None or not 1 <= len(sheet_nodes) <= policy.max_sheets:
                    _reject('sheet_limit_or_missing')
                linked = set()
                for item in sheet_nodes:
                    rid = item.attrib.get('{' + _OFFICE_REL + '}id')
                    if item.tag != '{' + _X + '}sheet' or rid not in relationships.get(main_path, {}):
                        _reject('invalid_sheet_relationship')
                    typ, target = relationships[main_path][rid]
                    if typ != _OFFICE_REL + '/worksheet' or target in linked or xml.get(target) is None or xml[target].tag != '{' + _X + '}worksheet':
                        _reject('invalid_sheet_relationship')
                    linked.add(target)
                sheets = len(linked)
                all_sheets = [node for node in xml.values() if node.tag == '{' + _X + '}worksheet']
                if len(all_sheets) != sheets:
                    _reject('unlinked_worksheet')
                for element in all_sheets:
                    cells += sum(1 for item in element.iter('{' + _X + '}c'))
                    formulas += sum(1 for item in element.iter('{' + _X + '}f'))
                if cells > policy.max_cells:
                    _reject('cell_limit')
                notes.append('Formulas are not evaluated; cached values may be stale or absent and never imply zero.')
            elif kind == 'pptx':
                listing = xml[main_path].find('{' + _P + '}sldIdLst')
                if listing is None or not 1 <= len(listing) <= policy.max_slides:
                    _reject('slide_limit_or_missing')
                linked = set()
                for item in listing:
                    rid = item.attrib.get('{' + _OFFICE_REL + '}id')
                    if item.tag != '{' + _P + '}sldId' or rid not in relationships.get(main_path, {}):
                        _reject('invalid_slide_relationship')
                    typ, target = relationships[main_path][rid]
                    if typ != _OFFICE_REL + '/slide' or target in linked or xml.get(target) is None or xml[target].tag != '{' + _P + '}sld':
                        _reject('invalid_slide_relationship')
                    linked.add(target)
                slides = len(linked)
                if sum(node.tag == '{' + _P + '}sld' for node in xml.values()) != slides:
                    _reject('unlinked_slide')
            return AttachmentInspection(kind, len(raw), 'bounded_ooxml_structure_validated', True, True,
                archive_entries=len(infos), uncompressed_bytes=observed_total, xml_nodes=aggregate[0],
                sheets=sheets, cells=cells, formulas=formulas, slides=slides,
                loss_notes=tuple(notes), unsupported_features=('formula_evaluation',) if formulas else (),
                limits=tuple(asdict(policy).items()))
    except InspectionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
            OSError, ValueError, UnicodeError, struct.error, OverflowError):
        _reject('invalid_ooxml_archive')


def _inspect_png(raw, policy):
    """Actually validate and unfilter a bounded static PNG using stdlib only.

    No rendering/colour conversion is claimed. Adam7/APNG and unknown critical
    chunks are explicitly unsupported, not silently flattened or sent as verified.
    """
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        _reject('file_magic_mismatch')
    offset = 8
    chunks = 0
    compressed = bytearray()
    header = None
    palette = None
    saw_end = closed_data = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            _reject('png_chunk_structure')
        size = int.from_bytes(raw[offset:offset+4], 'big')
        typ = raw[offset+4:offset+8]
        if (size > policy.max_bytes or offset+12+size > len(raw)
                or not re.fullmatch(rb'[A-Za-z]{4}', typ)):
            _reject('png_chunk_structure')
        body = raw[offset+8:offset+8+size]
        crc = int.from_bytes(raw[offset+8+size:offset+12+size], 'big')
        if zlib.crc32(typ+body) & 0xffffffff != crc:
            _reject('png_crc_mismatch')
        chunks += 1
        if chunks > policy.max_entries or saw_end:
            _reject('png_chunk_limit_or_trailing_data')
        if chunks == 1 and typ != b'IHDR':
            _reject('png_header_required')
        if typ == b'IHDR':
            if header is not None or size != 13:
                _reject('png_header_invalid')
            width, height, depth, colour, method, filtering, interlace = struct.unpack('>IIBBBBB', body)
            legal = {0:(1,2,4,8,16), 2:(8,16), 3:(1,2,4,8), 4:(8,16), 6:(8,16)}
            if (not 1 <= width <= 8192 or not 1 <= height <= 8192 or width*height > 16777216
                    or depth not in legal.get(colour, ()) or method or filtering):
                _reject('png_dimensions_or_colour_limit')
            if interlace:
                _reject('png_adam7_decoder_not_implemented')
            header = (width, height, depth, colour)
        elif typ == b'PLTE':
            if palette is not None or compressed or not 0 < size <= 768 or size % 3:
                _reject('png_palette_invalid')
            palette = size // 3
        elif typ in (b'acTL', b'fcTL', b'fdAT'):
            _reject('png_animation_not_implemented')
        elif typ in (b'iCCP', b'zTXt', b'iTXt'):
            # Scanline decoding alone does not bound compressed ancillary profiles
            # or text. Do not preserve them as an allegedly fully verified image.
            _reject('png_compressed_ancillary_decoder_not_implemented')
        elif typ == b'IDAT':
            if closed_data or not size or len(compressed)+size > policy.max_bytes:
                _reject('png_data_order_or_limit')
            compressed.extend(body)
        elif typ == b'IEND':
            if size or not compressed:
                _reject('png_end_invalid')
            saw_end = True
        elif typ[0] & 32 == 0:
            _reject('png_unknown_critical_chunk')
        if typ != b'IDAT' and compressed:
            closed_data = True
        offset += 12+size
    if not saw_end or header is None:
        _reject('png_incomplete')
    width, height, depth, colour = header
    if colour == 3 and (palette is None or palette > 2**depth):
        _reject('png_palette_required')
    if colour in (0,4) and palette is not None:
        _reject('png_palette_invalid')
    channels = {0:1, 2:3, 3:1, 4:2, 6:4}[colour]
    row_size = (width*channels*depth+7)//8
    expected = (row_size+1)*height
    # The Python unfilter loop has an intentionally smaller CPU/memory admission
    # limit than the aggregate archive budget. Larger images are refused, not resized.
    if expected > min(policy.max_uncompressed_bytes, 8 * 1024**2):
        _reject('png_decoded_byte_limit')
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed), expected+1)
        if (len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail):
            _reject('png_decoded_size_or_stream_invalid')
    except zlib.error:
        _reject('png_compression_invalid')
    stride = max(1, (channels*depth+7)//8)
    previous = bytearray(row_size)
    for line in range(height):
        start = line*(row_size+1)
        filter_kind = pixels[start]
        if filter_kind > 4:
            _reject('png_filter_invalid')
        current = bytearray(pixels[start+1:start+1+row_size])
        for index in range(row_size):
            left = current[index-stride] if index >= stride else 0
            above = previous[index]
            upper_left = previous[index-stride] if index >= stride else 0
            if filter_kind == 1:
                predictor = left
            elif filter_kind == 2:
                predictor = above
            elif filter_kind == 3:
                predictor = (left+above)//2
            elif filter_kind == 4:
                base = left+above-upper_left
                distances = (abs(base-left), abs(base-above), abs(base-upper_left))
                predictor = (left,above,upper_left)[distances.index(min(distances))]
            else:
                predictor = 0
            current[index] = (current[index]+predictor) & 255
        if colour == 3:
            for pixel in range(width):
                bit = pixel*depth
                value = (current[bit//8] >> (8-depth-bit%8)) & (2**depth-1)
                if value >= palette:
                    _reject('png_palette_index_invalid')
        previous = current
    return AttachmentInspection('png', len(raw), 'bounded_static_png_decoded', False, False,
        loss_notes=('完整原始PNG字节；已校验CRC、尺寸、像素预算与扫描行解码；不执行文字识别，不声称已验证提供方能力。',),
        limits=tuple(asdict(policy).items()))


def _inspect_pinned_raster(kind, raw, policy):
    """Use only the reviewed bundled Pillow provider, never install/search a tool.

    Dependency integrity and actual byte verification/decode are distinct checks.
    No thumbnail, resizing, EXIF transpose, OCR or colour conversion is performed.
    """
    try:
        from app.model_media_dependencies import require_python_dependency
        require_python_dependency('pillow')
    except Exception:
        return AttachmentInspection(kind, len(raw), 'magic_only_unverified_media', False, True,
            loss_notes=('原件保留；已审核的Pillow依赖不可用，未解码、变换或发送。',),
            unsupported_features=('reviewed_pillow_dependency_unavailable',),
            limits=tuple(asdict(policy).items()))
    try:
        import warnings
        from PIL import Image
        expected = {'jpeg':'JPEG', 'webp':'WEBP'}[kind]
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if (image.format != expected or getattr(image, 'n_frames', 1) != 1
                        or image.width < 1 or image.height < 1
                        or image.width > 8192 or image.height > 8192
                        or image.width*image.height > 16777216
                        or image.width*image.height*len(image.getbands()) > policy.max_uncompressed_bytes):
                    _reject('image_dimensions_frames_or_decoded_limit')
                image.verify()
            with Image.open(io.BytesIO(raw)) as decoded:
                if decoded.format != expected:
                    _reject('image_format_changed_during_decode')
                decoded.load()
        return AttachmentInspection(kind, len(raw), 'bounded_pinned_pillow_decoded', False, False,
            loss_notes=('完整原始图像字节；已实际校验并解码单帧及像素预算，不做缩放、裁切、OCR或EXIF变换；提供方能力仍未实测。',),
            limits=tuple(asdict(policy).items()))
    except InspectionError:
        raise
    except Exception:
        _reject('image_decode_failed_or_unsupported')


def inspect_attachment_bytes(filename, mime, raw, policy=None):
    """Inspect bounded provided bytes. No body/path is included in metadata reports."""
    policy = InspectionPolicy() if policy is None else policy
    if type(policy) is not InspectionPolicy:
        raise TypeError('explicit InspectionPolicy required')
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 255
            or any(c in filename for c in '/\\:\x00') or any(ord(c) < 32 for c in filename)
            or filename.endswith((' ', '.'))):
        _reject('invalid_filename')
    if not isinstance(mime, str) or len(mime) > 200:
        _reject('invalid_mime')
    # MIME parameters are not interpreted; only plain UTF-8 text's charset marker is allowed.
    bits = [part.strip().lower() for part in mime.split(';')]
    if len(bits) > 1 and bits[1:] != ['charset=utf-8']:
        _reject('invalid_mime')
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext == 'markdown':
        ext = 'md'  # Existing upload UI accepts this equivalent Markdown extension.
    if ext not in _MIMES:
        _reject('unsupported_attachment_type')
    if bits[0] not in _MIMES[ext] or len(bits) > 1 and ext not in ('txt', 'md'):
        _reject('extension_mime_mismatch')
    if type(raw) is not bytes:
        _reject('bytes_required')
    if not raw or len(raw) > policy.max_bytes:
        _reject('attachment_byte_limit')
    if raw.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        _reject('legacy_or_encrypted_office_unsupported')
    if ext in _MAIN:
        return _inspect_ooxml(ext, raw, policy)
    limits = tuple(asdict(policy).items())
    if ext in ('txt', 'md'):
        if raw.startswith((b'PK\x03\x04', b'%PDF-', b'\x89PNG', b'\xff\xd8\xff')) or b'\x00' in raw:
            _reject('text_magic_mismatch')
        try:
            value = raw.decode('utf-8-sig', errors='strict')
        except UnicodeError:
            _reject('text_requires_utf8')
        if len(value) > policy.max_text_characters:
            _reject('text_character_limit')
        return AttachmentInspection(ext, len(raw), 'utf8_text_validated', True, False,
            text_characters=len(value), limits=limits,
            loss_notes=('Text only; Markdown links, HTML and code blocks are not executed or fetched.',))
    if ext == 'pdf':
        if not re.match(rb'%PDF-(?:1\.[0-7]|2\.0)(?:\r|\n|\s)', raw[:16]):
            _reject('file_magic_mismatch')
        from app.model_pdf_extract import (PDF_VERSION, PDF_LIMITS, PDF_LOSS_NOTES,
                                           PdfExtractionError, inspect_pdf_bytes)
        pdf_limits = limits + tuple(PDF_LIMITS.items())
        try:
            metadata = inspect_pdf_bytes(raw)
        except PdfExtractionError as error:
            # Keep the exact stable diagnostic. The PDF service caches this
            # failure tombstone and never reparses the same failed request.
            return AttachmentInspection('pdf', len(raw), 'bounded_pdf_inspection_failed', False, True,
                parser_version=PDF_VERSION, diagnostic=error.code,
                loss_notes=PDF_LOSS_NOTES, unsupported_features=(error.code,), limits=pdf_limits)
        return AttachmentInspection('pdf', len(raw), 'bounded_host_job_pdf_metadata_validated', True, True,
            pages=metadata['pages'], pdf_objects=metadata['objects'], pdf_nodes=metadata['nodes'],
            pdf_decoded_work_bytes=metadata['decoded_work_bytes'], pdf_image_objects=metadata['image_objects'],
            parser_version=PDF_VERSION, delivery='extracted_text', loss_notes=PDF_LOSS_NOTES,
            unsupported_features=('ocr','layout_preservation','native_pdf_delivery'), limits=pdf_limits)
    if ext == 'png':
        return _inspect_png(raw, policy)
    elif ext in ('jpg', 'jpeg'):
        valid = raw.startswith(b'\xff\xd8\xff')
    elif ext == 'webp':
        valid = len(raw) >= 12 and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'
    elif ext in ('mp4','webm'):
        from app.model_video_extract import VideoExtractionError, probe_video
        notes = ('原始MP4/WebM字节保留；本检查不转码、抽帧或发送模型。',
                 '受控ffprobe仅验证容器、编码、时长及流元数据，不代表全片解码、安全保证或提供方原生能力实测。')
        try:
            probe = probe_video(filename, bits[0], raw)
        except VideoExtractionError as error:
            return AttachmentInspection(ext, len(raw), 'bounded_video_probe_failed', False, True,
                diagnostic=error.code, loss_notes=notes,
                unsupported_features=(error.code,), limits=limits)
        except Exception:
            return AttachmentInspection(ext, len(raw), 'bounded_video_probe_failed', False, True,
                diagnostic='video_probe_unavailable', loss_notes=notes,
                unsupported_features=('video_probe_unavailable',), limits=limits)
        return AttachmentInspection(ext, len(raw), probe.structural_validation, True, False,
            delivery='native', video_metadata=probe.metadata(), loss_notes=notes,
            unsupported_features=('full_video_decode_not_performed','native_provider_support_not_verified'),
            limits=limits)
    else:
        _reject('unsupported_attachment_type')
    if not valid:
        _reject('file_magic_mismatch')
    kind = 'jpeg' if ext == 'jpg' else ext
    return _inspect_pinned_raster(kind, raw, policy)
