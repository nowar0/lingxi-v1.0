"""Host-owned opaque file/data objects. No import I/O or implicit user-data root.

Reads use held-object identity checks. Writes use a host-only prepare/explicit
commit contract and return an exact DescriptorCommitReceipt; the strict broker
compares its NEW descriptor rather than dropping the postcondition. write_adapter
still refuses without a trusted explicit commit authorizer and publication guard.

Handles/CAS and Windows ancestor no-delete sharing reduce replacement races; they
are not an OS sandbox or a guarantee against a malicious same-user filesystem
attacker. Parent ancestry outside approved_root is checked but not locked. Never
expose registration/prepare/commit methods to package RPC. A real IPC host must
attest TrustedOperation, enforce its deadline/cancellation and consume its grant.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import copy
from dataclasses import dataclass
from functools import wraps
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
from collections.abc import Mapping

from app.plugin_proxy import (BrokerSession, TrustedOperation, DescriptorCommitReceipt,
                              PERMISSIONS, encode_json)

MAX_OBJECT_BYTES = 16 * 1024**2
MAX_PENDING = 128
MAX_OBJECTS = 128
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_PLUGIN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_PROTECTED = {'appdata', 'credentials', 'vault', 'windows', 'system32'}


class FileProxyError(ValueError):
    """Codes are deliberately generic: no host paths/content/exception text."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise FileProxyError(code)


def _safe_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except FileProxyError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise FileProxyError('object_operation_failed') from None
    return wrapped


def _hash(value):
    return hashlib.sha256(encode_json(value)).hexdigest()


def _plain(value):
    if isinstance(value, Mapping):
        return {k:_plain(v) for k,v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _checked(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or '\x00' in raw or raw.startswith(('\\\\', '//')):
        _fail('object_path_invalid')
    p = Path(raw)
    if not p.is_absolute() or '..' in p.parts:
        _fail('object_path_invalid')
    p = Path(os.path.abspath(p))
    for item in (p, *p.parents):
        if item.is_symlink() or item.is_junction():
            _fail('object_path_link')
        if item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
            _fail('object_path_link')
    return p


def _security_fingerprint(path):
    """Read only: do not let atomic publication silently replace ACL/MIC policy."""
    if os.name != 'nt':
        info = path.lstat()
        return _hash({'mode':stat.S_IMODE(info.st_mode), 'uid':info.st_uid, 'gid':info.st_gid})
    import ctypes
    from ctypes import wintypes as W
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = W.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, W.DWORD,
        W.DWORD, ctypes.POINTER(W.LPWSTR), ctypes.POINTER(W.DWORD)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = W.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p(); text = W.LPWSTR(); size = W.DWORD()
    flags = 0x1 | 0x2 | 0x4 | 0x10  # owner/group/DACL/mandatory integrity label only
    error = adv.GetNamedSecurityInfoW(str(path), 1, flags, None, None, None, None, ctypes.byref(descriptor))
    if error:
        _fail('object_security_unverified')
    try:
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, flags, ctypes.byref(text), ctypes.byref(size)):
            _fail('object_security_unverified')
        try:
            return hashlib.sha256((text.value or '').encode('utf-8')).hexdigest()
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(descriptor)


def _file_info(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail('object_not_ordinary')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _directory_info(path):
    value = _checked(path).lstat()
    if not stat.S_ISDIR(value.st_mode):
        _fail('object_parent_invalid')
    return (value.st_dev, value.st_ino)


def _windows_open(path, directory=False):
    """Open exact object with OPEN_REPARSE_POINT; no DELETE sharing for ancestry."""
    import ctypes
    from ctypes import wintypes as W
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
                                  W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    access = 0x80 if directory else 0x80000000
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
    # Directories allow ordinary file activity, but no rename/delete of that ancestor.
    share = 0x3 if directory else 0x1
    handle = kernel.CreateFileW(str(path), access, share, None, 3, flags, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        _fail('object_open_failed')
    if directory:
        return handle, kernel
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return fd, None  # fd owns native handle


@contextmanager
def _parents_locked(root, path):
    """Lock approved ancestry only; never changes ACL/MIC or enumerates contents."""
    path = _checked(path)
    if path == root or not path.is_relative_to(root):
        _fail('object_outside_approved_root')
    parents = [root]
    rel_parent = path.parent.relative_to(root)
    current = root
    for part in rel_parent.parts:
        current = current / part
        parents.append(current)
    identities = tuple((str(p), _directory_info(p)) for p in parents)
    handles = []
    try:
        for parent in parents:
            if os.name == 'nt':
                handle, kernel = _windows_open(parent, directory=True)
                handles.append((handle, kernel))
            else:
                flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                handles.append((os.open(parent, flags), None))
        if identities != tuple((str(p), _directory_info(p)) for p in parents):
            _fail('object_parent_changed')
        yield identities
        if identities != tuple((str(p), _directory_info(p)) for p in parents):
            _fail('object_parent_changed')
    finally:
        for handle, kernel in reversed(handles):
            if kernel is not None:
                kernel.CloseHandle(handle)
            else:
                os.close(handle)


@contextmanager
def _opened(root, path, *, check=None):
    with _parents_locked(root, path) as parents:
        before = _file_info(_checked(path).lstat())
        if before[2] > MAX_OBJECT_BYTES:
            _fail('object_capacity')
        if os.name == 'nt':
            fd, _ = _windows_open(path)
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            with os.fdopen(fd, 'rb', closefd=True) as stream:
                fd = None
                opened = _file_info(os.fstat(stream.fileno()))
                if before != opened:
                    _fail('object_changed')
                chunks = []; total = 0
                while total <= MAX_OBJECT_BYTES:
                    if check is not None: check()
                    chunk = stream.read(min(65536, MAX_OBJECT_BYTES + 1 - total))
                    if not chunk: break
                    chunks.append(chunk); total += len(chunk)
                data = b''.join(chunks)
                if check is not None: check()
                after = _file_info(os.fstat(stream.fileno()))
                if len(data) > MAX_OBJECT_BYTES or opened != after or _file_info(path.lstat()) != after:
                    _fail('object_changed')
                yield stream, data, opened, parents
                if opened != _file_info(os.fstat(stream.fileno())) or _file_info(_checked(path).lstat()) != opened:
                    _fail('object_changed')
        finally:
            if fd is not None:
                os.close(fd)


@dataclass(frozen=True)
class PreparedWrite:
    """Host-only receipt; stage is not published or a successful package write."""
    token: str
    fingerprint: str
    snapshot_json: str
    expires_in: float

    @property
    def snapshot(self):
        return json.loads(self.snapshot_json)


@dataclass
class _Object:
    path: Path
    descriptor: dict
    valid: bool = True


class FileObjectRegistry:
    def __init__(self, approved_root, *, live_authority_provider, data_roots=None,
                 clock=time.monotonic, prepared_ttl=60):
        if not callable(live_authority_provider):
            raise TypeError('explicit live_authority_provider required')
        if type(prepared_ttl) not in (int, float) or not math.isfinite(prepared_ttl) or not 0 < prepared_ttl <= 120:
            raise ValueError('prepared TTL invalid')
        # Constructor only stores paths. Explicit register/operations perform I/O.
        self.root = Path(approved_root)
        self._data_roots = dict(data_roots or {})
        self._live_provider, self._clock, self._ttl = live_authority_provider, clock, prepared_ttl
        self._objects, self._pending = {}, {}
        self._stages, self._committed = {}, {}
        self._lock = threading.RLock()

    def _root(self):
        root = _checked(self.root)
        if any(part.casefold() in _PROTECTED for part in root.parts):
            _fail('approved_root_protected')
        _directory_info(root)
        return root

    def _live(self, binding, deadline=None):
        if type(binding) is not BrokerSession:
            _fail('authority_invalid')
        try:
            current = self._live_provider(binding)
        except Exception:
            current = None
        if type(current) is not BrokerSession or current != binding:
            _fail('authority_changed')
        if deadline is not None and (type(deadline) not in (int, float)
                or not math.isfinite(deadline) or self._clock() >= deadline):
            _fail('operation_deadline')

    def _observe(self, entry, *, check=None):
        if not entry.valid:
            _fail('object_revoked')
        if check is not None: check()
        with _opened(self._root(), entry.path, check=check) as (_, data, identity, parents):
            fingerprint = _hash({'file':[str(v) for v in identity],
                                 'parents':[[p, [str(v) for v in i]] for p,i in parents],
                                 'security_sha256':_security_fingerprint(entry.path)})
            descriptor = dict(entry.descriptor)
            descriptor['identity_fingerprint'] = fingerprint
            descriptor['content_sha256'] = hashlib.sha256(data).hexdigest()
            return descriptor

    @_safe_errors
    def register(self, path, *, kind, plugin_id, permissions, root_scope,
                 max_read_bytes=12000, max_write_bytes=12000, max_extent_bytes=None, object_id=None):
        """Trusted host only. Existing ordinary files only; no arbitrary new filename."""
        if kind not in ('file', 'data') or not isinstance(plugin_id, str) or not _PLUGIN.fullmatch(plugin_id):
            _fail('object_scope_invalid')
        if not isinstance(root_scope, str) or not _ID.fullmatch(root_scope):
            _fail('object_scope_invalid')
        if (type(permissions) not in (tuple, list) or not permissions
                or len(set(permissions)) != len(permissions) or set(permissions) - {'read', 'write'}):
            _fail('object_permissions_invalid')
        read = max_read_bytes if 'read' in permissions else 0
        write = max_write_bytes if 'write' in permissions else 0
        if any(type(v) is not int or not 0 <= v <= 12000 for v in (read, write)) or ('read' in permissions and not read) or ('write' in permissions and not write):
            _fail('object_budget_invalid')
        with self._lock:
            if len(self._objects) >= MAX_OBJECTS:
                _fail('object_registry_capacity')
            root, target = self._root(), _checked(path)
            if target == root or not target.is_relative_to(root):
                _fail('object_outside_approved_root')
            if kind == 'data':
                data_root = _checked(self._data_roots.get(plugin_id, ''))
                if data_root == root or not data_root.is_relative_to(root) or target == data_root or not target.is_relative_to(data_root):
                    _fail('object_data_root_invalid')
                _directory_info(data_root)
            with _opened(root, target) as (_, data, _, _):
                extent = len(data) if max_extent_bytes is None else max_extent_bytes
                if type(extent) is not int or not len(data) <= extent <= MAX_OBJECT_BYTES:
                    _fail('object_extent_invalid')
            object_id = 'object_' + secrets.token_hex(16) if object_id is None else object_id
            if type(object_id) is not str or not _ID.fullmatch(object_id) or object_id in self._objects:
                _fail('object_id_invalid_or_reused')
            descriptor = {'object_id':object_id, 'kind':kind, 'plugin_id':plugin_id,
                          'root_scope':root_scope, 'permissions':sorted(permissions),
                          'max_extent_bytes':extent, 'max_read_bytes':read, 'max_write_bytes':write}
            entry = _Object(target, descriptor)
            descriptor = self._observe(entry)
            entry.descriptor = descriptor
            self._objects[object_id] = entry
            return copy.deepcopy(descriptor)

    def unregister(self, object_id):
        with self._lock:
            entry = self._objects.pop(object_id, None)
            if entry is not None:
                entry.valid = False
            # Pending writes retain their entry pointer but cannot publish revoked objects.

    def close(self):
        """Retire capabilities; retain owned stages for explicit review, never publish."""
        with self._lock:
            for entry in self._objects.values():
                entry.valid = False
            self._objects.clear()
            self._pending.clear()

    def resource_status(self):
        """Host-only retained ownership summary; no paths/content/raw authority."""
        with self._lock:
            return {'prepared_tokens':len(self._pending), 'retained_stages':len(self._stages),
                    'stages':[{'stage':item['stage'], 'published':item['published'],
                               'identity_observed':item['identity'] is not None}
                              for item in self._stages.values()],
                    'published_writes':len(self._committed),
                    'unconfirmed_publications':sum(item['new_descriptor'] is None for item in self._committed.values()),
                    'effects_undone':False}

    def _entry(self, binding, kind, object_id):
        self._live(binding)
        entry = self._objects.get(object_id)
        if entry is None or not entry.valid:
            _fail('object_not_registered')
        if entry.descriptor['kind'] != kind or entry.descriptor['plugin_id'] != binding.plugin_id:
            _fail('object_scope_invalid')
        return entry

    @_safe_errors
    def object_descriptor_provider(self, binding, kind, object_id, *, deadline=None):
        with self._lock:
            entry = self._entry(binding, kind, object_id)
            observed = self._observe(entry, check=lambda:self._live(binding, deadline))
            if observed != entry.descriptor:
                entry.valid = False
                _fail('object_changed')
            self._live(binding)
            return copy.deepcopy(observed)

    def _operation(self, operation, write):
        if type(operation) is not TrustedOperation:
            _fail('trusted_operation_required')
        allowed = ('file.write', 'data.write') if write else ('file.read', 'data.read')
        if operation.op not in allowed or operation.permission != PERMISSIONS[operation.op]:
            _fail('object_operation_invalid')
        self._live(operation.binding, operation.deadline)
        params, descriptor = _plain(operation.params), _plain(operation.object_descriptor)
        if type(params) is not dict or type(descriptor) is not dict or _hash(params) != operation.params_sha256 or _hash(descriptor) != operation.object_descriptor_sha256:
            _fail('object_fingerprint_invalid')
        expected = {'object_id', 'offset', 'content_base64', 'content_sha256'} if write else {'object_id', 'offset', 'length'}
        if set(params) != expected or type(params['offset']) is not int or params['offset'] < 0:
            _fail('object_params_invalid')
        entry = self._entry(operation.binding, operation.op.split('.')[0], params['object_id'])
        if descriptor != entry.descriptor or self._observe(entry,
                check=lambda: self._live(operation.binding, operation.deadline)) != descriptor:
            _fail('object_changed')
        mode = 'write' if write else 'read'
        if mode not in descriptor['permissions']:
            _fail('object_permission_denied')
        if write:
            try:
                content = base64.b64decode(params['content_base64'], validate=True)
            except (TypeError, ValueError):
                _fail('object_content_invalid')
            if base64.b64encode(content).decode('ascii') != params['content_base64'] or hashlib.sha256(content).hexdigest() != params['content_sha256']:
                _fail('object_content_invalid')
            length = len(content)
        else:
            length = params['length']
            if type(length) is not int or length < 1:
                _fail('object_params_invalid')
            content = None
        if length > descriptor['max_' + mode + '_bytes'] or params['offset'] + length > descriptor['max_extent_bytes']:
            _fail('object_range_denied')
        return entry, params, descriptor, content

    def read_adapter(self, operation):
        try:
            with self._lock:
                entry, params, descriptor, _ = self._operation(operation, False)
                check = lambda: self._live(operation.binding, operation.deadline)
                with _opened(self._root(), entry.path, check=check) as (_, data, _, _):
                    if hashlib.sha256(data).hexdigest() != descriptor['content_sha256']:
                        _fail('object_changed')
                    value = data[params['offset']:params['offset'] + params['length']]
                    self._live(operation.binding, operation.deadline)
                if self._observe(entry, check=check) != descriptor:
                    _fail('object_changed')
                self._live(operation.binding, operation.deadline)
                return {'object_id':params['object_id'], 'offset':params['offset'], 'bytes':len(value),
                        'content_base64':base64.b64encode(value).decode('ascii'),
                        'content_sha256':hashlib.sha256(value).hexdigest()}
        except FileProxyError:
            raise
        except (OSError, ValueError):
            raise FileProxyError('object_read_failed') from None

    @_safe_errors
    def write_adapter(self, operation, *, commit_authorizer=None, publication_guard=None):
        """Publish only an exact already-approved operation, never a default True.

        commit_authorizer(operation, prepared)->True checks the consumed host intent;
        publication_guard(operation, None) must hold manager generation authority
        and persist write/effect epochs + dirty BEFORE yielding to stage/publication.
        """
        if not callable(commit_authorizer) or not callable(publication_guard):
            _fail('write_requires_host_commit')
        # Staging itself changes the data directory. Reserve/persist epoch+dirty
        # BEFORE creating that stage, not just before the final atomic replace.
        with publication_guard(operation, None):
            prepared = self.prepare_write(operation)
            if commit_authorizer(operation, prepared) is not True:
                _fail('prepared_confirmation_required')
            result = self.commit_prepared(prepared.token, prepared.fingerprint, user_confirmed=True)
        return DescriptorCommitReceipt(operation.binding, operation.op, operation.params_sha256,
            operation.object_descriptor_sha256, encode_json(result['new_descriptor'], safe=True),
            encode_json({'committed':True, 'object_id':result['object_id'],
                         'bytes':len(base64.b64decode(operation.params['content_base64'], validate=True)),
                         'content_sha256':result['new_descriptor']['content_sha256']}, safe=True))

    @_safe_errors
    def prepare_write(self, operation):
        """Host only: prepare bounded patch in same parent, without publishing it."""
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                _fail('prepared_capacity')
            entry, params, descriptor, patch = self._operation(operation, True)
            root = self._root()
            stage = None
            try:
                check = lambda: self._live(operation.binding, operation.deadline)
                with _opened(root, entry.path, check=check) as (_, old, _, parents):
                    if params['offset'] > len(old):
                        _fail('object_sparse_write_denied')
                    updated = old[:params['offset']] + patch + old[params['offset'] + len(patch):]
                    if len(updated) > descriptor['max_extent_bytes']:
                        _fail('object_range_denied')
                    if len(self._stages) >= MAX_PENDING: _fail('prepared_stage_capacity')
                    stage = entry.path.with_name('.lingxi-proxy-stage-' + secrets.token_hex(16))
                    self._stages[str(stage)] = {'path':stage, 'identity':None, 'binding':operation.binding,
                                               'published':False, 'stage':'creating'}
                    with stage.open('xb') as output:
                        for offset in range(0, len(updated), 65536):
                            check(); output.write(updated[offset:offset+65536])
                        check(); output.flush(); os.fsync(output.fileno())
                    stage_identity = _file_info(stage.lstat())
                    self._stages[str(stage)].update(identity=stage_identity, stage='prepared')
                    self._live(operation.binding, operation.deadline)
                    if self._observe(entry, check=check) != descriptor:
                        _fail('object_changed')
                snapshot = {'object_id':params['object_id'], 'kind':descriptor['kind'],
                            'plugin_id':operation.binding.plugin_id, 'root_scope':descriptor['root_scope'],
                            'old_descriptor_sha256':_hash(descriptor), 'params_sha256':operation.params_sha256,
                            'new_content_sha256':hashlib.sha256(updated).hexdigest(), 'new_size':len(updated),
                            'notice':'Prepared only; exact host commit and new-descriptor broker CAS required'}
                token = secrets.token_urlsafe(32)
                expires = min(operation.deadline, self._clock() + self._ttl)
                fingerprint = _hash(snapshot)
                self._pending[token] = (entry, descriptor, operation.binding, operation.deadline,
                                        stage, stage_identity, parents, snapshot, fingerprint, expires)
                return PreparedWrite(token, fingerprint, encode_json(snapshot).decode('utf-8'), max(0, expires-self._clock()))
            except BaseException:
                # Preserve an uncertain partial stage rather than deleting a path
                # whose identity may have changed. No publish occurred.
                raise

    def commit_prepared(self, token, fingerprint, *, user_confirmed):
        """Host-only one-attempt CAS publish; retain effects even if postchecks fail."""
        with self._lock:
            pending = self._pending.pop(token, None)
            if pending is None:
                _fail('prepared_invalid')
            entry, descriptor, binding, deadline, stage, stage_identity, parents, snapshot, expected, expires = pending
            try:
                if user_confirmed is not True or fingerprint != expected:
                    _fail('prepared_confirmation_required')
                check = lambda: self._live(binding, min(deadline, expires))
                check()
                if not entry.valid or entry.descriptor != descriptor or self._observe(entry, check=check) != descriptor:
                    _fail('object_changed')
                with _parents_locked(self._root(), entry.path) as now_parents:
                    if now_parents != parents:
                        _fail('object_parent_changed')
                    with _opened(self._root(), stage, check=check) as (_, content, observed_stage, _):
                        if observed_stage != stage_identity or hashlib.sha256(content).hexdigest() != snapshot['new_content_sha256']:
                            _fail('prepared_stage_changed')
                    # Original read handle is closed before Windows atomic replace;
                    # exact original CAS is rechecked immediately at this boundary.
                    if self._observe(entry, check=check) != descriptor:
                        _fail('object_changed')
                    self._live(binding, min(deadline, expires))
                    if _security_fingerprint(stage) != _security_fingerprint(entry.path):
                        _fail('write_security_policy_mismatch')
                    check()
                    ownership = self._stages[str(stage)]
                    ownership['stage'] = 'publish_attempted'
                    os.replace(stage, entry.path)
                    ownership.update(published=True, stage='published')
                    # Record the actual publication before cancellation/trust checks.
                    # Never erase this owner/effect or restore the old descriptor.
                    self._committed[token] = {'binding':binding, 'object_id':descriptor['object_id'],
                        'old_descriptor_sha256':_hash(descriptor), 'params_sha256':snapshot['params_sha256'],
                        'content_sha256':snapshot['new_content_sha256'], 'new_descriptor':None}
                    new_descriptor = self._observe(entry, check=check)
                    if new_descriptor['content_sha256'] != snapshot['new_content_sha256']:
                        entry.valid = False
                        _fail('write_outcome_unconfirmed')
                    entry.descriptor = new_descriptor
                    self._committed[token]['new_descriptor'] = copy.deepcopy(new_descriptor)
                    self._stages.pop(str(stage), None)
                    self._live(binding, min(deadline, expires))
                return {'committed':True, 'object_id':descriptor['object_id'],
                        'new_descriptor':copy.deepcopy(new_descriptor), 'requires_new_session':False,
                        'old_descriptor_grants_invalidated':True}
            except FileProxyError:
                raise
            except (OSError, ValueError):
                raise FileProxyError('object_commit_failed') from None
            finally:
                if stage.exists():
                    try:
                        if _file_info(_checked(stage).lstat()) == stage_identity:
                            stage.unlink()
                            self._stages.pop(str(stage), None)
                    except (OSError, FileProxyError):
                        pass
