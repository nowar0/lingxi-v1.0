# -*- mode: python ; coding: utf-8 -*-
"""V1.0 Electron backend collector; build_v1.py supplies an isolated context.

Context is a fresh run-owned JSON, never an existing candidate or installation.
The builder checks every delivered media/native byte and frozen static afterwards.
"""
import json
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import (
    collect_all, collect_data_files, collect_submodules, copy_metadata,
)

context = json.loads(Path(os.environ['LINGXI_BACKEND_CONTEXT']).read_text(encoding='utf-8'))
import tomllib
assert context['version'] == tomllib.loads(Path(context['source'], 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
source = Path(context['source'])
assert source.is_absolute() and (source / 'launch.py').is_file()
# Hook collectors run before Analysis adds pathex; resolve app ONLY from this
# run-owned compiler workspace, never an installed/working-directory package.
sys.path.insert(0, str(source))

datas = [
    (str(source / 'app/static'), 'app/static'),
    (str(source / 'config'), 'config'),
    (context['program_tools'], 'program-tools'),
]
# Include non-code app resources without shipping a loose development source tree.
for path in sorted((source / 'app').rglob('*')):
    if not path.is_file() or 'static' in path.relative_to(source / 'app').parts:
        continue
    if '__pycache__' in path.parts or path.suffix.lower() in {'.py', '.pyc', '.pyo'}:
        continue
    datas.append((str(path), path.parent.relative_to(source).as_posix()))
datas += [(str(Path(context['runtime']) / 'team-runtimes'), 'team-runtimes'),
          (str(source / 'app/subteam_worker.py'), 'app')]
binaries = []
hiddenimports = ['win32timezone', 'tomllib', 'zipfile', 'sqlite3', 'ctypes',
                 'shutil', 'stat', 'argparse', 'datetime', 'hashlib', 'secrets',
                 'tempfile', 'uuid', 'uvicorn.logging', 'uvicorn.loops.asyncio',
                 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto',
                 'uvicorn.lifespan.on', 'anyio._backends._asyncio']
datas += copy_metadata('sqlite-vec') + copy_metadata('sherpa-onnx') + copy_metadata('sherpa-onnx-core') + copy_metadata('pywinpty')
excludes = []
hiddenimports += collect_submodules('app')
hiddenimports += collect_submodules('asyncssh')
hiddenimports += collect_submodules('mcp.client')
hiddenimports += collect_submodules('keyring.backends')
datas += collect_data_files('mcp')
for package in ('playwright', 'onnxruntime', 'tokenizers', 'sqlite_vec', 'sherpa_onnx', 'winpty'):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden

# Consume the existing media helper's actual argument contract, not a stale copy.
arguments = iter(context['media_arguments'])
noupx = False
for option in arguments:
    if option == '--noupx':
        noupx = True
    elif option == '--add-data':
        origin, target = next(arguments).rsplit(os.pathsep, 1)
        assert target == 'media-tools' and Path(origin) == Path(context['media_bundle'])
        datas.append((origin, target))
    elif option == '--hidden-import':
        hiddenimports.append(next(arguments))
    elif option == '--copy-metadata':
        datas += copy_metadata(next(arguments))
    elif option == '--collect-submodules':
        hiddenimports += collect_submodules(next(arguments))
    elif option == '--exclude-module':
        excludes.append(next(arguments))
    else:
        raise ValueError('Unreviewed media PyInstaller option: ' + option)
assert noupx, 'Native SHA pins forbid UPX'
# Pillow's hook can otherwise auto-collect every available optional codec, which
# would outrun the reviewed JPEG/WebP native subset. Do not freeze those extras.
excludes += ['PIL.ImageCms', 'PIL._imagingcms', 'PIL.ImageMorph', 'PIL._imagingmorph']

a = Analysis(
    [str(source / 'launch.py')], pathex=[str(source)], binaries=binaries,
    datas=datas, hiddenimports=sorted(set(hiddenimports)), hookspath=[],
    hooksconfig={}, runtime_hooks=[], excludes=sorted(set(excludes)),
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='LingxiServer',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=str(source / 'packaging/icon/lingxi.ico'), version=context['pe_version_file'],
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               upx_exclude=[], name='LingxiServer')
