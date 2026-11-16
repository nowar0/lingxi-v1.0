"""Assemble relocatable Windows Python SDK runtimes without host credentials."""
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
BASE = Path(sys.base_prefix)
DEST = ROOT / 'team-runtimes'
DEST.mkdir(exist_ok=True)
stdlib = DEST / 'python314.zip'
with zipfile.ZipFile(stdlib, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in (BASE / 'Lib').rglob('*'):
        relative = path.relative_to(BASE / 'Lib')
        if path.is_file() and not {'site-packages', '__pycache__', 'test', 'tests', 'tkinter', 'idlelib'} & set(relative.parts):
            archive.write(path, relative.as_posix())
for engine, env in [('agno', 'integration-env'), ('agentscope', 'agentscope-env'), ('camel', 'camel-env')]:
    target = DEST / engine
    target.mkdir(exist_ok=True)
    shutil.copy2(BASE / 'LICENSE.txt', target / 'PYTHON-LICENSE.txt')
    for path in BASE.iterdir():
        if path.name == 'python.exe' or path.suffix.lower() == '.dll':
            shutil.copy2(path, target / path.name)
    shutil.copytree(BASE / 'DLLs', target / 'DLLs', dirs_exist_ok=True)
    shutil.copy2(stdlib, target / stdlib.name)
    shutil.copytree(ROOT.parent / env / 'Lib/site-packages', target / 'Lib/site-packages',
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    cache = ROOT.parent / env / 'tiktoken-cache'
    if cache.is_dir():
        shutil.copytree(cache, target / 'tiktoken-cache', dirs_exist_ok=True)
    (target / 'python314._pth').write_text('python314.zip\nDLLs\nLib/site-packages\n.\nimport site\n', encoding='utf-8')
    print(engine, target)
