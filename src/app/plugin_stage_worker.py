"""Independent migration-only worker entry; stdlib and the wire codec only.

This is NOT a sandbox. Only the host's native strict runtime may start this entry
with protected pipes. Never dispatch this module via the ordinary plugin worker.
No active-data path, host configuration, credential, network or model SDK is sent.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from uuid import UUID

from app.plugin_wire import decode_json, encode_json, read_frame, write_frame

CHUNK = 12000
MAX_DATA_BYTES = 256 * 1024**2
MAX_RPC = 100000
_SHA = re.compile(r'^[0-9a-f]{64}$')
_OPAQUE = re.compile(r'^[A-Za-z0-9_-]{32,100}$')


class StageWorkerError(ValueError):
    pass


def _strict(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise StageWorkerError('stage_fields')


def _relative(value):
    if type(value) is not str or not value or len(value) > 240 or '\\' in value:
        raise StageWorkerError('stage_path')
    for part in value.split('/'):
        if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
                or any(ord(c) < 32 or c in ':<>"|?*' for c in part)):
            raise StageWorkerError('stage_path')
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _ordinary(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or directory and not stat.S_ISDIR(info.st_mode)
            or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        raise StageWorkerError('stage_code_identity')
    return info


def validate_init(init):
    _strict(init, ('v', 'type', 'plugin_id', 'revision', 'digest', 'call_id', 'request_id',
                   'plan_fingerprint', 'session', 'package_root', 'manifest', 'migration',
                   'target_schema', 'source_schema', 'stage_generation', 'permissions'))
    if (type(init['v']) is not int or init['v'] != 1 or init['type'] != 'stage_init'
            or init['permissions'] != ['migration.old.read', 'migration.stage.write']
            or type(init['revision']) is not int or not 1 <= init['revision'] <= 2**53-1
            or type(init['plugin_id']) is not str
            or not re.fullmatch(r'[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+', init['plugin_id'])):
        raise StageWorkerError('stage_init')
    for name in ('plan_fingerprint', 'digest'):
        if type(init[name]) is not str or not _SHA.fullmatch(init[name]):
            raise StageWorkerError('stage_binding')
    for name in ('session', 'call_id', 'request_id'):
        if type(init[name]) is not str or not _OPAQUE.fullmatch(init[name]):
            raise StageWorkerError('stage_binding')
    if not init['call_id'].startswith('stage_'):
        raise StageWorkerError('stage_call_id')
    for name in ('source_schema', 'target_schema'):
        if type(init[name]) is not int or not 0 <= init[name] <= 2147483647:
            raise StageWorkerError('stage_schema')
    if init['target_schema'] <= init['source_schema']:
        raise StageWorkerError('stage_schema')
    try:
        if type(init['stage_generation']) is not str or str(UUID(init['stage_generation'])) != init['stage_generation']:
            raise ValueError()
    except (ValueError, AttributeError):
        raise StageWorkerError('stage_generation') from None
    manifest = init['manifest']
    if (type(manifest) is not dict or manifest.get('id') != init['plugin_id']
            or manifest.get('data_schema') != init['target_schema']
            or hashlib.sha256(_canonical(manifest)).hexdigest() != init['digest']):
        raise StageWorkerError('stage_manifest')
    migration = init['migration']
    _strict(migration, ('from_schema', 'to_schema', 'entrypoint'))
    if (migration.get('from_schema') != init['source_schema']
            or migration.get('to_schema') != init['target_schema']
            or sum(item == migration for item in manifest.get('migrations', [])) != 1):
        raise StageWorkerError('stage_declaration')
    entry = migration['entrypoint']; _strict(entry, ('type', 'path', 'callable'))
    if (entry['type'] != 'python' or not _relative(entry['path']).endswith('.py')
            or type(entry['callable']) is not str
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,100}', entry['callable'])):
        raise StageWorkerError('stage_entry')
    raw_root = init['package_root']
    if type(raw_root) is not str or raw_root.startswith(('\\\\', '//')):
        raise StageWorkerError('stage_root')
    root = Path(raw_root)
    if not root.is_absolute() or '..' in root.parts or str(root) != os.path.abspath(root):
        raise StageWorkerError('stage_root')
    for parent in (*reversed(root.parents), root):
        _ordinary(parent, directory=True)
    files = manifest.get('files')
    if type(files) is not dict or not 1 <= len(files) <= 1000 or entry['path'] not in files:
        raise StageWorkerError('stage_files')
    seen = set(); total = 0; count = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        count += len(dirs) + len(names)
        if count > 2001:
            raise StageWorkerError('stage_code_limit')
        for name in dirs:
            _ordinary(Path(folder) / name, directory=True)
        for name in names:
            path = Path(folder) / name; relative = _relative(path.relative_to(root).as_posix())
            before = _ordinary(path); maximum = 65536 if relative == 'manifest.json' else 8 * 1024**2
            if before.st_size > maximum:
                raise StageWorkerError('stage_code_limit')
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno()); raw = stream.read(maximum + 1); after = os.fstat(stream.fileno())
            final = _ordinary(path)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if len(raw) > maximum or any(identity(info) != identity(before) for info in (opened, after, final)):
                raise StageWorkerError('stage_code_changed')
            total += len(raw)
            if total > 32 * 1024**2:
                raise StageWorkerError('stage_code_limit')
            if relative == 'manifest.json':
                if raw != _canonical(manifest) + b'\n' or decode_json(raw) != manifest:
                    raise StageWorkerError('stage_manifest')
            elif relative not in files or hashlib.sha256(raw).hexdigest() != files[relative]:
                raise StageWorkerError('stage_code_hash')
            seen.add(relative)
    if seen != set(files) | {'manifest.json'}:
        raise StageWorkerError('stage_code_set')
    return root


class MigrationSDK:
    """Only old read/stage RW and safe config/state byte documents; no generic RPC."""
    def __init__(self, reader, writer, init):
        self._reader, self._writer, self._init = reader, writer, init
        self._sequence = 0; self._closed = False
        self.source_schema = init['source_schema']; self.target_schema = init['target_schema']
        self.stage_generation = init['stage_generation']

    def _request(self, op, params):
        if self._closed or self._sequence >= MAX_RPC:
            raise StageWorkerError('stage_session_closed')
        self._sequence += 1; request_id = secrets.token_urlsafe(24)
        message = {'v': 1, 'type': 'stage_rpc', 'session': self._init['session'],
                   'plan_fingerprint': self._init['plan_fingerprint'], 'seq': self._sequence,
                   'request_id': request_id, 'op': op, 'params': params}
        try:
            write_frame(self._writer, message); response = read_frame(self._reader)
            _strict(response, ('v', 'type', 'seq', 'request_id', 'ok', 'result'))
            if (type(response['v']) is not int or response['v'] != 1 or response['type'] != 'stage_reply'
                    or response['seq'] != self._sequence or response['request_id'] != request_id
                    or response['ok'] is not True):
                raise StageWorkerError('stage_proxy_refused')
            return response['result']
        except BaseException:
            self._closed = True
            raise

    def list_files(self, *, stage=False):
        cursor = 0
        while True:
            page = self._request('stage.list' if stage else 'old.list', {'cursor': cursor})
            for item in page['files']:
                yield dict(item)
            if page['next'] is None:
                return
            cursor = page['next']

    def read_chunks(self, path, *, stage=False):
        _relative(path); offset = 0
        while True:
            result = self._request('stage.read' if stage else 'old.read',
                                   {'path': path, 'offset': offset, 'length': CHUNK})
            content = base64.b64decode(result['content_base64'], validate=True)
            if len(content) > CHUNK or hashlib.sha256(content).hexdigest() != result['sha256']:
                raise StageWorkerError('stage_read_hash')
            if content:
                yield content; offset += len(content)
            if result['eof'] is True:
                return
            if not content:
                raise StageWorkerError('stage_read_progress')

    def write_chunks(self, path, chunks):
        _relative(path)
        upload = self._request('stage.begin', {'path': path})['upload']
        digest = hashlib.sha256(); size = 0
        for part in chunks:
            if type(part) is not bytes:
                raise StageWorkerError('stage_write_bytes')
            for offset in range(0, len(part), CHUNK):
                chunk = part[offset:offset + CHUNK]; size += len(chunk)
                if size > MAX_DATA_BYTES:
                    raise StageWorkerError('stage_write_limit')
                digest.update(chunk)
                self._request('stage.append', {'upload': upload, 'content_base64': base64.b64encode(chunk).decode('ascii')})
        return self._request('stage.commit', {'upload': upload, 'size': size, 'sha256': digest.hexdigest()})

    def write_bytes(self, path, content):
        return self.write_chunks(path, (content,))

    def copy_old(self, source, destination=None):
        return self.write_chunks(destination or source, self.read_chunks(source))

    def delete_stage(self, path):
        return self._request('stage.delete', {'path': _relative(path)})

    def document(self, kind):
        if kind not in ('config', 'state'):
            raise StageWorkerError('stage_document_kind')
        parts = []; offset = 0
        while True:
            page = self._request('document.read', {'kind': kind, 'offset': offset})
            chunk = base64.b64decode(page['content_base64'], validate=True)
            if len(chunk) > CHUNK or sum(map(len, parts)) + len(chunk) > 65536:
                raise StageWorkerError('stage_document_limit')
            parts.append(chunk); offset += len(chunk)
            if page['eof'] is True:
                return json.loads(b''.join(parts))
            if not chunk:
                raise StageWorkerError('stage_document_progress')

    def set_document(self, kind, value):
        if kind not in ('config', 'state') or type(value) is not dict:
            raise StageWorkerError('stage_document_kind')
        raw = _canonical(value)
        if len(raw) > 65536:
            raise StageWorkerError('stage_document_limit')
        self._request('document.begin', {'kind': kind})
        for offset in range(0, len(raw), CHUNK):
            self._request('document.append', {'kind': kind, 'content_base64': base64.b64encode(raw[offset:offset + CHUNK]).decode('ascii')})
        return self._request('document.commit', {'kind': kind, 'sha256': hashlib.sha256(raw).hexdigest()})


class _Diagnostics:
    def __init__(self, stream):
        self.stream = stream; self.remaining = 4096
    def write(self, text):
        if type(text) is not str:
            return 0
        count = len(text); part = text[:self.remaining]; self.remaining -= len(part)
        if part:
            self.stream.write(part)
        return count
    def flush(self):
        self.stream.flush()


def run_worker(reader, writer, diagnostics):
    call_id = ''; request_id = ''
    try:
        init = read_frame(reader); root = validate_init(init)
        call_id, request_id = init['call_id'], init['request_id']
        sdk = MigrationSDK(reader, writer, init)
        sys.dont_write_bytecode = True; sys.path.insert(0, str(root))
        with contextlib.redirect_stdout(_Diagnostics(diagnostics)), contextlib.redirect_stderr(_Diagnostics(diagnostics)):
            entry = init['migration']['entrypoint']
            spec = importlib.util.spec_from_file_location('_lingxi_migration_entry', root / entry['path'])
            if spec is None or spec.loader is None:
                raise StageWorkerError('stage_entry')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            handler = getattr(module, entry['callable'], None)
            if not callable(handler) or handler(sdk) is not None:
                raise StageWorkerError('stage_hook_must_return_none')
        completed = sdk._request('stage.finish', {})
        response = {'v': 1, 'type': 'stage_result', 'call_id': call_id, 'request_id': request_id,
                    'ok': True, 'result': completed}
    except BaseException:
        response = {'v': 1, 'type': 'stage_result', 'call_id': call_id, 'request_id': request_id,
                    'ok': False, 'result': None}
    write_frame(writer, response)
    return 0


def main():
    return run_worker(sys.stdin.buffer, sys.stdout.buffer, sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
