"""Read-only plans / explicitly requested fresh standalone manager builds.

Never installs dependencies, runs the product, deletes old output, changes ACLs, or
backs up a project. A compiler inventory is not portable-GUI/installation acceptance.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib
import uuid

import program_tools_bundle as tools
import build_sandbox_runtime as runtime_build

FORMAT = 'lingxi.program-manager-build-plan.v1'
RUNTIME_FORMAT = 'lingxi.program-manager-runtime.v1'
NAME = 'LingxiProgramManager'
MANIFEST = 'manager-runtime-inventory.json'
APP_MODULES = {'app', 'app.program_manager', 'app.program_manager_api', 'app.program_manager_ui',
               'app.program_manager_rehearsal'}
INPUTS = ('app/__init__.py', 'app/program_manager.py', 'app/program_manager_api.py',
          'app/program_manager_ui.py', 'app/program_manager_rehearsal.py', 'pyproject.toml', 'packaging/program_manager_entry.py',
          'packaging/program_tools_bundle.py', 'packaging/build_program_manager.py',
          'packaging/release_tools.py', 'packaging/install_transaction.py', 'packaging/uninstall_transaction.py')
HIDDEN = ('app.program_manager', 'app.program_manager_ui', 'app.program_manager_rehearsal', 'uvicorn.logging',
          'uvicorn.loops.asyncio', 'uvicorn.loops.auto', 'uvicorn.protocols.http.h11_impl',
          'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'anyio._backends._asyncio',
          'tomllib', 'zipfile', 'sqlite3', 'ctypes', 'shutil', 'stat', 'argparse',
          'datetime', 'hashlib', 'secrets', 'tempfile', 'uuid')
EXCLUDED = {'keyring', 'numpy', 'onnxruntime', 'tokenizers', 'playwright', 'openai', 'cryptography'}
MAX_FILES = 20000
MAX_BYTES = 256 * 1024**2
MAX_SOURCE_BYTES = 2 * 1024**2


class ManagerBuildError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def checked(value):
    raw = os.fspath(value)
    path = Path(value)
    if not path.is_absolute() or raw.startswith(('\\\\', '//')) or '..' in path.parts:
        raise ManagerBuildError('explicit local absolute path required')
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction() or part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ManagerBuildError('link/reparse path refused')
    return path


def relative(name):
    if not isinstance(name, str) or not name or '\\' in name or name.startswith('/'):
        raise ManagerBuildError('unsafe relative payload path')
    for part in name.split('/'):
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ManagerBuildError('unsafe payload alias')
    return name


def record(path, *, identity=False, allow_dependency_hardlinks=False):
    path = checked(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or (before.st_nlink != 1 and not allow_dependency_hardlinks):
        raise ManagerBuildError('ordinary single-link file required')
    ident = lambda info: [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if ident(before) != ident(opened) or ident(before) != ident(after) or ident(before) != ident(path.stat()):
        raise ManagerBuildError('file changed during observation')
    result = {'sha256': digest, 'size': before.st_size}
    if identity:
        result['identity'] = ident(before)
    return result


def inventory(root):
    root = checked(root)
    if not root.is_dir():
        raise ManagerBuildError('ordinary inventory root required')
    entries, seen, total = [], set(), 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs+files:
            path = checked(Path(folder)/name)
            rel = relative(path.relative_to(root).as_posix())
            if rel.casefold() in seen:
                raise ManagerBuildError('case collision in inventory')
            seen.add(rel.casefold())
            if len(seen) > MAX_FILES:
                raise ManagerBuildError('inventory capacity exceeded')
            if path.is_dir():
                continue
            item = record(path)
            total += item['size']
            if total > MAX_BYTES:
                raise ManagerBuildError('runtime bytes exceed bound')
            entries.append({'relative_path': rel, **item})
    return sorted(entries, key=lambda item: item['relative_path'])


def _read(path):
    before = record(path, identity=True)
    if before['size'] > MAX_SOURCE_BYTES:
        raise ManagerBuildError('source input exceeds bound')
    raw = path.read_bytes()
    if before != record(path, identity=True) or hashlib.sha256(raw).hexdigest() != before['sha256']:
        raise ManagerBuildError('source bytes changed')
    return raw, before


def _imports(raw):
    for node in ast.walk(ast.parse(raw.decode('utf-8-sig'))):
        if isinstance(node, ast.Import):
            yield from (item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ManagerBuildError('relative app import requires explicit review')
            if node.module == 'app':
                yield from ('app.'+item.name for item in node.names)
            elif node.module:
                yield node.module


def source_snapshot(source):
    source = checked(source)
    records = {}
    raw_inputs = {}
    for name in INPUTS:
        raw_inputs[name], records[name] = _read(source/name)
    if raw_inputs['app/__init__.py'].decode('utf-8-sig').strip():
        raise ManagerBuildError('app initializer must not initialize host data')
    for name in ('app/program_manager.py', 'app/program_manager_api.py', 'app/program_manager_ui.py',
                 'app/program_manager_rehearsal.py', 'packaging/program_manager_entry.py'):
        for module in _imports(raw_inputs[name]):
            if module.split('.')[0] in EXCLUDED or module.startswith('app.') and module not in APP_MODULES:
                raise ManagerBuildError('unapproved host/dependency import: '+module)
    pins = tools._bootstrap_metadata(raw_inputs['app/program_manager.py'])
    for name, pin in pins.items():
        if {key: records['packaging/'+name][key] for key in ('sha256', 'size')} != pin:
            raise ManagerBuildError('fixed program tool pin changed')
    version = tomllib.loads(raw_inputs['pyproject.toml'].decode('utf-8-sig'))['project']['version']
    if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ManagerBuildError('canonical version required')
    ui = source/'frontend/dist-manager'
    ui_entries = inventory(ui)
    paths = [item['relative_path'] for item in ui_entries]
    if (len(paths) != 3 or 'manager.html' not in paths
            or sum(bool(re.fullmatch(r'assets/manager-[A-Za-z0-9_-]+\.js', p)) for p in paths) != 1
            or sum(bool(re.fullmatch(r'assets/manager-[A-Za-z0-9_-]+\.css', p)) for p in paths) != 1):
        raise ManagerBuildError('expected independent manager HTML/JS/CSS only')
    html = (ui/'manager.html').read_text(encoding='utf-8')
    for path in paths:
        if path != 'manager.html' and path not in html:
            raise ManagerBuildError('manager page does not reference its exact assets')
    # Record identities as well as bytes; same-content replacement is not an approved source.
    ui_sources = {item['relative_path']: record(ui/item['relative_path'], identity=True) for item in ui_entries}
    return {'version': version, 'inputs': records, 'ui_root': str(ui), 'ui': ui_entries, 'ui_sources': ui_sources, 'tool_pins': pins}


def _pe_helper_record():
    helper = checked(runtime_build.__file__)
    if helper != checked(Path(__file__).parent / 'build_sandbox_runtime.py'):
        raise ManagerBuildError('PE build helper from unapproved origin')
    return record(helper, identity=True)


def _require_pe_inputs(plan):
    runtime_build.require_pe_version(plan)
    if 'pe_version_record' in plan and _pe_helper_record() != plan['pe_helper_record']:
        raise ManagerBuildError('PE build helper changed; re-plan/re-freeze')


def _make_plan(source_root, build_root, python, run_id, pe_version_file=None):
    source, root, python = map(checked, (source_root, build_root, python))
    if not source.is_dir() or not python.is_file() or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ManagerBuildError('existing source/interpreter and exact UUID required')
    if root != source/'packaging/_build/program-manager':
        raise ManagerBuildError('fixed program-manager build namespace required')
    output = root/run_id
    staging = source/'packaging/_build/staging'/('program-tools-mgr-'+run_id)
    if output.exists() or staging.exists():
        raise ManagerBuildError('fresh output/staging required; old files are retained')
    snapshot = source_snapshot(source)
    command = [str(python), '-I', '-m', 'PyInstaller', '--noconfirm', '--onedir', '--console', '--noupx',
               '--name', NAME, '--paths', str(source)]
    for module in HIDDEN:
        command += ['--hidden-import', module]
    for module in sorted(EXCLUDED):
        command += ['--exclude-module', module]
    pe_input = runtime_build.pe_version_record(pe_version_file, snapshot['version'])
    if pe_input is not None:
        command += ['--version-file', pe_input['path']]
    command += ['--add-data', str(staging)+';program-tools', '--add-data', str(source/'frontend/dist-manager')+';static/program-manager',
                '--distpath', str(output/'dist'), '--workpath', str(output/'work'), '--specpath', str(output/'spec'),
                str(source/'packaging/program_manager_entry.py')]
    body = {'schema': FORMAT, 'source_root': str(source), 'build_root': str(root), 'run_id': run_id,
            'output_root': str(output), 'runtime_root': str(output/'dist'/NAME), 'tools_staging': str(staging),
            'python': str(python), 'python_record': record(python, identity=True), 'source_snapshot': snapshot,
            'command': command, 'build_started': False, 'runtime_executed': False}
    if pe_input is not None:
        body['pe_version_record'] = pe_input
        body['pe_helper_record'] = _pe_helper_record()
    return {**body, 'fingerprint': fingerprint(body)}


def plan_build(source_root, build_root, python, *, pe_version_file=None):
    """Default operation is read-only: includes optional PE input, never a process."""
    return _make_plan(source_root, build_root, python, uuid.uuid4().hex, pe_version_file)


def validate_plan(plan):
    if type(plan) is not dict or plan.get('schema') != FORMAT:
        raise ManagerBuildError('unknown build plan')
    fresh = _make_plan(plan['source_root'], plan['build_root'], plan['python'], plan['run_id'],
                       plan.get('pe_version_record', {}).get('path'))
    if fresh != plan:
        raise ManagerBuildError('source/interpreter/command changed; stale plan refused')
    return fresh


def _audit_analysis(output, source):
    path = checked(output/'work'/NAME/'PYZ-00.toc')
    if record(path)['size'] > 16*1024**2:
        raise ManagerBuildError('module inventory exceeds bound')
    value = ast.literal_eval(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, tuple) or len(value) < 2 or not isinstance(value[1], list):
        raise ManagerBuildError('unknown PYZ inventory')
    names = set()
    for item in value[1]:
        if not isinstance(item, tuple) or len(item) < 3 or not isinstance(item[0], str):
            raise ManagerBuildError('invalid module inventory')
        name, origin = item[:2]
        if name in names:
            raise ManagerBuildError('duplicate analyzed module')
        names.add(name)
        if name.split('.')[0] in EXCLUDED or name.startswith('app.') and name not in APP_MODULES:
            raise ManagerBuildError('host/forbidden module collected: '+name)
        if name in APP_MODULES:
            expected = source/('app/__init__.py' if name == 'app' else name.replace('.', '/')+'.py')
            if checked(origin) != expected:
                raise ManagerBuildError('application module from unapproved source')
    if not APP_MODULES <= names:
        raise ManagerBuildError('fixed manager modules missing')
    return sorted(names)


def _audit_runtime(runtime, output, snapshot, staging):
    path = checked(output/'work'/NAME/'COLLECT-00.toc')
    if record(path)['size'] > 16*1024**2:
        raise ManagerBuildError('collection inventory exceeds bound')
    value = ast.literal_eval(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, tuple) or not value or not isinstance(value[0], list):
        raise ManagerBuildError('unknown collection inventory')
    expected, seen = {}, set()
    for item in value[0]:
        if not isinstance(item, tuple) or len(item) != 3 or not all(isinstance(x, str) for x in item):
            raise ManagerBuildError('invalid collected artifact')
        name, origin, kind = item
        name = relative(name.replace('\\', '/'))
        if kind not in ('EXECUTABLE', 'BINARY', 'EXTENSION', 'DATA'):
            raise ManagerBuildError('unknown collection type')
        rel = name if kind == 'EXECUTABLE' else '_internal/'+name
        if rel.casefold() in seen:
            raise ManagerBuildError('duplicate collected destination')
        seen.add(rel.casefold())
        if len(seen) > MAX_FILES:
            raise ManagerBuildError('collection capacity exceeded')
        normalized_origin = str(checked(origin)).replace('\\', '/').casefold()
        if name.startswith('program-tools/'):
            if checked(origin) != staging/name.removeprefix('program-tools/'):
                raise ManagerBuildError('tool asset from unapproved source')
        elif name.startswith('static/program-manager/'):
            if checked(origin) != Path(snapshot['ui_root'])/name.removeprefix('static/program-manager/'):
                raise ManagerBuildError('manager UI asset from unapproved source')
        elif kind == 'EXECUTABLE':
            if name != NAME+'.exe' or checked(origin) != output/'work'/NAME/(NAME+'.exe'):
                raise ManagerBuildError('unapproved executable in collection')
        elif kind in ('BINARY','EXTENSION'):
            if Path(name).suffix.lower() not in ('.dll','.pyd'):
                raise ManagerBuildError('unapproved native artifact')
        elif not (name == 'base_library.zip' or ('/site-packages/' in normalized_origin
                  and (any(part.endswith('.dist-info') for part in name.split('/'))
                       or Path(name).name.lower().startswith(('license','notice','copying'))
                       or name == 'setuptools/_vendor/jaraco/text/Lorem ipsum.txt'))):
            raise ManagerBuildError('unreviewed collected data: '+name)
        # uv/pip caches may legitimately hard-link read-only dependency origins.
        # This exception never applies to our source, UI, tools, or delivered files.
        dependency_root = Path(snapshot['ui_root']).parents[1]/'.venv/Lib/site-packages'
        source_root = Path(snapshot['ui_root']).parents[1]
        dependency_origin = (checked(origin).is_relative_to(dependency_root)
                             or '/site-packages/' in normalized_origin
                             and not checked(origin).is_relative_to(source_root))
        first = record(origin, identity=True, allow_dependency_hardlinks=dependency_origin)
        if first != record(origin, identity=True, allow_dependency_hardlinks=dependency_origin):
            raise ManagerBuildError('dependency changed during collection audit')
        expected[rel] = {key:first[key] for key in ('sha256','size')}
    entries = inventory_runtime(runtime)
    if {item['relative_path']: {k:item[k] for k in ('sha256','size')} for item in entries} != expected:
        raise ManagerBuildError('runtime differs from exact compiler collection inventory')
    for item in snapshot['ui']:
        rel = '_internal/static/program-manager/'+item['relative_path']
        if expected.get(rel) != {k:item[k] for k in ('sha256','size')}:
            raise ManagerBuildError('independent manager UI bytes missing/changed')
    staged_entries = inventory(staging)
    if {item['relative_path'] for item in staged_entries} != tools.TOOL_NAMES | {'tools-manifest.json'}:
        raise ManagerBuildError('unexpected staging tool artifact')
    for name, pin in snapshot['tool_pins'].items():
        if record(staging/name) != pin:
            raise ManagerBuildError('staged tool pin changed during compilation')
    if _strict_json(staging/'tools-manifest.json',4096) != {'schema':tools.FORMAT,'files':snapshot['tool_pins']}:
        raise ManagerBuildError('staged tool manifest changed')
    for item in staged_entries:
        rel = '_internal/program-tools/'+item['relative_path']
        if expected.get(rel) != {k:item[k] for k in ('sha256','size')}:
            raise ManagerBuildError('fixed tool bundle missing/changed')
    return entries


def inventory_runtime(runtime, *, allow_manifest=False):
    entries = inventory(runtime)
    filtered = []
    for item in entries:
        rel = item['relative_path']
        if rel == MANIFEST:
            if allow_manifest:
                continue
            raise ManagerBuildError('old runtime manifest in fresh build')
        parts = rel.casefold().split('/')
        stem = Path(rel).stem.casefold().split('.')[0]
        if (any(part in EXCLUDED or part in {'config','models','attachments','characters','plugin-packages','src'} for part in parts)
                or stem in EXCLUDED or Path(rel).suffix.lower() in {'.db','.sqlite','.sqlite3','.wal','.shm','.yaml','.yml'}
                or rel.endswith(('.py','.pyc')) and not rel.startswith('_internal/program-tools/')):
            raise ManagerBuildError('unexpected host/source/database asset: '+rel)
        if not (rel == NAME+'.exe' or rel.startswith('_internal/')):
            raise ManagerBuildError('unexpected root artifact: '+rel)
        with (Path(runtime)/rel).open('rb') as stream:
            if stream.read(16) == b'SQLite format 3\x00':
                raise ManagerBuildError('database bytes in runtime')
        filtered.append(item)
    if not any(item['relative_path'] == NAME+'.exe' for item in filtered):
        raise ManagerBuildError('manager executable missing')
    return filtered


def _strict_json(path, maximum):
    if record(path)['size'] > maximum:
        raise ManagerBuildError('JSON evidence exceeds bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ManagerBuildError('duplicate JSON evidence key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ManagerBuildError('nonfinite JSON')))


def build(plan, *, runner=subprocess.run):
    """Explicit compiler invocation, never product execution. Failures retain output."""
    plan = validate_plan(plan)
    source, output = Path(plan['source_root']), Path(plan['output_root'])
    checked(output.parent).mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    for name in ('dist','work','spec','cache'):
        (output/name).mkdir()
    bundle = tools.prepare_tools(source, Path(plan['tools_staging']))
    if source_snapshot(source) != plan['source_snapshot']:
        raise ManagerBuildError('source changed during tool bundling')
    env = dict(os.environ)
    for name in ('PYTHONPATH','PYTHONHOME'):
        env.pop(name, None)
    env['PYINSTALLER_CONFIG_DIR'] = str(output/'cache')
    metadata_path = output/'target-python.json'
    probe = ('import json,platform,struct,sys;from pathlib import Path;'
             'Path(sys.argv[1]).write_text(json.dumps({"version":sys.version.split()[0],'
             '"bits":struct.calcsize("P")*8,"machine":platform.machine()}),encoding="utf-8")')
    result = runner([plan['python'],'-I','-c',probe,str(metadata_path)], cwd=str(source), env=env, check=False)
    if result.returncode != 0 or not metadata_path.is_file():
        raise ManagerBuildError('pinned target interpreter metadata probe failed')
    metadata = _strict_json(metadata_path, 4096)
    if (type(metadata) is not dict or set(metadata) != {'version','bits','machine'}
            or not isinstance(metadata['version'],str) or not re.fullmatch(r'3\.\d+\.\d+(?:[A-Za-z0-9.+-]*)',metadata['version'])
            or type(metadata['bits']) is not int or metadata['bits'] != 64
            or not isinstance(metadata['machine'], str) or metadata['machine'].upper() not in ('AMD64','X86_64')):
        raise ManagerBuildError('Windows x64 interpreter metadata required')
    if record(Path(plan['python']),identity=True) != plan['python_record'] or source_snapshot(source) != plan['source_snapshot']:
        raise ManagerBuildError('source/interpreter changed before compiler')
    _require_pe_inputs(plan)
    result = runner(plan['command'], cwd=str(source), env=env, check=False)
    _require_pe_inputs(plan)
    if result.returncode != 0:
        raise ManagerBuildError('manager compiler failed; partial output retained')
    modules = _audit_analysis(output, source)
    runtime = Path(plan['runtime_root'])
    entries = _audit_runtime(runtime, output, plan['source_snapshot'], Path(plan['tools_staging']))
    if record(Path(plan['python']),identity=True) != plan['python_record'] or source_snapshot(source) != plan['source_snapshot']:
        raise ManagerBuildError('source/interpreter changed during build; runtime unverified')
    document = {'schema': RUNTIME_FORMAT, 'product': NAME, 'host_version': plan['source_snapshot']['version'],
                'built_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
                'build_plan_fingerprint': plan['fingerprint'], 'source_snapshot': plan['source_snapshot'],
                'python': {'target':metadata,'target_executable_sha256':plan['python_record']['sha256']},
                'tool_pins': bundle['files'], 'tool_manifest_sha256':bundle['manifest_sha256'],
                'ui_inventory': plan['source_snapshot']['ui'], 'analyzed_modules':modules,
                'files':entries, 'inventory_sha256':fingerprint(entries),
                'runtime_executed':False, 'gui_tested':False, 'installation_tested':False,
                'notice':'Build integrity only; not a signature, GUI acceptance, or proof of portable installation.'}
    options = runtime_build.pe_build_options(plan, runtime / (NAME + '.exe'))
    if options is not None:
        document['build_options'] = options
    _require_pe_inputs(plan)
    if (inventory_runtime(runtime) != entries or source_snapshot(source) != plan['source_snapshot']
            or record(Path(plan['python']),identity=True) != plan['python_record']):
        raise ManagerBuildError('inputs/runtime changed before manifest publication')
    with (runtime/MANIFEST).open('xb') as stream:
        stream.write((json.dumps(document,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
        stream.flush(); os.fsync(stream.fileno())
    verify_runtime(runtime)
    return {'runtime_root':str(runtime),'manifest':str(runtime/MANIFEST),'inventory_sha256':document['inventory_sha256'],
            'files':len(entries),'bytes':sum(item['size'] for item in entries),
            'runtime_executed':False,'gui_tested':False,'installation_tested':False}


def verify_runtime(runtime):
    root = checked(runtime)
    document = _strict_json(root/MANIFEST, 16*1024**2)
    if (type(document) is not dict or document.get('schema') != RUNTIME_FORMAT or document.get('product') != NAME
            or any(document.get(key) is not False for key in ('runtime_executed','gui_tested','installation_tested'))):
        raise ManagerBuildError('unknown runtime manifest')
    actual = inventory_runtime(root, allow_manifest=True)
    if actual != document.get('files') or fingerprint(actual) != document.get('inventory_sha256'):
        raise ManagerBuildError('runtime integrity mismatch')
    for item in document['ui_inventory']:
        expected = {**item,'relative_path':'_internal/static/program-manager/'+item['relative_path']}
        if expected not in actual:
            raise ManagerBuildError('UI manifest mismatch')
    toolroot = root/'_internal/program-tools'
    manifest = _strict_json(toolroot/'tools-manifest.json',4096)
    if manifest != {'schema':tools.FORMAT,'files':document['tool_pins']} or record(toolroot/'tools-manifest.json')['sha256'] != document['tool_manifest_sha256']:
        raise ManagerBuildError('fixed tools manifest mismatch')
    if {item['relative_path'] for item in inventory(toolroot)} != tools.TOOL_NAMES | {'tools-manifest.json'}:
        raise ManagerBuildError('unknown tool bundle artifact')
    for name, pin in document['tool_pins'].items():
        relative(name)
        if record(toolroot/name) != pin:
            raise ManagerBuildError('tool pin mismatch')
    runtime_build.verify_pe_options(document, root / (NAME + '.exe'))
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--build-root',type=Path,required=True)
    parser.add_argument('--python',type=Path,required=True)
    parser.add_argument('--pe-version-file',type=Path,help='Optional explicit PyInstaller PE version input; pinned in plan')
    parser.add_argument('--build',action='store_true',help='Explicitly invoke compiler; default is read-only plan')
    args = parser.parse_args(argv)
    plan = plan_build(args.source_root,args.build_root,args.python,pe_version_file=args.pe_version_file)
    print(json.dumps(build(plan) if args.build else plan,ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ManagerBuildError,tools.BundleError,OSError,ValueError,KeyError,SyntaxError) as error:
        print('program manager build refused: '+str(error),file=sys.stderr)
        raise SystemExit(1)
