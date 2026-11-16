"""User-wide credential mutation generations; never stores a secret or secret hash.

Import/construction do no I/O. Only guarded save/delete creates the explicit metadata
store. Legacy credential reads remain unchanged; proxy use requires a ready generation.
This tracks writes through Lingxi, not external changes made directly to OS Keyring.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading

from app.credential_refs import validate_reference

FORMAT = 'lingxi.credential-generations.v1'
MAX_BYTES = 1024 * 1024
MAX_REFS = 4096
MAX_INT = 2**53 - 1
_AUTHORITY = re.compile(r'^[0-9a-f]{64}$')


class CredentialGenerationError(RuntimeError):
    """Safe metadata failure, never includes Keyring errors or credential values."""


def _reference(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CredentialGenerationError('凭据引用格式无效')
    try:
        validate_reference(value)
    except (TypeError, ValueError):
        raise CredentialGenerationError('凭据引用不能是密钥正文') from None
    return value


def _checked(value):
    path = Path(value)
    raw = os.fspath(value)
    if (not path.is_absolute() or raw.startswith(('\\\\', '//')) or '..' in path.parts
            or len(raw) > 30000 or '\x00' in raw
            or any(':' in part or part.endswith((' ', '.'))
                   or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
                   for part in path.parts[1:])):
        raise CredentialGenerationError('凭据代际库必须是显式本地绝对路径')
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if (item.is_symlink() or item.is_junction()
                or item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400):
            raise CredentialGenerationError('凭据代际路径不允许链接或重解析点')
    return path


def _ordinary(path):
    result = path.lstat()
    if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
        raise CredentialGenerationError('凭据代际文件不允许特殊文件或硬链接')
    return result


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _encoded(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CredentialGenerationError('凭据代际JSON字段重复')
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise CredentialGenerationError('凭据代际JSON无效') from None
    return _validate(result)


def _validate(document):
    if (type(document) is not dict or set(document) != {'schema', 'authority', 'revision', 'refs'}
            or document['schema'] != FORMAT
            or not isinstance(document['authority'], str) or not _AUTHORITY.fullmatch(document['authority'])
            or type(document['revision']) is not int or not 1 <= document['revision'] <= MAX_INT
            or type(document['refs']) is not dict or len(document['refs']) > MAX_REFS):
        raise CredentialGenerationError('凭据代际库结构或版本无效')
    for reference, entry in document['refs'].items():
        _reference(reference)
        if (type(entry) is not dict or set(entry) != {'generation', 'status'}
                or type(entry['generation']) is not int or not 1 <= entry['generation'] <= MAX_INT
                or entry['status'] not in ('pending', 'ready', 'deleted')):
            raise CredentialGenerationError('凭据代际记录无效')
    return document


class CredentialGenerationStore:
    """One user-wide store, independent of DATA_DIR and excluded from user backups.

    The process lock spans pending publication, Keyring mutation and final publication.
    Any failed/uncertain mutation leaves pending and retires prior proxy approvals.
    Compensating writes are new mutations, never restoration of an old generation.
    """
    def __init__(self, path):
        self.path = Path(path)  # no stat/resolve/create at construction
        self._lock = threading.RLock()

    def _read(self):
        path = _checked(self.path)
        if not path.exists():
            return None, None
        before = _ordinary(path)
        if before.st_size > MAX_BYTES:
            raise CredentialGenerationError('凭据代际库超过容量限制')
        with path.open('rb') as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise CredentialGenerationError('读取凭据代际库时文件变化')
            raw = stream.read(MAX_BYTES + 1)
            opened_after = os.fstat(stream.fileno())
        after = _ordinary(_checked(path))
        if (len(raw) > MAX_BYTES or _identity(before) != _identity(after)
                or _identity(before) != _identity(opened_after)):
            raise CredentialGenerationError('读取凭据代际库时文件变化')
        return _decode(raw), (_identity(after), hashlib.sha256(raw).hexdigest())

    @contextmanager
    def _process_lock(self):
        path = _checked(self.path)
        if not _checked(path.parent).is_dir():
            raise CredentialGenerationError('凭据代际父目录不存在')
        lock = _checked(path.with_name(path.name + '.lock'))
        if lock.exists():
            _ordinary(lock)
        with lock.open('a+b') as stream:
            info = _ordinary(_checked(lock))
            if _identity(os.fstat(stream.fileno()))[:2] != _identity(info)[:2]:
                raise CredentialGenerationError('凭据代际锁文件变化')
            if stream.seek(0, 2) == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CredentialGenerationError('另一个凭据变更正在进行，请稍后重试') from None
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _publish(self, document, expected):
        _validate(document)
        raw = _encoded(document) + b'\n'
        if len(raw) > MAX_BYTES:
            raise CredentialGenerationError('凭据代际库超过容量限制')
        target = _checked(self.path)
        temporary = _checked(target.with_name('.' + target.name + '.' + secrets.token_hex(16) + '.tmp'))
        try:
            with temporary.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            _, current = self._read()
            if current != expected:
                raise CredentialGenerationError('凭据代际库已变化，拒绝覆盖')
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                _ordinary(_checked(temporary)); temporary.unlink()

    def snapshot_for_reference(self, reference):
        """Host-only metadata, no Key/Key hash. Unknown/legacy/pending must re-save."""
        reference = _reference(reference)
        with self._lock:
            document, _ = self._read()
            entry = document['refs'].get(reference) if document else None
            if entry is None:
                raise CredentialGenerationError('此凭据尚未登记安全代际，请明确重新保存后使用代理')
            if entry['status'] != 'ready':
                raise CredentialGenerationError('此凭据已删除或变更结果未确认，请明确重新保存')
            snapshot = {'authority': document['authority'], 'generation': entry['generation'],
                        'status': entry['status']}
            # Hash metadata only. No secret, secret digest or global ref roster is exposed.
            bound = {'reference': reference, **snapshot}
            snapshot['metadata_fingerprint'] = hashlib.sha256(_encoded(bound)).hexdigest()
            return snapshot

    def mutate(self, reference, action, writer):
        reference = _reference(reference)
        if action not in ('save', 'delete') or not callable(writer):
            raise CredentialGenerationError('凭据变更必须为save/delete和显式写入器')
        with self._lock, self._process_lock():
            document, observed = self._read()
            if document is None:
                document = {'schema': FORMAT, 'authority': secrets.token_hex(32), 'revision': 0, 'refs': {}}
            entry = document['refs'].get(reference)
            if entry is None and len(document['refs']) >= MAX_REFS:
                raise CredentialGenerationError('凭据引用数量超过安全上限')
            generation = entry['generation'] + 1 if entry else 1
            if generation > MAX_INT or document['revision'] > MAX_INT - 2:
                raise CredentialGenerationError('凭据代际计数超过安全上限')
            document['refs'][reference] = {'generation': generation, 'status': 'pending'}
            document['revision'] += 1
            self._publish(document, observed)  # must complete before touching Keyring
            current_pending, pending_observation = self._read()
            if current_pending != document:
                raise CredentialGenerationError('写入凭据前安全代际已变化，未调用系统凭据管理器')
            try:
                writer()  # Key remains solely in the caller's closure
            except Exception:
                raise CredentialGenerationError('系统凭据变更未确认，旧代理授权已失效；请重新保存') from None
            document['refs'][reference]['status'] = 'ready' if action == 'save' else 'deleted'
            document['revision'] += 1
            try:
                self._publish(document, pending_observation)
            except Exception:
                raise CredentialGenerationError('系统凭据可能已变更，但安全代际未确认；请重新保存') from None
            return {'authority': document['authority'], 'generation': generation,
                    'status': document['refs'][reference]['status']}


def default_store_path():
    """Resolve only the user-wide LOCALAPPDATA root, never fall back to DATA_DIR."""
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise CredentialGenerationError('缺少LOCALAPPDATA，无法建立用户级凭据安全代际')
    return _checked(_checked(base) / 'LingxiV1Security' / 'credential-generations.json')


def default_store():
    """Called only for explicit save/delete. No default file is created by loading keys."""
    path = default_store_path()
    parent = _checked(path.parent)
    if not parent.exists():
        if not _checked(parent.parent).is_dir():
            raise CredentialGenerationError('LOCALAPPDATA安全父目录不存在')
        try:
            parent.mkdir()  # create only the dedicated user-level metadata directory
        except FileExistsError:
            pass  # another guarded writer may have created the same dedicated parent
    if not _checked(parent).is_dir():
        raise CredentialGenerationError('凭据安全目录不可用')
    return CredentialGenerationStore(path)


def snapshot_for_reference(reference):
    """Read-only user-wide host snapshot; never creates a store or parent directory."""
    return CredentialGenerationStore(default_store_path()).snapshot_for_reference(reference)


# Short alias for host-side callers; neither form grants access to secret material.
GenerationStore = CredentialGenerationStore
