"""Development-only worker launcher with explicit existing host dependency paths."""
import json
import os
from pathlib import Path
import runpy
import sys

mode=sys.argv[1]
if mode not in {'plugin_worker','package_mcp_worker'}:
    raise SystemExit(2)
paths=json.loads(sys.argv[2])
for value in reversed(paths):
    path=Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise SystemExit(2)
    sys.path.insert(0,str(path))
_dll_paths=[]
if os.name=='nt':
    for value in paths:
        path=Path(value)
        if path.name=='pywin32_system32':
            _dll_paths.append(os.add_dll_directory(str(path)))
sys.argv=[mode,*sys.argv[3:]]
runpy.run_module('app.'+mode,run_name='__main__')
