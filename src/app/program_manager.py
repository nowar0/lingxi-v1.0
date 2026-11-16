"""Fixed offline program-manager bootstrap, not a general Python/module runner.

Import and --help perform no filesystem, APPDATA, configuration or key access.
The frozen build MUST bundle all three pinned tools and tools-manifest.json under
sys._MEIPASS/program-tools. No cwd, PATH, sys.path or user-supplied tool fallback.
This bootstrap alone is not proof of a working Python-free frozen manager.
Uninstall has a distinct preview/commit/recover/status namespace; never default delete.
"""
from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
from types import ModuleType
import zipfile

FORMAT = 'lingxi.program-tools.v1'
# Build-time owned metadata, not a publisher signature or an adversarial same-user
# filesystem defense. Changing a bundled tool requires re-review/re-pin/re-freeze.
TOOL_PINS = {'release_tools.py': {'sha256': '65a6f10b573ec9e0596649b297e3d242ab53473b0d0b046b73e169b1a524e8ce', 'size': 33247}, 'install_transaction.py': {'sha256': 'd9164125066b789bbd6337f1ba7934920f2da856f4ccf764aae7a2749b15cdcf', 'size': 38380}, 'uninstall_transaction.py': {'sha256': '6b9aa3723d88625ecc18e7e3dc6c58bd5ea4be2e6bb3364b19b7fa694a723034', 'size': 29980}}

MAX_MANIFEST_BYTES = 4096
_LOAD_LOCK = threading.RLock()


class ProgramManagerError(ValueError):
    pass


def _tool_root():
    if getattr(sys, 'frozen', False):
        bundle = getattr(sys, '_MEIPASS', None)
        if not isinstance(bundle, str) or not Path(bundle).is_absolute():
            raise ProgramManagerError('冻结程序缺少可信内置工具根；不使用外部Python或工作目录回退')
        return Path(bundle) / 'program-tools'
    return Path(__file__).resolve().parents[1] / 'packaging'


def _checked_file(path, maximum):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if (part.is_symlink() or part.is_junction()
                or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise ProgramManagerError('内置工具路径含链接或重解析点，拒绝加载')
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise ProgramManagerError('内置工具不是普通单链接文件或超过容量限制')
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    with path.open('rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ProgramManagerError('读取内置工具时文件已改变')
        data = stream.read(maximum + 1)
        after_handle = os.fstat(stream.fileno())
    if len(data) > maximum or identity(before) != identity(after_handle) or identity(before) != identity(path.stat()):
        raise ProgramManagerError('读取内置工具时文件已改变')
    return data


def _strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProgramManagerError('内置工具清单含重复字段')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProgramManagerError('非有限工具清单')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProgramManagerError('内置工具清单格式无效') from exc


def _verified_tools(root, *, frozen):
    if not Path(root).is_absolute():
        raise ProgramManagerError('内置工具根必须为固定绝对路径')
    if frozen:
        manifest = _strict_json(_checked_file(root / 'tools-manifest.json', MAX_MANIFEST_BYTES))
        if (type(manifest) is not dict or set(manifest) != {'schema', 'files'}
                or manifest['schema'] != FORMAT or manifest['files'] != TOOL_PINS):
            raise ProgramManagerError('冻结内置工具清单与规范哈希不匹配')
    result = {}
    for name, pin in TOOL_PINS.items():
        raw = _checked_file(root / name, pin['size'])
        if len(raw) != pin['size'] or hashlib.sha256(raw).hexdigest() != pin['sha256']:
            raise ProgramManagerError('内置安装工具内容与已审核哈希不匹配，拒绝执行')
        result[name] = raw
    return result


def _compile_private(name, path, raw, release_module=None, install_module=None):
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ''
    namespace = dict(vars(builtins))
    standard_import = builtins.__import__
    private_dependencies = {'release_tools': release_module, 'install_transaction': install_module}
    def trusted_import(module_name, globals=None, locals=None, fromlist=(), level=0):
        top_level = module_name.split('.', 1)[0]
        if top_level in {'release_tools', 'install_transaction', 'uninstall_transaction'}:
            dependency = private_dependencies.get(module_name)
            if level != 0 or dependency is None:
                raise ProgramManagerError('内置工具依赖未验证或不在固定依赖映射中')
            return dependency
        return standard_import(module_name, globals, locals, fromlist, level)
    namespace['__import__'] = trusted_import
    module.__dict__['__builtins__'] = namespace
    # Compiles the bytes that were hashed, not a second loader read by filename.
    exec(compile(raw, str(path), 'exec', dont_inherit=True), module.__dict__)
    return module


def _load_fixed_tools():
    with _LOAD_LOCK:
        root = _tool_root()
        raw = _verified_tools(root, frozen=bool(getattr(sys, 'frozen', False)))
        release = _compile_private('_lingxi_program_release_tools', root / 'release_tools.py', raw['release_tools.py'])
        installer = _compile_private('_lingxi_program_install_transaction', root / 'install_transaction.py',
                                     raw['install_transaction.py'], release)
        uninstaller = _compile_private('_lingxi_program_uninstall_transaction', root / 'uninstall_transaction.py',
                                       raw['uninstall_transaction.py'], release, installer)
        if not all(callable(getattr(module, 'main', None)) for module in (installer, uninstaller)):
            raise ProgramManagerError('内置程序管理工具缺少固定CLI入口')
        return installer, uninstaller


def _load_installer():
    return _load_fixed_tools()[0]


def _load_uninstaller():
    return _load_fixed_tools()[1]


def _sha_argument(value):
    if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        raise argparse.ArgumentTypeError('必须为64位小写SHA256精确指纹')
    return value


def _parser():
    parser = argparse.ArgumentParser(
        prog='LingxiServer.exe --program-manager',
        description='离线便携程序管理；不启动服务、不读默认数据/Key、不自动批准安装。',
        epilog='preview/dry及uninstall-preview仅生成计划；安装与卸载commit/recover必须准确指纹。卸载仅移除跟踪程序文件，保留数据/未知文件/previous。',
        allow_abbrev=False)
    parser.add_argument('--allow-root', type=Path, help='显式允许的独立程序父目录')
    parser.add_argument('--state-dir', type=Path, help='允许目录内的事务状态目录')
    sub = parser.add_subparsers(dest='command')
    preview = sub.add_parser('preview', aliases=['dry'], help='只生成安装预览，不改程序目标', allow_abbrev=False)
    preview.add_argument('--source', required=True, type=Path)
    preview.add_argument('--target', required=True, type=Path)
    preview.add_argument('--archive-record', type=Path)
    preview.add_argument('--legacy-manifest', type=Path)
    preview.add_argument('--data-dir', type=Path)
    preview.add_argument('--data-schema', type=int)
    rollback = sub.add_parser('rollback', help='仅生成回滚计划，仍需独立commit', allow_abbrev=False)
    rollback.add_argument('--installed-plan', required=True, type=Path)
    rollback.add_argument('--data-dir', type=Path)
    rollback.add_argument('--data-schema', type=int)
    for name in ('status', 'recover', 'commit'):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument('--plan', required=True, type=Path)
        if name != 'status':
            command.add_argument('--confirm-fingerprint', required=True, type=_sha_argument)
    uninstall_preview = sub.add_parser('uninstall-preview', help='只生成卸载预览，保留用户数据/未知文件/previous', allow_abbrev=False)
    uninstall_preview.add_argument('--target', required=True, type=Path)
    uninstall_preview.add_argument('--expected-manifest-sha', required=True, type=_sha_argument)
    uninstall_preview.add_argument('--data-dir', type=Path)
    for name in ('uninstall-status', 'uninstall-recover', 'uninstall-commit'):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument('--plan', required=True, type=Path)
        if name != 'uninstall-status':
            command.add_argument('--confirm-fingerprint', required=True, type=_sha_argument)
    sub.add_parser('uninstall', help='仅说明明确子命令；不隐式删除', allow_abbrev=False)
    sub.add_parser('verify-tools', help='只读核验固定内置工具及定义，不访问程序/用户数据目录', allow_abbrev=False)
    return parser


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    args = parser.parse_args(arguments)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == 'uninstall':
        print('program_manager_action_required: 请先使用 uninstall-preview 审阅精确计划，再独立调用 uninstall-commit；'
              'uninstall-status/uninstall-recover 用于已存在的卸载计划。未加载工具或删除任何文件。', file=sys.stderr)
        return 2
    if args.command == 'verify-tools':
        try:
            _load_fixed_tools()
        except (ProgramManagerError, OSError, ValueError, KeyError, ImportError, sqlite3.Error, zipfile.BadZipFile):
            print('program_manager_tools_refused: 内置工具验证失败；未执行安装或卸载。', file=sys.stderr)
            return 1
        print(json.dumps({'tools_verified': True, 'files': sorted(TOOL_PINS),
                          'installation_executed': False, 'user_data_accessed': False}, ensure_ascii=False))
        return 0
    if args.allow_root is None or args.state_dir is None:
        parser.error('执行管理命令必须显式提供 --allow-root 和 --state-dir；不使用默认用户路径')
    try:
        uninstall_commands = {'uninstall-preview': 'preview', 'uninstall-commit': 'commit',
                              'uninstall-recover': 'recover', 'uninstall-status': 'status'}
        if args.command in uninstall_commands:
            # Rebuild only validated fixed arguments. Never use replace() on raw
            # values: a source/plan path can legitimately equal a command string.
            forwarded = ['--allow-root', str(args.allow_root), '--state-dir', str(args.state_dir),
                         uninstall_commands[args.command]]
            if args.command == 'uninstall-preview':
                forwarded += ['--target', str(args.target), '--expected-manifest-sha', args.expected_manifest_sha]
                if args.data_dir is not None:
                    forwarded += ['--data-dir', str(args.data_dir)]
            else:
                forwarded += ['--plan', str(args.plan)]
                if args.command != 'uninstall-status':
                    forwarded += ['--confirm-fingerprint', args.confirm_fingerprint]
            return _load_uninstaller().main(forwarded)
        return _load_installer().main(arguments)
    except (ProgramManagerError, OSError, ValueError, KeyError, ImportError, sqlite3.Error, zipfile.BadZipFile) as error:
        # Expose only a fixed, reviewed diagnosis. Never echo raw tool exception text.
        if 'Low/Untrusted' in str(error):
            print('program_manager_low_integrity: 来源或目标带Low/Untrusted完整性标签；未搬移或修改权限，请用户手动解压到确认的普通目录后重新预览。', file=sys.stderr)
            return 1
        print('program_manager_refused: 内置程序管理工具缺失、完整性不符或操作无效；未自动重试，请审阅精确计划。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
