"""Explicit user-controlled Ed25519 public trust roots; no import I/O or private keys.

Signature payload: b'LingxiPluginManifest-v1\\x00' + UTF-8 canonical_json of the
whole manifest with only its top-level signature removed. Actual package bytes
still require the existing scanner's independent files/hash validation.
This module neither approves plugin execution nor invalidates manager approvals;
the host must bind trust revision/fingerprint and recheck them at execution.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.plugin_manifest import canonical_json

FORMAT = 'lingxi.plugin-trust.v1'
DOMAIN = b'LingxiPluginManifest-v1\x00'
MAX_BYTES = 131072
MAX_KEYS = 128
MAX_PENDING = 128
KEY_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')


class TrustError(ValueError):
    """A trust operation was malformed, stale or not explicitly confirmed."""


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _key_id(value):
    if not isinstance(value, str) or not KEY_ID.fullmatch(value):
        raise TrustError('key_id 必须为1..100位ASCII标识')
    return value


def _base64(value, length, label):
    if not isinstance(value, str) or len(value) != 4 * ((length + 2) // 3):
        raise TrustError(label + ' 长度无效')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise TrustError(label + ' 必须是严格base64') from exc
    if len(raw) != length or base64.b64encode(raw).decode('ascii') != value:
        raise TrustError(label + ' 编码不是规范值')
    return raw


def _public_key(value):
    return _base64(value, 32, 'Ed25519公钥')


def _display(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise TrustError('display_name 必须为有限单行名称')
    return value


def _checked(path):
    value = Path(path)
    if '..' in value.parts or str(value).startswith(('\\\\', '//')):
        raise TrustError('信任库拒绝父级跳转、网络或设备路径')
    if not value.is_absolute():
        raise TrustError('信任库必须是显式绝对路径')
    value = Path(os.path.abspath(value))
    for part in (value, *value.parents):
        if (part.is_symlink() or part.is_junction()
                or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise TrustError('信任库路径不允许链接或重解析点')
    return value


def _ordinary(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TrustError('信任库/锁不允许特殊文件或硬链接')
    return info


def _identity(info):
    # Windows path stat reports creation time while fstat can report change time
    # after rename; ctime is not a cross-API identity. File ID+mtime+bytes bind CAS.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TrustError('信任库JSON字段重复')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(TrustError('非有限JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TrustError('信任库JSON无效') from exc


def _validate(document):
    if not isinstance(document, dict) or set(document) != {'schema', 'revision', 'keys'}:
        raise TrustError('未知信任库结构')
    if document['schema'] != FORMAT or type(document['revision']) is not int or not 1 <= document['revision'] <= 2**53-1:
        raise TrustError('信任库schema/revision无效')
    if not isinstance(document['keys'], list) or len(document['keys']) > MAX_KEYS:
        raise TrustError('信任公钥数量超限')
    seen = set()
    public_fingerprints = set()
    for key in document['keys']:
        if not isinstance(key, dict) or set(key) != {'key_id', 'public_key', 'display_name', 'fingerprint', 'status'}:
            raise TrustError('未知公钥记录字段')
        ident = _key_id(key['key_id'])
        if ident in seen:
            raise TrustError('重复key_id')
        seen.add(ident)
        raw = _public_key(key['public_key']); _display(key['display_name'])
        public_fingerprint = hashlib.sha256(raw).hexdigest()
        if public_fingerprint in public_fingerprints:
            raise TrustError('同一公钥不得以多个发布者ID登记；防止撤销被别名绕过')
        public_fingerprints.add(public_fingerprint)
        if key['fingerprint'] != public_fingerprint or key['status'] not in ('trusted', 'revoked'):
            raise TrustError('公钥指纹或信任状态无效')
    return document


def signature_payload(manifest):
    """Canonical domain-separated unsigned manifest bytes; never mutates input."""
    if not isinstance(manifest, dict):
        raise TrustError('manifest必须为对象')
    unsigned = copy.deepcopy(manifest)
    unsigned.pop('signature', None)
    try:
        value = DOMAIN + canonical_json(unsigned).encode('utf-8')
    except (ValueError, TypeError, RecursionError) as exc:
        raise TrustError('manifest必须为有限JSON') from exc
    if len(value) > 2 * 1024**2:
        raise TrustError('签名manifest超过容量限制')
    return value


class TrustStore:
    """No constructor I/O; absent storage means no trusted keys, never default trust.

    Preview creates only a bounded in-memory intent. commit consumes its token on
    every attempt, requires the exact fingerprint, and compares bytes+file identity
    under a process lock. A new TrustStore process cannot resume old approvals.
    JSON contains public keys only; loading an edited file is NOT authentication.
    User-control/host same-user filesystem protection remains the trust boundary.
    """
    def __init__(self, path, *, clock=time.monotonic, ttl=300):
        # Deliberately do not resolve/stat/create at construction time.
        self.path = Path(path)
        if type(ttl) not in (int, float) or not 1 <= ttl <= 600:
            raise TrustError('信任审批TTL无效')
        self.clock, self.ttl = clock, ttl
        self._lock = threading.RLock()
        self._pending = {}

    def _read(self):
        path = _checked(self.path)
        if not path.exists():
            document = {'schema': FORMAT, 'revision': 0, 'keys': []}
            return document, {'exists': False, 'sha256': None, 'identity': None}
        before = _ordinary(path)
        if before.st_size > MAX_BYTES:
            raise TrustError('信任库超过容量限制')
        with path.open('rb') as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise TrustError('读取信任库时发生变化')
            raw = stream.read(MAX_BYTES + 1)
            opened_after = os.fstat(stream.fileno())
        after = _ordinary(_checked(path))
        if len(raw) > MAX_BYTES or _identity(before) != _identity(after) or _identity(before) != _identity(opened_after):
            raise TrustError('读取信任库时发生变化')
        return _validate(_json(raw)), {'exists': True, 'sha256': hashlib.sha256(raw).hexdigest(), 'identity': list(_identity(after))}

    def snapshot(self):
        with self._lock:
            document, observed = self._read()
            return {'schema': FORMAT, 'revision': document['revision'], 'fingerprint': _digest({'document':document,'observation':observed}),
                    'keys': copy.deepcopy(document['keys']), 'exists': observed['exists']}

    def _preview(self, action, key_id, expected_revision, public_key=None, display_name=None):
        with self._lock:
            now = self.clock()
            self._pending = {k:v for k,v in self._pending.items() if v['expires'] > now}
            if len(self._pending) >= MAX_PENDING:
                raise TrustError('信任审批过多')
            if type(expected_revision) is not int or expected_revision < 0:
                raise TrustError('expected_revision必须为非负整数')
            key_id = _key_id(key_id)
            document, observed = self._read()
            if expected_revision != document['revision']:
                raise TrustError('信任库修订已改变，请重新预览')
            existing = next((x for x in document['keys'] if x['key_id'] == key_id), None)
            if action == 'add':
                raw = _public_key(public_key); _display(display_name)
                if existing:
                    raise TrustError('key_id已存在；不允许覆盖或替换公钥，撤销后恢复必须单独preview_trust')
                public_fingerprint = hashlib.sha256(raw).hexdigest()
                if any(item['fingerprint'] == public_fingerprint for item in document['keys']):
                    raise TrustError('同一公钥已登记，不能通过新发布者ID绕过撤销；请使用原记录单独恢复信任')
                if len(document['keys']) >= MAX_KEYS:
                    raise TrustError('信任公钥数量超限')
                key = {'key_id':key_id,'public_key':public_key,'display_name':display_name,
                       'fingerprint':hashlib.sha256(raw).hexdigest(),'status':'trusted'}
            else:
                if not existing:
                    raise TrustError('不存在的key_id')
                required = 'trusted' if action == 'revoke' else 'revoked'
                if existing['status'] != required:
                    raise TrustError('公钥当前状态不允许该操作')
                key = copy.deepcopy(existing)
                key['status'] = 'revoked' if action == 'revoke' else 'trusted'
            intent = {'action':action,'expected_revision':expected_revision,'key':key,
                      'trust_fingerprint':_digest({'document':document,'observation':observed}),
                      'notice':'仅信任发布者公钥；不是执行插件或改变权限的批准'}
            fingerprint = _digest(intent)
            token = secrets.token_urlsafe(32)
            self._pending[token] = {'intent':copy.deepcopy(intent),'fingerprint':fingerprint,
                                    'observation':observed,'expires':now + self.ttl}
            return {'snapshot':copy.deepcopy(intent),'token':token,'fingerprint':fingerprint,
                    'expires_in':self.ttl,'approval_required':True}

    def preview_add(self, key_id, public_key, display_name, expected_revision):
        return self._preview('add', key_id, expected_revision, public_key, display_name)

    def preview_revoke(self, key_id, expected_revision):
        return self._preview('revoke', key_id, expected_revision)

    def preview_trust(self, key_id, expected_revision):
        """Explicit revive of the SAME public key, never implicit on load/add."""
        return self._preview('trust', key_id, expected_revision)

    @contextmanager
    def _process_lock(self):
        path = _checked(self.path)
        if not _checked(path.parent).is_dir():
            raise TrustError('信任库父目录必须已存在')
        lock = _checked(path.with_name(path.name + '.lock'))
        if lock.exists():
            _ordinary(lock)
        with lock.open('a+b') as stream:
            _ordinary(lock)
            if _identity(os.fstat(stream.fileno()))[:2] != _identity(lock.stat())[:2]:
                raise TrustError('信任库锁文件变化')
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
            except OSError as exc:
                raise TrustError('另一个信任管理操作正在进行') from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def commit(self, token, fingerprint):
        with self._lock:
            approval = self._pending.pop(token, None) if isinstance(token, str) else None
            if (not approval or not isinstance(fingerprint, str)
                    or not secrets.compare_digest(approval['fingerprint'], fingerprint)
                    or approval['expires'] <= self.clock()):
                raise TrustError('信任批准无效/过期/已消费；请重新预览')
            with self._process_lock():
                document, observed = self._read()
                intent = approval['intent']
                if (document['revision'] != intent['expected_revision'] or observed != approval['observation']
                        or _digest({'document':document,'observation':observed}) != intent['trust_fingerprint']):
                    raise TrustError('信任库已被其他进程/文件修改；拒绝旧批准')
                updated = copy.deepcopy(document)
                key = intent['key']
                if intent['action'] == 'add':
                    updated['keys'].append(key)
                else:
                    for index, existing in enumerate(updated['keys']):
                        if existing['key_id'] == key['key_id']:
                            updated['keys'][index] = key
                            break
                    else:
                        raise TrustError('信任公钥已不存在')
                updated['keys'].sort(key=lambda item:item['key_id'])
                updated['revision'] += 1
                _validate(updated)
                raw = canonical_json(updated).encode('utf-8') + b'\n'
                if len(raw) > MAX_BYTES:
                    raise TrustError('信任库超过容量限制')
                target = _checked(self.path)
                temporary = _checked(target.with_name('.' + target.name + '.' + secrets.token_hex(16) + '.tmp'))
                try:
                    with temporary.open('xb') as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    # Recheck immediately before publishing. This CAS does not claim
                    # protection from a hostile same-user arbitrary-filesystem attacker.
                    _, current = self._read()
                    if current != observed:
                        raise TrustError('提交期间信任库发生变化')
                    os.replace(temporary, target)
                finally:
                    if temporary.exists():
                        _ordinary(_checked(temporary)); temporary.unlink()
                return self.snapshot()

    def verify(self, manifest):
        """Five-state signature result plus current trust revision/CAS fingerprint.

        Unknown publishers remain present_unverified, not cryptographically invalid.
        Revoked means a valid signature by an explicitly revoked stored public key.
        Missing/invalid registry raises TrustError: fail closed, don't reset roots.
        """
        snapshot = self.snapshot()
        result = {'status':'absent','key_id':None,'key_fingerprint':None,
                  'trust_revision':snapshot['revision'],'trust_fingerprint':snapshot['fingerprint']}
        if not isinstance(manifest, dict):
            return {**result,'status':'invalid'}
        if 'signature' not in manifest or manifest['signature'] is None:
            return result
        try:
            signature = manifest['signature']
            if not isinstance(signature, dict) or set(signature) != {'algorithm','key_id','signature'} or signature['algorithm'] != 'ed25519':
                raise TrustError('signature结构无效')
            key_id = _key_id(signature['key_id'])
            raw_signature = _base64(signature['signature'],64,'Ed25519签名')
            payload = signature_payload(manifest)
            result['key_id'] = key_id
            key = next((x for x in snapshot['keys'] if x['key_id'] == key_id),None)
            if key is None:
                return {**result,'status':'present_unverified'}
            result['key_fingerprint'] = key['fingerprint']
            Ed25519PublicKey.from_public_bytes(_public_key(key['public_key'])).verify(raw_signature,payload)
            return {**result,'status':'verified_trusted' if key['status'] == 'trusted' else 'revoked'}
        except (TrustError, InvalidSignature, ValueError, TypeError, KeyError, RecursionError):
            return {**result,'status':'invalid'}

    def verifier(self, manifest):
        """Legacy bool callback; host must use verify() to preserve unknown/revoked states."""
        return self.verify(manifest)['status'] == 'verified_trusted'
