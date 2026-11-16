"""Offline, explicitly confirmed program-directory transactions.

No APPDATA discovery/read, app launch, network, ACL/MIC change, or process killing.
The retained previous directory is PROGRAM FILES, not a development/data backup.
Integrity manifests are not signatures: obtain release metadata via a trusted channel.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
import zipfile

import release_tools as release

FORMAT = 'lingxi.program-transaction.v1'
TTL = 300
MAX_FILES = 100000
MAX_BYTES = 8 * 1024**3


class InstallError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InstallError('重复 JSON 字段')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def checked(path):
    """Check lexical ancestors before resolving, including junctions/reparse points."""
    raw = str(path)
    if raw.startswith(('\\\\', '//', '\\\\?\\', '\\\\.\\')):
        raise InstallError('拒绝网络/设备路径；只支持显式本地卷目录')
    lexical = Path(raw)
    if '..' in lexical.parts:
        raise InstallError('拒绝父目录跳转路径')
    value = Path(os.path.abspath(path))
    for part in (value, *value.parents):
        if part.is_symlink() or part.is_junction() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise InstallError('路径包含链接或重解析点：' + str(part))
    return value.resolve()


def overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def mandatory_label(path):
    """Read MIC only. Never strip/modify labels to make a candidate executable."""
    if os.name != 'nt':
        return ''
    from ctypes import wintypes as W
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(W.LPWSTR), ctypes.POINTER(W.DWORD)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    status = adv.GetNamedSecurityInfoW(str(path), 1, 0x10, None, None, None, None, ctypes.byref(descriptor))
    if status:
        raise InstallError('无法读取候选完整性标签；不自动更改权限：' + str(status))
    text, length = W.LPWSTR(), W.DWORD()
    try:
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x10, ctypes.byref(text), ctypes.byref(length)):
            raise InstallError('无法读取完整性标签文本')
        try:
            label = text.value or ''
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.LocalFree(descriptor)
    if ';;;LW)' in label or ';;;UN)' in label or 'S-1-16-4096' in label or 'S-1-16-0)' in label:
        raise InstallError('Low/Untrusted 候选不能由包装搬移来解除标签；请用户手动解压到已确认普通目录')
    return label


def labels_in_tree(root):
    mandatory_label(root)
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            mandatory_label(checked(Path(folder) / name))


def _ordinary_file(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise InstallError('拒绝特殊文件或硬链接：' + str(path))
    return st


def tree(root):
    root = checked(root)
    if not root.is_dir():
        raise InstallError('程序目录不存在：' + str(root))
    entries, seen, total = [], {}, 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            item = checked(Path(folder) / name)
            relative = release.safe_relative(item.relative_to(root).as_posix())
            folded = relative.casefold()
            if folded in seen:
                raise InstallError('Windows 大小写路径冲突')
            seen[folded] = relative
            if item.is_dir():
                entries.append({'path': relative, 'kind': 'directory'})
                continue
            st = _ordinary_file(item)
            # Program transactions must not move an embedded user database.
            if item.suffix.casefold() in {'.db', '.sqlite', '.sqlite3'}:
                raise InstallError('程序目录包含数据数据库，拒绝搬移：' + relative)
            with item.open('rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':
                    raise InstallError('程序目录包含 SQLite 数据，拒绝搬移')
            total += st.st_size
            if total > MAX_BYTES or len(entries) >= MAX_FILES:
                raise InstallError('程序文件集合超过安全限额')
            before = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            digest = release.sha256(item)
            after = item.stat()
            if before != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise InstallError('扫描期间程序文件变化')
            entries.append({'path': relative, 'kind': 'file', 'size': st.st_size, 'sha256': digest})
    return sorted(entries, key=lambda e: e['path'])


def schema_range(manifest):
    value = manifest.get('data_schema_supported')
    canonical_schema = manifest.get('data_schema')
    if canonical_schema is not None:
        if (not isinstance(canonical_schema, dict) or set(canonical_schema) != {'min', 'max', 'target'}
                or any(type(v) is not int for v in canonical_schema.values())
                or not 0 <= canonical_schema['min'] <= canonical_schema['target'] <= canonical_schema['max']):
            raise InstallError('发布 data_schema 声明无效')
        canonical_range = {key: canonical_schema[key] for key in ('min', 'max')}
        if value is not None and value != canonical_range:
            raise InstallError('两种数据 schema 声明冲突')
        value = canonical_range
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'min', 'max'} or any(type(value[k]) is not int for k in value):
        raise InstallError('data_schema_supported 必须是显式 min/max 整数范围')
    if not 0 <= value['min'] <= value['max']:
        raise InstallError('数据 schema 支持范围非法')
    return value


def schema_state(data_dir=None, data_schema=None):
    """Explicit synthetic/isolated data path only; never consult app.config/APPDATA."""
    if data_schema is not None and (type(data_schema) is not int or data_schema < 0):
        raise InstallError('data_schema 必须是非负整数，不是源码版本')
    if data_dir is None:
        return {'present': data_schema is not None, 'schema': data_schema, 'data_dir': None, 'source': 'explicit_value'}
    root = checked(data_dir)
    database = root / 'lingxi.db'
    if not database.exists():
        return {'present': data_schema is not None, 'schema': data_schema, 'data_dir': str(root),
                'source': 'explicit_schema_no_database' if data_schema is not None else 'explicit_isolated_directory'}
    _ordinary_file(checked(database))
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        observed = conn.execute('PRAGMA user_version').fetchone()[0]
    if data_schema is not None and data_schema != observed:
        raise InstallError('显式 schema 与只读数据库观测不符')
    return {'present': True, 'schema': observed, 'data_dir': str(root), 'source': 'readonly_user_version'}


def guard_schema(profile, data):
    if not data['present']:
        return
    supported = profile.get('data_schema_supported')
    if supported is None:
        raise InstallError('有数据但旧/新发布未声明可信数据 schema 范围；拒绝切换。离线旧快照只能恢复到新目录。')
    if not supported['min'] <= data['schema'] <= supported['max']:
        raise InstallError('数据 schema 不兼容，禁止直接程序回滚/升级。请用匹配程序或离线旧快照恢复新目录，不覆盖现库。')


def profile(root, legacy_manifest=None):
    root = checked(root)
    manifest_path = root / release.MANIFEST_NAME
    inventory = tree(root)
    if manifest_path.is_file():
        manifest = release.verify_manifest(root)
        return {'kind': 'manifest', 'version': manifest['version'], 'manifest_sha256': release.sha256(manifest_path),
                'data_schema_supported': schema_range(manifest), 'inventory': inventory, 'extras': []}
    if legacy_manifest is None:
        raise InstallError('已存在目标缺少 manifest；须显式提供旧发布文件清单，未知目录不覆盖')
    legacy_path = checked(legacy_manifest)
    _ordinary_file(legacy_path)
    manifest = read_json(legacy_path)
    expected, folded = [], set()
    for item in manifest.get('files', []):
        raw = item.get('path', '')
        if not raw.startswith('零汐/'):
            continue
        rel = release.safe_relative(raw[len('零汐/'):])
        if rel.casefold() in folded:
            raise InstallError('旧发布清单大小写重复')
        folded.add(rel.casefold())
        if type(item.get('bytes')) is not int or item['bytes'] < 0 or not release.SHA_RE.fullmatch(item.get('sha256', '')):
            raise InstallError('旧发布清单记录无效')
        target = checked(root / rel)
        if not target.is_file() or _ordinary_file(target).st_size != item['bytes'] or release.sha256(target) != item['sha256']:
            raise InstallError('旧目标偏离已批准发布清单：' + rel)
        expected.append(rel)
    if not expected:
        raise InstallError('旧清单未包含 零汐/ 前缀程序文件')
    extras = [e['path'] for e in inventory if e['kind'] == 'file' and e['path'] not in expected]
    return {'kind': 'legacy_manifest', 'version': manifest.get('version'), 'manifest_sha256': release.sha256(legacy_path),
            'legacy_manifest': str(legacy_path), 'data_schema_supported': schema_range(manifest), 'inventory': inventory,
            'extras': extras, 'extra_policy': 'retain_entire_previous_program_directory_never_delete'}


def windows_processes(target):
    """Enumerate image paths; never kill a process. Query failure fails closed."""
    if os.name != 'nt':
        return []
    from ctypes import wintypes as W
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    p = ctypes.WinDLL('psapi', use_last_error=True)
    p.EnumProcesses.argtypes = [ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    k.CloseHandle.argtypes = [W.HANDLE]
    ids = (W.DWORD * 65536)(); needed = W.DWORD()
    if not p.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(needed)) or needed.value >= ctypes.sizeof(ids):
        raise InstallError('无法完整枚举进程，拒绝安装')
    busy = []
    for pid in ids[:needed.value // 4]:
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            continue  # unrelated protected/system processes cannot run from this directory
        try:
            buffer = ctypes.create_unicode_buffer(32768); length = W.DWORD(len(buffer))
            if k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                image = Path(buffer.value).resolve()
                if image == target or image.is_relative_to(target):
                    busy.append({'pid': pid, 'image': str(image)})
        finally:
            k.CloseHandle(handle)
    return busy


def _zip_check(archive):
    count, total, paths, root = 0, 0, {}, set()
    with zipfile.ZipFile(archive) as stream:
        for item in stream.infolist():
            name = release.safe_relative(item.filename.rstrip('/'))
            count += 1; total += item.file_size
            mode = item.external_attr >> 16
            if (count > MAX_FILES or total > MAX_BYTES or item.file_size > 2 * 1024**3
                    or item.file_size / max(1, item.compress_size) > 1000 or item.flag_bits & 1
                    or item.external_attr & 0x400 or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise InstallError('ZIP 条目/大小/压缩比/链接超出安全范围')
            folded = name.casefold()
            if folded in paths:
                raise InstallError('ZIP 重复或大小写路径冲突')
            paths[folded] = item.is_dir()
            root.add(name.split('/')[0])
        for name, directory in paths.items():
            parts = name.split('/')
            if any('/'.join(parts[:i]) in paths and not paths['/'.join(parts[:i])] for i in range(1, len(parts))):
                raise InstallError('ZIP 文件/目录前缀冲突')
        if len(root) != 1:
            raise InstallError('ZIP 必须有唯一程序根目录')
    return root.pop()


class Installer:
    def __init__(self, allow_root, state_dir, *, process_probe=windows_processes, hook=None, ttl=TTL):
        self.root = checked(allow_root)
        if not self.root.is_dir():
            raise InstallError('allow_root 必须是已存在普通目录')
        for variable in ('APPDATA', 'LOCALAPPDATA'):
            protected = os.environ.get(variable)
            if protected and overlap(self.root, checked(protected)):
                raise InstallError('allow_root 不能包含或位于 APPDATA 用户数据根')
        self.state = checked(state_dir)
        if self.state == self.root or not self.state.is_relative_to(self.root):
            raise InstallError('state_dir 必须在显式 allow_root 内')
        self.process_probe, self.hook, self.ttl = process_probe, hook, ttl

    def _target(self, path, data_dir=None):
        target = checked(path)
        if target == self.root or not target.is_relative_to(self.root) or overlap(target, self.state):
            raise InstallError('目标越界或与状态目录重叠')
        if not target.parent.is_dir():
            raise InstallError('目标父目录必须已存在，保证同卷暂存')
        for key in ('APPDATA', 'LOCALAPPDATA'):
            protected = os.environ.get(key)
            if protected and overlap(target, checked(protected)):
                raise InstallError('禁止目标覆盖 APPDATA/LOCALAPPDATA')
        if data_dir is not None and overlap(target, checked(data_dir)):
            raise InstallError('程序目标与显式数据目录重叠')
        return target

    def _data(self, data_dir, data_schema):
        if data_dir is not None:
            data_dir = checked(data_dir)
            # This first implementation intentionally reads only explicitly authorized
            # isolated data roots. Formal APPDATA integration is a separate user act.
            if not data_dir.is_relative_to(self.root) or overlap(data_dir, self.state):
                raise InstallError('本隔离工具只读取 allow_root 内显式数据目录')
        return schema_state(data_dir, data_schema)

    @contextmanager
    def _lease(self):
        checked(self.state).mkdir(parents=True, exist_ok=True)
        mandatory_label(self.state)
        lease = self.state / '.lease'
        if lease.exists():
            checked(lease); _ordinary_file(lease)
        with lease.open('a+b') as stream:
            stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise InstallError('已有安装/恢复事务持有锁') from exc
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _write_json(self, path, value):
        checked(path.parent)
        if path.exists():
            checked(path); _ordinary_file(path)
        temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(canonical(value) + '\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _event(self, folder, phase, **details):
        log = folder / 'events.jsonl'
        events = self._events(folder)
        record = {'seq': len(events) + 1, 'phase': phase, 'at': time.time(), 'details': details,
                  'previous_hash': fingerprint(events[-1]) if events else None}
        if log.exists():
            checked(log); _ordinary_file(log)
        with log.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(canonical(record) + '\n'); stream.flush(); os.fsync(stream.fileno())
        self._write_json(folder / 'journal.json', record)

    def _events(self, folder):
        log = folder / 'events.jsonl'
        if not log.exists():
            return []
        checked(log); _ordinary_file(log)
        events = []
        for line in log.read_text(encoding='utf-8').splitlines():
            record = json.loads(line)
            if record.get('seq') != len(events) + 1 or record.get('previous_hash') != (fingerprint(events[-1]) if events else None):
                raise InstallError('事务日志链损坏，停止自动恢复')
            events.append(record)
        return events

    def _load(self, plan_path, confirm=None):
        path = checked(plan_path)
        if path.name != 'plan.json' or path.parent.parent != self.state or not path.parent.name.startswith('tx-'):
            raise InstallError('不是本安装器的已验证事务路径')
        _ordinary_file(path)
        envelope = read_json(path)
        plan = envelope['plan']
        if envelope.get('fingerprint') != fingerprint(plan) or plan.get('format') != FORMAT:
            raise InstallError('计划内容或指纹改变')
        if confirm is not None and confirm != envelope['fingerprint']:
            raise InstallError('需要用户确认的精确 fingerprint，不支持自动批准')
        if plan['allow_root'] != str(self.root) or plan['state_dir'] != str(self.state) or plan['id'] != path.parent.name:
            raise InstallError('事务根或编号不符')
        target = self._target(plan['target'], plan['data'].get('data_dir'))
        expected = {'previous': str(target.with_name(target.name + '.previous')),
                    'stage': str(target.with_name('.' + target.name + '.' + plan['id'] + '.stage')),
                    'swap': str(target.with_name('.' + target.name + '.' + plan['id'] + '.swap'))}
        if any(plan[key] != value for key, value in expected.items()):
            raise InstallError('事务路径不符合精确派生规则')
        for key in expected:
            self._target(plan[key], plan['data'].get('data_dir'))
        return path.parent, plan, envelope['fingerprint']

    def _new(self, target, data, action):
        folder = self.state / ('tx-' + secrets.token_hex(16))
        folder.mkdir()
        plan = {'format': FORMAT, 'id': folder.name, 'action': action, 'allow_root': str(self.root),
                'state_dir': str(self.state), 'target': str(target),
                'previous': str(target.with_name(target.name + '.previous')),
                'stage': str(target.with_name('.' + target.name + '.' + folder.name + '.stage')),
                'swap': str(target.with_name('.' + target.name + '.' + folder.name + '.swap')),
                'created_at': time.time(), 'expires_at': time.time() + self.ttl, 'data': data,
                'warning': 'Program files only; no application start, security-label change, data migration or automatic previous cleanup.'}
        return folder, plan

    def _finish_preview(self, folder, plan):
        result = {'plan': plan, 'fingerprint': fingerprint(plan)}
        self._write_json(folder / 'plan.json', result)
        self._event(folder, 'preview')
        return {**result, 'plan_path': str(folder / 'plan.json'), 'approved': False}

    def preview(self, source, target, *, archive_record=None, legacy_manifest=None, data_dir=None, data_schema=None):
        source = checked(source)
        target = self._target(target, data_dir)
        if overlap(source, target) or overlap(source, self.state):
            raise InstallError('来源与目标/状态目录祖先互套')
        mandatory_label(source)
        data = self._data(data_dir, data_schema)
        with self._lease():
            previous = target.with_name(target.name + '.previous')
            if previous.exists():
                raise InstallError('上一代程序已存在，先另行审阅/确认清理；不覆盖')
            old = profile(target, legacy_manifest) if target.exists() else None
            if target.exists():
                labels_in_tree(target)
            busy = self.process_probe(target)
            if busy:
                raise InstallError('目标程序正在运行；请用户自行退出，不自动结束：' + canonical(busy))
            folder, plan = self._new(target, data, 'install')
            if source.is_file():
                _ordinary_file(source)
                if archive_record is None:
                    raise InstallError('ZIP 安装必须有可信渠道取得的外置哈希记录')
                record_path = checked(archive_record); _ordinary_file(record_path)
                archive_digest, record_digest = release.sha256(source), release.sha256(record_path)
                root_name = _zip_check(source)
                verified_record = release.verify_archive(source, record_path, temp_dir=folder / 'verify')
                if (release.sha256(source) != archive_digest or release.sha256(record_path) != record_digest
                        or verified_record.get('sha256') != archive_digest):
                    raise InstallError('验证过程中 ZIP 或外置记录被替换')
                payload = folder / 'payload'; payload.mkdir()
                with zipfile.ZipFile(source) as stream:
                    stream.extractall(payload)
                if release.sha256(source) != archive_digest or release.sha256(record_path) != record_digest:
                    raise InstallError('解压过程中 ZIP 或外置记录改变')
                package = payload / root_name
                new = profile(package)
                if release.sha256(package / release.MANIFEST_NAME) != verified_record.get('manifest_sha256'):
                    raise InstallError('解压的发布清单不是刚才验证的清单')
                plan['source'] = {'kind': 'archive', 'path': str(source), 'sha256': archive_digest,
                                  'record': str(record_path), 'record_sha256': record_digest, 'payload': str(package)}
            elif source.is_dir():
                labels_in_tree(source)
                package, new = source, profile(source)
                plan['source'] = {'kind': 'directory', 'path': str(source)}
            else:
                raise InstallError('来源不是普通发布目录或 ZIP')
            guard_schema(new, data)
            plan.update(old=old, new=new)
            return self._finish_preview(folder, plan)

    def _assert_tree(self, path, inventory, label):
        if tree(path) != inventory:
            raise InstallError(label + ' 内容已变化；停止操作，保留所有目录')

    def _busy(self, plan):
        for key in ('target', 'previous', 'stage', 'swap'):
            if Path(plan[key]).exists() and self.process_probe(Path(plan[key])):
                raise InstallError('已验证路径存在运行进程；用户自行退出，不自动结束')

    def _rename(self, folder, source, destination, point):
        source, destination = checked(source), checked(destination)
        if destination.exists() or not source.exists():
            raise InstallError('rename 来源缺失或目标已存在；拒绝覆盖')
        if source.parent.stat().st_dev != destination.parent.stat().st_dev:
            raise InstallError('拒绝跨卷 rename')
        labels_in_tree(source)
        mandatory_label(destination.parent)
        self._event(folder, point + '_intent')
        if self.hook:
            self.hook(point + '_before')
        if self.process_probe(source):
            raise InstallError('rename 前目标出现运行进程，未自动结束')
        os.rename(source, destination)
        if self.hook:
            self.hook(point + '_after')
        self._event(folder, point + '_done')

    def commit(self, plan_path, confirm_fingerprint):
        with self._lease():
            folder, plan, digest = self._load(plan_path, confirm_fingerprint)
            events = self._events(folder)
            if not events or events[-1]['phase'] != 'preview' or plan['expires_at'] < time.time():
                raise InstallError('计划过期/已尝试提交；需恢复或重新预览，不能重放')
            # Consume the approval before any validation that can fail; restoring
            # bytes afterwards must not make the same human approval replayable.
            self._event(folder, 'commit_attempted', fingerprint=digest)
            self._busy(plan)
            target, previous, stage, swap = [Path(plan[k]) for k in ('target', 'previous', 'stage', 'swap')]
            if self._data(plan['data']['data_dir'], plan['data']['schema']) != plan['data']:
                raise InstallError('数据 schema 状态已改变')
            mandatory_label(target.parent)
            if target.exists(): labels_in_tree(target)
            if previous.exists(): labels_in_tree(previous)
            desired = plan['new'] if plan['action'] == 'install' else plan['old']
            guard_schema(desired, plan['data'])
            self._assert_tree(target, plan['old']['inventory'], '旧目标') if plan['action'] == 'install' and plan['old'] else None
            if plan['action'] == 'install':
                if not plan['old'] and target.exists() or previous.exists() or stage.exists() or swap.exists():
                    raise InstallError('目标/保留目录/暂存路径已变化')
                source = plan['source']; package = Path(source.get('payload', source['path']))
                mandatory_label(checked(source['path']))
                if source['kind'] == 'directory': labels_in_tree(package)
                if source['kind'] == 'archive':
                    _ordinary_file(checked(source['path'])); _ordinary_file(checked(source['record']))
                    if release.sha256(checked(source['path'])) != source['sha256'] or release.sha256(checked(source['record'])) != source['record_sha256']:
                        raise InstallError('ZIP 或外置哈希记录已变化')
                self._assert_tree(package, plan['new']['inventory'], '候选来源')
                self._event(folder, 'confirmed', fingerprint=digest)
                stage.mkdir()
                release.copy_tree(package, stage)
                self._assert_tree(stage, plan['new']['inventory'], 'staging')
                labels_in_tree(stage)
                self._event(folder, 'staged')
                # Copying may take minutes: recheck the live schema and old tree
                # immediately before the first publish rename, not only at entry.
                if self._data(plan['data']['data_dir'], plan['data']['schema']) != plan['data']:
                    raise InstallError('暂存期间数据 schema 变化，未发布')
                self._busy(plan)
                if plan['old']: self._assert_tree(target, plan['old']['inventory'], '发布前旧目标')
                if plan['old']:
                    self._rename(folder, target, previous, 'retain_previous')
                self._rename(folder, stage, target, 'publish_candidate')
            else:
                self._assert_tree(target, plan['new']['inventory'], '当前目标')
                self._assert_tree(previous, plan['old']['inventory'], '上一代程序')
                if stage.exists() or swap.exists():
                    raise InstallError('回滚暂存路径已存在')
                self._event(folder, 'confirmed', fingerprint=digest)
                self._rename(folder, target, swap, 'rollback_save_current')
                self._rename(folder, previous, target, 'rollback_publish_previous')
                self._rename(folder, swap, previous, 'rollback_retain_current')
            self._assert_tree(target, desired['inventory'], '安装后程序')
            self._event(folder, 'installed' if plan['action'] == 'install' else 'rolled_back')
            return self.status(plan_path)

    def preview_rollback(self, installed_plan, *, data_dir=None, data_schema=None):
        with self._lease():
            _, original, _ = self._load(installed_plan)
            status = self.status(installed_plan)
            if status['phase'] not in ('installed', 'installed_recovered') or not original['old']:
                raise InstallError('只允许对已安装且保留上一代的精确事务预览回滚')
            # Omitting data arguments cannot silently bypass the original
            # installation's schema guard. Inherit its explicit observation.
            if data_dir is None:
                data_dir = original['data']['data_dir']
                if data_schema is None and data_dir is None:
                    data_schema = original['data']['schema']
            target = self._target(original['target'], data_dir)
            data = self._data(data_dir, data_schema)
            guard_schema(original['old'], data)
            self._assert_tree(target, original['new']['inventory'], '当前目标')
            self._assert_tree(Path(original['previous']), original['old']['inventory'], '上一代程序')
            self._busy(original)
            folder, plan = self._new(target, data, 'rollback')
            plan.update(old=original['old'], new=original['new'], installed_plan=str(checked(installed_plan)))
            return self._finish_preview(folder, plan)

    def status(self, plan_path):
        folder, plan, digest = self._load(plan_path)
        events = self._events(folder)
        return {'transaction': plan['id'], 'action': plan['action'], 'phase': events[-1]['phase'] if events else 'missing_journal',
                'fingerprint': digest, 'target': plan['target'], 'previous': plan['previous'],
                'automatic_start': False, 'data_modified': False, 'events': len(events)}

    def recover(self, plan_path, confirm_fingerprint):
        """Reconcile only exact known tree identities, never guess/delete an unknown path."""
        with self._lease():
            folder, plan, _ = self._load(plan_path, confirm_fingerprint)
            self._busy(plan)
            events = self._events(folder)
            if not events:
                raise InstallError('缺失日志，不能恢复')
            terminal = events[-1]['phase']
            if terminal in ('installed', 'installed_recovered'):
                guard_schema(plan['new'], self._data(plan['data']['data_dir'], None if plan['data']['data_dir'] else plan['data']['schema']))
                self._assert_tree(Path(plan['target']), plan['new']['inventory'], '已安装目标')
                if plan['old']: self._assert_tree(Path(plan['previous']), plan['old']['inventory'], '上一代程序')
                return self.status(plan_path)
            if terminal in ('rolled_back', 'rolled_back_recovered'):
                guard_schema(plan['old'], self._data(plan['data']['data_dir'], None if plan['data']['data_dir'] else plan['data']['schema']))
                self._assert_tree(Path(plan['target']), plan['old']['inventory'], '已回滚目标')
                self._assert_tree(Path(plan['previous']), plan['new']['inventory'], '保留的新程序')
                return self.status(plan_path)
            if terminal in ('preview', 'aborted_before_publish', 'restored_previous'):
                return self.status(plan_path)
            target, previous, stage, swap = [Path(plan[k]) for k in ('target', 'previous', 'stage', 'swap')]
            def identity(path):
                if not path.exists(): return None
                observed = tree(path)
                if observed == plan['new']['inventory']: return 'new'
                if plan['old'] and observed == plan['old']['inventory']: return 'old'
                return 'unknown'
            states = tuple(identity(p) for p in (target, previous, stage, swap))
            if 'unknown' in states:
                # A partial staging tree remains for explicit review; no cleanup or move.
                raise InstallError('恢复路径内容未知/不完整，停止并保留：' + repr(states))
            t, p, s, w = states
            if plan['action'] == 'install':
                if t == 'new' and p == ('old' if plan['old'] else None) and s is None and w is None:
                    guard_schema(plan['new'], self._data(plan['data']['data_dir'], plan['data']['schema']))
                    self._event(folder, 'installed_recovered')
                elif t is None and p == 'old' and s in ('new', None) and w is None:
                    # Restore only the old program, leave the verified new stage intact.
                    guard_schema(plan['old'], self._data(plan['data']['data_dir'], plan['data']['schema']))
                    self._rename(folder, previous, target, 'recover_previous')
                    self._event(folder, 'restored_previous')
                elif t == ('old' if plan['old'] else None) and p is None and w is None:
                    self._event(folder, 'aborted_before_publish', preserved_stage=s is not None)
                else:
                    raise InstallError('不识别的安装中断状态，保留全部目录：' + repr(states))
            else:
                current_data = self._data(plan['data']['data_dir'], plan['data']['schema'])
                if t == 'old':
                    guard_schema(plan['old'], current_data)
                elif t == 'new' or w == 'new':
                    guard_schema(plan['new'], current_data)
                if t == 'old' and p == 'new' and s is None and w is None:
                    self._event(folder, 'rolled_back_recovered')
                elif t == 'old' and p is None and w == 'new' and s is None:
                    self._rename(folder, swap, previous, 'recover_retain_current')
                    self._event(folder, 'rolled_back_recovered')
                elif t is None and p == 'old' and w == 'new' and s is None:
                    # Reverse the first rollback rename; no data migration occurred.
                    self._rename(folder, swap, target, 'recover_current')
                    self._event(folder, 'aborted_before_publish')
                elif t == 'new' and p == 'old' and w is None and s is None:
                    self._event(folder, 'aborted_before_publish')
                else:
                    raise InstallError('不识别的回滚中断状态，保留全部目录：' + repr(states))
            return self.status(plan_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-root', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview', aliases=['dry'])
    preview.add_argument('--source', type=Path, required=True); preview.add_argument('--target', type=Path, required=True)
    preview.add_argument('--archive-record', type=Path); preview.add_argument('--legacy-manifest', type=Path)
    preview.add_argument('--data-dir', type=Path); preview.add_argument('--data-schema', type=int)
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--installed-plan', type=Path, required=True); rollback.add_argument('--data-dir', type=Path); rollback.add_argument('--data-schema', type=int)
    for name in ('status', 'recover', 'commit'):
        command = sub.add_parser(name); command.add_argument('--plan', type=Path, required=True)
        if name != 'status': command.add_argument('--confirm-fingerprint', required=True)
    args = parser.parse_args(argv)
    engine = Installer(args.allow_root, args.state_dir)
    if args.command in ('preview', 'dry'):
        result = engine.preview(args.source, args.target, archive_record=args.archive_record, legacy_manifest=args.legacy_manifest,
                                data_dir=args.data_dir, data_schema=args.data_schema)
    elif args.command == 'rollback':
        result = engine.preview_rollback(args.installed_plan, data_dir=args.data_dir, data_schema=args.data_schema)
    else:
        method = getattr(engine, args.command)
        result = method(args.plan) if args.command == 'status' else method(args.plan, args.confirm_fingerprint)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (InstallError, OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print('安装事务拒绝：' + str(exc), file=__import__('sys').stderr)
        raise SystemExit(1)
