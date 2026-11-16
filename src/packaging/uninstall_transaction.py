"""Explicit offline program uninstall; retains user data, previous and unknown files.

Never runs/kills an application, reads default APPDATA data, changes ACL/MIC, or
creates backups. Only a human-confirmed manifest/hash plan may unlink tracked
ordinary files. Uninstall is NOT reversible: reinstall a separately trusted package.
Use the SAME explicit state_dir as Installer for their shared .lease mutex.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

import install_transaction as install
import release_tools as release

FORMAT = 'lingxi.program-uninstall.v1'
TTL = 300
MAX_ENTRIES = 100000


class UninstallError(install.InstallError):
    pass


@contextmanager
def _held_event_stream(path):
    """Own the actual append/read handle, not a separate lock-file/cache token."""
    install.checked(path.parent)
    if path.exists():
        install._ordinary_file(install.checked(path))
    if os.name == 'nt':
        import msvcrt
        from ctypes import wintypes as W
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
                                      W.DWORD, W.DWORD, W.HANDLE]
        kernel.CreateFileW.restype = W.HANDLE
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.CloseHandle.restype = W.BOOL
        # GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ only, OPEN_ALWAYS,
        # FILE_FLAG_OPEN_REPARSE_POINT. Existing foreign write/delete handles
        # also prevent this acquisition: no cooperative .lease is assumed here.
        handle = kernel.CreateFileW(str(path), 0xC0000000, 1, None, 4, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        try:
            stream = os.fdopen(descriptor, 'r+b', buffering=0)
        except BaseException:
            os.close(descriptor)  # open_osfhandle transferred HANDLE ownership.
            raise
        with stream:
            yield stream, True
    else:
        import fcntl
        try:
            stream = path.open('x+b', buffering=0)
        except FileExistsError:
            stream = path.open('r+b', buffering=0)
        with stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UninstallError('已有事务持有事件日志锁') from exc
            try:
                # flock is advisory, NOT Windows deny-write/delete protection.
                # Non-cooperating writers remain outside its concurrency bound;
                # on this platform checks retain full-history verification.
                yield stream, False
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


class _HeldEventAppendSession:
    """Transaction-local verified records; never a reusable global log cache."""
    def __init__(self, path, stream, deny_foreign_writes):
        self.path, self.stream = path, stream
        self.deny_foreign_writes = deny_foreign_writes
        self.failed = False
        self.stable_id, self.length = self._observed()
        self.records, data = self._read_full_chain()
        self.content_hash = hashlib.sha256(data)
        self.last_hash = install.fingerprint(self.records[-1]) if self.records else None
        self.tail = data.splitlines(keepends=True)[-1] if data else b''
        self.check()

    def _observed(self):
        handle_stat = os.fstat(self.stream.fileno())
        path_stat = install._ordinary_file(install.checked(self.path))
        if (not stat.S_ISREG(handle_stat.st_mode) or handle_stat.st_nlink != 1
                or (handle_stat.st_dev, handle_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or handle_stat.st_size != path_stat.st_size):
            raise UninstallError('事务日志句柄/路径身份改变，停止')
        return (handle_stat.st_dev, handle_stat.st_ino), handle_stat.st_size

    def _read_full_chain(self):
        """Read through the held handle; validate the same seq/hash contract as Installer."""
        self.stream.seek(0)
        data = self.stream.read()
        records = []
        previous_hash = None
        for line in data.decode('utf-8').splitlines():
            record = json.loads(line)
            if record.get('seq') != len(records) + 1 or record.get('previous_hash') != previous_hash:
                raise UninstallError('事务日志链损坏，停止自动恢复')
            records.append(record)
            previous_hash = install.fingerprint(record)
        return records, data

    def _verify_full(self):
        records, data = self._read_full_chain()
        if (records != self.records or len(data) != self.length
                or hashlib.sha256(data).digest() != self.content_hash.digest()):
            raise UninstallError('事务日志历史内容改变，停止')

    def check(self):
        if self.failed:
            raise UninstallError('事务日志 session 已失败，停止')
        try:
            if self._observed() != (self.stable_id, self.length):
                raise UninstallError('事务日志身份/长度改变，停止')
            self.stream.seek(self.length - len(self.tail))
            if self.stream.read(len(self.tail)) != self.tail:
                raise UninstallError('事务日志尾部 hash 记录改变，停止')
            # Same-length/forged-mtime edits cannot be excluded by advisory flock.
            if not self.deny_foreign_writes:
                self._verify_full()
            if self._observed() != (self.stable_id, self.length):
                raise UninstallError('校验期间事务日志身份/长度改变，停止')
        except BaseException:
            self.failed = True
            raise

    def events(self):
        self.check()
        return deepcopy(self.records)  # including details/identity nested lists

    def append(self, owner, phase, details):
        self.check()
        try:
            record = {'seq': len(self.records) + 1, 'phase': phase, 'at': time.time(),
                      'details': deepcopy(details), 'previous_hash': self.last_hash}
            payload = (install.canonical(record) + '\n').encode('utf-8')
            # Preserve Installer's splitlines-compatible old input contract while
            # separating a valid legacy final record lacking its newline.
            separator = b'\n' if self.length and not self.tail.endswith((b'\n', b'\r')) else b''
            written = separator + payload
            self.stream.seek(self.length)
            if self.stream.write(written) != len(written):
                raise UninstallError('事务日志短写，停止')
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.records.append(record)
            self.length += len(written)
            self.tail = payload
            self.last_hash = install.fingerprint(record)
            self.content_hash.update(written)
            self.check()
            owner._write_json(self.path.parent / 'journal.json', record)
        except BaseException:
            self.failed = True
            raise

    def finish(self):
        # Always re-read all old/new bytes before releasing the owned handle,
        # including exceptional exits. Never delete/repair an unknown log.
        self._verify_full()
        self.check()


def _identity(path):
    stat = install._ordinary_file(install.checked(path))
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _scan(root):
    """Reject links/embedded databases before any unlink, including unknown extras."""
    install.mandatory_label(install.checked(root))
    result = []
    if not root.is_dir():
        raise UninstallError('程序目标必须仍为普通目录')
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = install.checked(Path(folder) / name)
            install.mandatory_label(path)
            relative = release.safe_relative(path.relative_to(root).as_posix())
            if len(result) >= MAX_ENTRIES:
                raise UninstallError('卸载目录项目过多，拒绝自动处理')
            if path.is_dir():
                result.append({'path': relative, 'kind': 'directory'})
                continue
            stat = install._ordinary_file(path)
            if path.name.casefold().endswith(release.DATABASE_SUFFIXES):
                raise UninstallError('程序目录包含数据库；先将用户数据独立保存，卸载拒绝')
            with path.open('rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':
                    raise UninstallError('程序目录含SQLite数据，卸载拒绝')
            result.append({'path': relative, 'kind': 'file', 'size': stat.st_size})
    folded = set()
    for item in result:
        if item['path'].casefold() in folded:
            raise UninstallError('Windows大小写目录冲突')
        folded.add(item['path'].casefold())
    return result


def _validate_manifest(root, manifest, actual):
    """Same release-v1 contract, allowing ONLY additional preserved ordinary files.

    verify_manifest itself intentionally rejects extras. When no extras are present
    it is reused directly. With extras this validates the identical metadata and
    each tracked hash without modifying/copying either tree or release module.
    """
    if (type(manifest) is not dict or manifest.get('schema') != release.SCHEMA
            or not isinstance(manifest.get('version'), str)
            or not release.VERSION_RE.fullmatch(manifest['version'])):
        raise UninstallError('未知发布清单/版本')
    built = datetime.fromisoformat(manifest['built_at_utc'].replace('Z', '+00:00'))
    if built.tzinfo != timezone.utc:
        raise UninstallError('发布构建时间须UTC')
    source = manifest.get('version_source', {})
    if source.get('path') != release.VERSION_SOURCE or source.get('field') != 'project.version':
        raise UninstallError('不可信规范版本来源')
    metadata = install.checked(root / release.VERSION_SOURCE)
    if (release.sha256(metadata) != source.get('sha256')
            or release.version_from(metadata) != manifest['version']
            or release.data_schema_from(metadata) != manifest.get('data_schema')):
        raise UninstallError('版本/schema规范源不匹配')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise UninstallError('发布文件清单无效')
    seen = set()
    for entry in entries:
        if type(entry) is not dict or set(entry) != {'path', 'size', 'sha256'}:
            raise UninstallError('发布文件记录字段无效')
        rel = release.safe_relative(entry['path'])
        if rel == release.MANIFEST_NAME or rel.casefold() in seen:
            raise UninstallError('重复路径或清单自引用')
        seen.add(rel.casefold())
        if (type(entry['size']) is not int or entry['size'] < 0
                or not release.SHA_RE.fullmatch(entry['sha256'])):
            raise UninstallError('文件大小/hash记录无效')
        target = install.checked(root / rel)
        if (not target.is_file() or install._ordinary_file(target).st_size != entry['size']
                or release.sha256(target) != entry['sha256']):
            raise UninstallError('卸载目标偏离批准清单：' + rel)
    files = {item['path'] for item in actual if item['kind'] == 'file'}
    expected = {entry['path'] for entry in entries} | {release.MANIFEST_NAME}
    if files == expected:
        release.verify_manifest(root)
    return sorted(files - expected)


class Uninstaller(install.Installer):
    """Shares Installer's lease; only actual deletion uses the held event writer."""
    def __init__(self, allow_root, state_dir, *, process_probe=install.windows_processes,
                 hook=None, ttl=TTL):
        if type(ttl) not in (int, float) or not 1 <= ttl <= 600:
            raise UninstallError('卸载审批TTL无效')
        super().__init__(allow_root, state_dir, process_probe=process_probe, hook=hook, ttl=ttl)
        self._held_events = None

    @contextmanager
    def _new_held_event_append_session(self, folder):
        if self._held_events is not None:
            raise UninstallError('拒绝嵌套事务日志 session')
        with _held_event_stream(folder / 'events.jsonl') as (stream, protected):
            session = _HeldEventAppendSession(folder / 'events.jsonl', stream, protected)
            self._held_events = session
            try:
                yield session
            finally:
                try:
                    session.finish()
                finally:
                    # The enclosing stream owner closes even on failed validation;
                    # retain bytes as-is rather than dropping/repairing unknown data.
                    self._held_events = None

    def _event(self, folder, phase, **details):
        session = self._held_events
        if session is None:
            return super()._event(folder, phase, **details)
        if session.path != folder / 'events.jsonl':
            raise UninstallError('事务日志 session 路径不符')
        session.append(self, phase, details)

    def _events(self, folder):
        session = self._held_events
        if session is None:
            return super()._events(folder)
        if session.path != folder / 'events.jsonl':
            raise UninstallError('事务日志 session 路径不符')
        return session.events()

    def _uninstall_target(self, target, data_dir=None):
        path = self._target(target, data_dir)
        lower = path.name.casefold()
        if (lower.endswith(('.previous', '.stage', '.swap')) or lower.startswith(('.', 'tx-', 'uninstall-'))
                or any(part.casefold() in {'staging', '_build'} for part in path.relative_to(self.root).parts)):
            raise UninstallError('拒绝previous/staging/swap/事务目录作为卸载目标')
        return path

    def _busy_target(self, target):
        if self.process_probe(target):
            raise UninstallError('程序仍在运行；请正常退出，不自动结束进程')

    def preview(self, target, expected_manifest_sha, *, data_dir=None):
        if not isinstance(expected_manifest_sha, str) or not release.SHA_RE.fullmatch(expected_manifest_sha):
            raise UninstallError('必须提供可信清单SHA256')
        target = self._uninstall_target(target, data_dir)
        with self._lease():
            install.mandatory_label(target.parent)
            actual = _scan(target)
            self._busy_target(target)
            manifest_path = install.checked(target / release.MANIFEST_NAME)
            manifest_identity = _identity(manifest_path)
            if manifest_identity[2] > 32 * 1024**2:
                raise UninstallError('卸载发布清单超过容量限制')
            if release.sha256(manifest_path) != expected_manifest_sha:
                raise UninstallError('清单SHA256不匹配')
            manifest = install.read_json(manifest_path)
            unknown = _validate_manifest(target, manifest, actual)
            files = [{**entry, 'identity': _identity(target / entry['path'])}
                     for entry in sorted(manifest['files'], key=lambda item: item['path'])]
            files.append({'path': release.MANIFEST_NAME, 'size': manifest_path.stat().st_size,
                          'sha256': expected_manifest_sha, 'identity': manifest_identity})
            # Add only ancestors of tracked files, never unrelated unknown directories.
            directories = sorted({str(parent).replace('\\', '/') for entry in files
                                  for parent in Path(entry['path']).parents if parent != Path('.')},
                                 key=lambda value: (-value.count('/'), value))
            folder = self.state / ('uninstall-' + secrets.token_hex(16))
            folder.mkdir()
            plan = {'format': FORMAT, 'id': folder.name, 'allow_root': str(self.root), 'state_dir': str(self.state),
                    'target': str(target), 'data_dir': str(install.checked(data_dir)) if data_dir is not None else None,
                    'expected_manifest_sha': expected_manifest_sha, 'manifest': manifest, 'files': files,
                    'directories': directories, 'unknown_files_at_preview': unknown,
                    'created_at': time.time(), 'expires_at': time.time() + self.ttl,
                    'previous_preserved': str(target.with_name(target.name + '.previous')),
                    'warning': 'Only tracked program files; unknown/user data/previous retained. Not undoable; reinstall trusted package.'}
            digest = install.fingerprint(plan)
            self._write_json(folder / 'plan.json', {'plan': plan, 'fingerprint': digest})
            self._event(folder, 'preview', fingerprint=digest)
            return {'plan_path': str(folder / 'plan.json'), 'fingerprint': digest,
                    'plan': plan, 'approved': False}

    def _load_uninstall(self, plan_path, confirm=None):
        path = install.checked(plan_path)
        if (path.name != 'plan.json' or path.parent.parent != self.state
                or not path.parent.name.startswith('uninstall-')):
            raise UninstallError('不是本卸载器事务计划')
        _identity(path)
        envelope = install.read_json(path)
        plan = envelope['plan']; digest = install.fingerprint(plan)
        if (envelope.get('fingerprint') != digest or plan.get('format') != FORMAT
                or plan.get('allow_root') != str(self.root) or plan.get('state_dir') != str(self.state)
                or plan.get('id') != path.parent.name or confirm is not None and confirm != digest):
            raise UninstallError('卸载计划/精确批准指纹不符')
        target = self._uninstall_target(plan['target'], plan['data_dir'])
        expected_previous = str(target.with_name(target.name + '.previous'))
        if plan.get('previous_preserved') != expected_previous:
            raise UninstallError('previous保护路径改变')
        # Plan mutation with a recomputed fingerprint must not insert arbitrary deletions.
        expected = sorted(plan['manifest']['files'], key=lambda item: item['path'])
        records = plan['files']
        if (len(records) != len(expected) + 1 or records[-1]['path'] != release.MANIFEST_NAME
                or records[-1]['sha256'] != plan['expected_manifest_sha']):
            raise UninstallError('卸载文件集合或manifest最后规则不符')
        for entry, record in zip(expected, records[:-1]):
            release.safe_relative(entry['path'])
            if {key: record[key] for key in ('path', 'size', 'sha256')} != entry:
                raise UninstallError('卸载清单与原发布清单不符')
        events = self._events(path.parent)
        if (not events or events[0]['phase'] != 'preview'
                or events[0]['details'].get('fingerprint') != digest):
            raise UninstallError('卸载计划与最初预览记录不一致')
        for record in records:
            release.safe_relative(record['path'])
            if type(record['identity']) is not list or len(record['identity']) != 4:
                raise UninstallError('卸载文件身份无效')
            destination = install.checked(target / record['path'])
            if not destination.is_relative_to(target):
                raise UninstallError('卸载文件逃逸目标')
        return path.parent, plan, digest

    def _file_events(self, folder, plan):
        events = self._events(folder)
        known = {entry['path']: entry for entry in plan['files']}
        intents, done = set(), set()
        for event in events:
            if event['phase'] in ('unlink_intent', 'unlinked'):
                relative = event['details'].get('path')
                if relative not in known:
                    raise UninstallError('日志包含非计划文件')
                if event['phase'] == 'unlink_intent':
                    if (event['details'].get('sha256') != known[relative]['sha256']
                            or event['details'].get('identity') != known[relative]['identity']):
                        raise UninstallError('删除意图与批准文件指纹不符')
                    intents.add(relative)
                elif relative not in intents:
                    raise UninstallError('删除完成记录缺少意图')
                else:
                    done.add(relative)
        return events, intents, done

    def _assert_record(self, target, record):
        path = install.checked(target / record['path'])
        if path == target or not path.is_relative_to(target):
            raise UninstallError('禁止删除根目录或越界目标')
        install.mandatory_label(path)
        if (_identity(path) != record['identity'] or path.stat().st_size != record['size']
                or release.sha256(path) != record['sha256']):
            raise UninstallError('文件已变化，保留并停止：' + record['path'])
        return path

    def _continue(self, folder, plan):
        # Caller still holds the ordinary shared Installer/Uninstaller .lease.
        # The stronger log handle lifetime covers every actual delete and status.
        with self._new_held_event_append_session(folder) as session:
            return self._continue_held(folder, plan, session)

    def _continue_held(self, folder, plan, session):
        target = self._uninstall_target(plan['target'], plan['data_dir'])
        install.mandatory_label(target.parent)
        _scan(target)
        self._busy_target(target)
        events, intents, done = self._file_events(folder, plan)
        digest = install.fingerprint(plan)
        if (not events or events[0]['phase'] != 'preview'
                or events[0]['details'].get('fingerprint') != digest
                or not any(event['phase'] == 'confirmed' and event['details'].get('fingerprint') == digest
                           for event in events)):
            raise UninstallError('持有日志缺失已批准预览/确认，停止')
        # Revalidate the whole surviving tracked set before the next deletion.
        for record in plan['files']:
            path = install.checked(target / record['path'])
            if record['path'] in done:
                if path.exists():
                    raise UninstallError('已删文件重新出现，停止且保留')
            elif path.exists():
                self._assert_record(target, record)
            elif record['path'] not in intents:
                raise UninstallError('文件无删除意图即缺失，停止：' + record['path'])
        for record in plan['files']:
            path = install.checked(target / record['path'])
            relative = record['path']
            if relative in done:
                if path.exists():
                    raise UninstallError('已删文件重新出现，停止且保留')
                continue
            if not path.exists():
                if relative not in intents:
                    raise UninstallError('文件无删除意图即缺失，停止：' + relative)
                self._event(folder, 'unlinked', path=relative, reconciled_after_crash=True)
                done.add(relative)
                continue
            self._assert_record(target, record)
            if relative not in intents:
                self._event(folder, 'unlink_intent', path=relative, sha256=record['sha256'], identity=record['identity'])
                intents.add(relative)
            if self.hook:
                self.hook('before_unlink', path)
            self._busy_target(target)
            path = self._assert_record(target, record)  # exact resolved absolute path checked immediately before unlink
            session.check()  # detect helper/probe log changes before a destructive step
            path.unlink()
            if self.hook:
                self.hook('after_unlink', path)
            self._event(folder, 'unlinked', path=relative)
            done.add(relative)
        # Remove only now-empty tracked ancestor directories, never unknown directories/root.
        allowed_directories = {str(parent).replace('\\', '/') for record in plan['files']
                               for parent in Path(record['path']).parents if parent != Path('.')}
        for relative in sorted(allowed_directories, key=lambda value: (-value.count('/'), value)):
            path = install.checked(target / release.safe_relative(relative))
            if not path.exists():
                continue
            if not path.is_relative_to(target) or path == target or not path.is_dir():
                raise UninstallError('非计划普通祖先目录，拒绝清理')
            install.mandatory_label(path)
            if next(path.iterdir(), None) is None:
                session.check()
                path.rmdir()
        self._event(folder, 'uninstalled', remaining_unknown_files=[item['path'] for item in _scan(target) if item['kind'] == 'file'])
        return self.status(folder / 'plan.json')

    def commit(self, plan_path, confirm_fingerprint):
        with self._lease():
            folder, plan, digest = self._load_uninstall(plan_path)
            events = self._events(folder)
            if len(events) != 1 or events[0]['phase'] != 'preview':
                raise UninstallError('卸载批准已尝试，不能重放；中断需显式recover')
            # Failed/expired attempts consume this approval as well.
            self._event(folder, 'commit_attempted', fingerprint=digest)
            if confirm_fingerprint != digest:
                self._event(folder, 'confirmation_rejected')
                raise UninstallError('需要用户确认的精确fingerprint；本次尝试已消费')
            if time.time() >= plan['expires_at']:
                self._event(folder, 'confirmation_expired')
                raise UninstallError('卸载预览已过期，未删除；须新预览')
            self._event(folder, 'confirmed', fingerprint=digest)
            return self._continue(folder, plan)

    def recover(self, plan_path, confirm_fingerprint):
        with self._lease():
            folder, plan, _ = self._load_uninstall(plan_path, confirm_fingerprint)
            events = self._events(folder)
            if not events or events[0]['phase'] != 'preview' or not any(e['phase'] == 'confirmed' for e in events):
                raise UninstallError('仅恢复已明确尝试的卸载事务；预览不是授权')
            self._event(folder, 'recover_attempted', fingerprint=confirm_fingerprint)
            return self._continue(folder, plan)

    def status(self, plan_path):
        folder, plan, digest = self._load_uninstall(plan_path)
        events, _, done = self._file_events(folder, plan)
        target = self._uninstall_target(plan['target'], plan['data_dir'])
        remaining = _scan(target)
        remaining_files = [item['path'] for item in remaining if item['kind'] == 'file']
        tracked = {item['path'] for item in plan['files']}
        removed = len(done) == len(tracked) and not tracked.intersection(remaining_files)
        return {'transaction': plan['id'], 'phase': events[-1]['phase'] if events else 'missing_journal',
                'fingerprint': digest, 'target': str(target), 'program_removed': removed,
                'deleted_tracked_files': len(done), 'remaining_unknown_files': sorted(set(remaining_files)-tracked),
                'remaining_directories': [item['path'] for item in remaining if item['kind'] == 'directory'],
                'previous_preserved': plan['previous_preserved'], 'data_modified': False,
                'automatic_start': False, 'reversible': False,
                'notice': '仅移除跟踪的程序文件；未知文件/数据/previous保留。恢复程序须另行提供可信发布包。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-root', required=True, type=Path)
    parser.add_argument('--state-dir', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    preview = commands.add_parser('preview')
    preview.add_argument('--target', required=True, type=Path)
    preview.add_argument('--expected-manifest-sha', required=True)
    preview.add_argument('--data-dir', type=Path)
    for name in ('commit', 'recover', 'status'):
        command = commands.add_parser(name)
        command.add_argument('--plan', type=Path, required=True)
        if name != 'status':
            command.add_argument('--confirm-fingerprint', required=True)
    args = parser.parse_args(argv)
    tool = Uninstaller(args.allow_root, args.state_dir)
    if args.command == 'preview':
        result = tool.preview(args.target, args.expected_manifest_sha, data_dir=args.data_dir)
    elif args.command == 'status':
        result = tool.status(args.plan)
    else:
        result = getattr(tool, args.command)(args.plan, args.confirm_fingerprint)
    print(install.canonical(result))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as error:
        print('卸载事务拒绝：' + str(error), file=__import__('sys').stderr)
        raise SystemExit(1)
