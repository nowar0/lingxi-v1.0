"""Explicit frozen-manager rehearsal using newly generated text fixtures only.

Default preview is read-only. Running requires the exact preview fingerprint and
an explicit synthetic-changes flag. No fixture program is executed, no user data
is imported, and no ACL/MIC is changed or bypassed. A production Low-label refusal
stops the rehearsal and preserves its report and already-created synthetic files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

FORMAT = 'lingxi.program-manager-rehearsal.v1'
_RUN_ID = re.compile(r'^[0-9a-f]{32}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
OLD_PROGRAM = b'LINGXI SYNTHETIC OLD PROGRAM - NOT AN EXECUTABLE\n'
NEW_PROGRAM = b'LINGXI SYNTHETIC NEW PROGRAM - NOT AN EXECUTABLE\n'
ASSET = b'Synthetic non-user asset\n'
DATA = b'SYNTHETIC USER DATA SENTINEL - PRESERVE EXACTLY\n'
UNKNOWN = b'SYNTHETIC UNKNOWN NOTE - PRESERVE EXACTLY\n'
STEPS = ('generate_text_fixtures', 'installation_preview_without_target_change',
         'install_upgrade_with_one_previous', 'explicit_program_rollback',
         'uninstall_preview_without_deletion', 'uninstall_only_tracked_files',
         'verify_data_unknown_and_previous_preserved')


class RehearsalError(ValueError):
    """Fixed codes only; never expose tool exception bodies."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _checked(value):
    raw = os.fspath(value)
    path = Path(raw)
    if (not path.is_absolute() or raw.startswith(('\\\\', '//')) or '..' in path.parts
            or any(ord(c) < 32 for c in raw) or path.parent == path):
        raise RehearsalError('explicit_ordinary_nonroot_path_required')
    for part in path.parts[1:]:
        if (':' in part or part.endswith((' ', '.'))
                or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part)):
            raise RehearsalError('unsafe_path_alias')
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise RehearsalError('linked_path_refused')
    return path


def _overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _fixtures(version, program):
    return {'src/pyproject.toml': ('[project]\nname="lingxi-app"\nversion="'+version+'"\n').encode('utf-8'),
            'Lingxi.exe': program, 'assets/image.txt': ASSET}


def preview(allow_root, run_id):
    """Observe only the explicitly selected parent; do not create even the lab."""
    root = _checked(allow_root)
    if not root.is_dir() or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise RehearsalError('existing_root_and_exact_run_uuid_required')
    for name in ('APPDATA', 'LOCALAPPDATA'):
        protected = os.environ.get(name)
        if protected and _overlap(root, _checked(protected)):
            raise RehearsalError('default_user_data_root_refused')
    lab = _checked(root/('rehearsal-'+run_id))
    if lab.exists():
        raise RehearsalError('fresh_lab_required_no_overwrite')
    fixture_sets = {'incoming': _fixtures('0.5.0', NEW_PROGRAM), 'app': _fixtures('0.4.0', OLD_PROGRAM)}
    root_info=root.lstat()
    body = {'schema': FORMAT, 'allow_root': str(root),
            'root_identity': {'device':root_info.st_dev,'file_id':root_info.st_ino},
            'run_id': run_id, 'lab': str(lab),
            'paths': {key: str(lab/key) for key in ('incoming', 'app', 'app.previous', 'data', 'transactions')},
            'fixture_files': {str(lab/folder/name): {'bytes':len(raw),'sha256':_sha(raw)}
                              for folder, files in fixture_sets.items() for name, raw in files.items()},
            'data_sentinel': {'path':str(lab/'data/sentinel.txt'),'sha256':_sha(DATA),'bytes':len(DATA)},
            'unknown_note': {'path':str(lab/'app/my-unknown-note.txt'),'sha256':_sha(UNKNOWN),'bytes':len(UNKNOWN)},
            'derived_files': ['incoming/release-manifest.json','app/release-manifest.json',
                              'transactions/*/plan.json','transactions/*/events.jsonl','transactions/*/journal.json',
                              'transactions/.lease','rehearsal-report.json'],
            'sequence': list(STEPS), 'fixture_versions_only': ['0.4.0','0.5.0'],
            'fixture_program_executed': False, 'application_started': False,
            'formal_data_accessed': False, 'security_labels_modified': False,
            'external_python_required_when_frozen': False,
            'warning': 'Only these fresh synthetic fixtures may be changed. Low/Untrusted refusal stops; no relabel, fallback, cleanup or retry.'}
    return {**body, 'fingerprint': _sha(_canonical(body)), 'approved':False, 'applied':False}


def _ordinary_bytes(path):
    _checked(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4*1024**2:
        raise RehearsalError('fixture_identity_or_size_refused')
    raw = path.read_bytes()
    after = path.lstat()
    if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise RehearsalError('fixture_changed_during_read')
    return raw


def _assert_package(root, version, program, release):
    manifest = release.verify_manifest(root)
    if manifest.get('version') != version:
        raise RehearsalError('fixture_version_changed')
    expected = _fixtures(version, program)
    for name, raw in expected.items():
        if _ordinary_bytes(root/name) != raw:
            raise RehearsalError('fixture_content_changed')
    files = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
    if files != set(expected)|{release.MANIFEST_NAME}:
        raise RehearsalError('fixture_package_has_unknown_files')
    return _sha(_ordinary_bytes(root/release.MANIFEST_NAME))


def _create_package(root, version, program, release):
    root.mkdir()
    for name, raw in _fixtures(version, program).items():
        path = root/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as stream:
            stream.write(raw)
    release.create_manifest(root, 'synthetic-not-electron')
    _assert_package(root,version,program,release)


def _default_tools():
    from app.program_manager import _load_fixed_tools
    return _load_fixed_tools()


def run(allow_root, run_id, *, confirm_synthetic_changes, confirm_fingerprint, tool_loader=None):
    """Execute only the previewed synthetic sequence, with production tool guards intact.

    tool_loader is a host/test-only injection, never selected by CLI or package input.
    The fixed tool modules run in-process, so a frozen entry needs no external Python.
    """
    plan = preview(allow_root,run_id)
    if (confirm_synthetic_changes is not True or not isinstance(confirm_fingerprint,str)
            or not _SHA.fullmatch(confirm_fingerprint) or confirm_fingerprint != plan['fingerprint']):
        raise RehearsalError('exact_synthetic_confirmation_required')
    if tool_loader is not None and not callable(tool_loader):
        raise RehearsalError('trusted_tool_loader_required')
    modules = (tool_loader or _default_tools)()
    if not isinstance(modules,tuple) or len(modules)!=2:
        raise RehearsalError('fixed_tools_unavailable')
    installer_module, uninstaller_module = modules
    release = installer_module.release
    # Recheck after loading definitions: never silently choose a new lab on collision.
    if preview(allow_root,run_id) != plan:
        raise RehearsalError('preview_changed')
    lab = Path(plan['lab']);lab.mkdir(exist_ok=False)
    report = {'schema':FORMAT,'lab':str(lab),'preview_fingerprint':plan['fingerprint'],
              'completed':False,'steps':[],'error_code':None,'formal_data_accessed':False,
              'fixture_program_executed':False,'application_started':False,
              'security_labels_modified':False,'external_python_required_when_frozen':False,
              'development_backup_created':False,'leftovers_preserved':True}
    try:
        incoming, target, data, state = (lab/name for name in ('incoming','app','data','transactions'))
        _create_package(incoming,'0.5.0',NEW_PROGRAM,release)
        _create_package(target,'0.4.0',OLD_PROGRAM,release)
        data.mkdir();(data/'sentinel.txt').write_bytes(DATA)
        report['steps'].append(STEPS[0])
        installer = installer_module.Installer(lab,state)
        uninstaller = uninstaller_module.Uninstaller(lab,state)
        before_manifest = _assert_package(target,'0.4.0',OLD_PROGRAM,release)
        installed = installer.preview(incoming,target,data_dir=data)
        _assert_package(incoming,'0.5.0',NEW_PROGRAM,release)
        if _assert_package(target,'0.4.0',OLD_PROGRAM,release)!=before_manifest or _ordinary_bytes(data/'sentinel.txt')!=DATA:
            raise RehearsalError('preview_modified_fixture')
        report['steps'].append(STEPS[1])
        result=installer.commit(installed['plan_path'],installed['fingerprint'])
        if result.get('phase')!='installed':raise RehearsalError('install_result_unconfirmed')
        _assert_package(target,'0.5.0',NEW_PROGRAM,release)
        previous=lab/'app.previous';_assert_package(previous,'0.4.0',OLD_PROGRAM,release)
        if _ordinary_bytes(data/'sentinel.txt')!=DATA:raise RehearsalError('data_sentinel_changed')
        report['steps'].append(STEPS[2])
        rollback=installer.preview_rollback(installed['plan_path'],data_dir=data)
        _assert_package(target,'0.5.0',NEW_PROGRAM,release);_assert_package(previous,'0.4.0',OLD_PROGRAM,release)
        result=installer.commit(rollback['plan_path'],rollback['fingerprint'])
        if result.get('phase')!='rolled_back':raise RehearsalError('rollback_result_unconfirmed')
        manifest_sha=_assert_package(target,'0.4.0',OLD_PROGRAM,release)
        _assert_package(previous,'0.5.0',NEW_PROGRAM,release)
        if _ordinary_bytes(data/'sentinel.txt')!=DATA:raise RehearsalError('data_sentinel_changed')
        report['steps'].append(STEPS[3])
        with (target/'my-unknown-note.txt').open('xb') as stream:stream.write(UNKNOWN)
        removal=uninstaller.preview(target,manifest_sha,data_dir=data)
        if _ordinary_bytes(target/'Lingxi.exe')!=OLD_PROGRAM or _ordinary_bytes(target/'my-unknown-note.txt')!=UNKNOWN:
            raise RehearsalError('uninstall_preview_modified_fixture')
        report['steps'].append(STEPS[4])
        result=uninstaller.commit(removal['plan_path'],removal['fingerprint'])
        if result.get('program_removed') is not True or (target/'Lingxi.exe').exists():
            raise RehearsalError('uninstall_result_unconfirmed')
        remaining={p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
        if remaining!={'my-unknown-note.txt'}:raise RehearsalError('unexpected_uninstall_remainder')
        report['steps'].append(STEPS[5])
        if _ordinary_bytes(data/'sentinel.txt')!=DATA or _ordinary_bytes(target/'my-unknown-note.txt')!=UNKNOWN:
            raise RehearsalError('preserved_sentinel_changed')
        _assert_package(previous,'0.5.0',NEW_PROGRAM,release)
        report['steps'].append(STEPS[6]);report['completed']=True
    except Exception as error:
        report['error_code'] = ('low_integrity_refused' if 'Low/Untrusted' in str(error)
                                else str(error) if isinstance(error,RehearsalError)
                                else 'synthetic_rehearsal_refused_or_unconfirmed')
        report['requires_manual_normal_directory'] = report['error_code']=='low_integrity_refused'
    destination=lab/'rehearsal-report.json'
    with destination.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2);stream.write('\n')
    return {**report,'report_path':str(destination)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--allow-root',type=Path,required=True)
    parser.add_argument('--run-id',required=True,help='Exact lowercase 32-hex UUID; the same value must be confirmed')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--preview',action='store_true',help='Read-only plan (default); creates no fixture files')
    mode.add_argument('--run',action='store_true',help='Run only the exact newly generated synthetic fixture sequence')
    parser.add_argument('--confirm-synthetic-changes',action='store_true')
    parser.add_argument('--confirm-fingerprint')
    args=parser.parse_args(argv)
    if not args.run and (args.confirm_synthetic_changes or args.confirm_fingerprint is not None):
        parser.error('Confirmation is accepted only with --run; preview is read-only')
    try:
        value=(run(args.allow_root,args.run_id,confirm_synthetic_changes=args.confirm_synthetic_changes,
                   confirm_fingerprint=args.confirm_fingerprint) if args.run else preview(args.allow_root,args.run_id))
    except (RehearsalError,OSError,ValueError,TypeError,ImportError):
        print(json.dumps({'completed':False,'error_code':'rehearsal_request_refused','applied':False}))
        return 1
    # Redirected Windows console codepages differ; keep machine-readable CLI JSON
    # ASCII-only. The report itself stays UTF-8 and contains exact Unicode paths.
    print(json.dumps(value,ensure_ascii=True,indent=2))
    return 0 if not args.run or value['completed'] else 1


if __name__=='__main__':raise SystemExit(main())
