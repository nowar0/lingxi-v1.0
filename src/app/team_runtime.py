"""Runtime discovery for the explicitly provisioned team engine."""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

AGNO_VERSION = '3.1.12'


def status():
    from app.config import BASE_DIR
    candidates = [Path(BASE_DIR) / 'team-runtimes' / 'agno' / 'Lib' / 'site-packages',
                  Path(BASE_DIR).parent / 'integration-env' / 'Lib' / 'site-packages']
    for candidate in candidates:
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.append(str(candidate))
    try:
        installed = version('agno')
    except PackageNotFoundError:
        installed = None
    return {'engine': 'agno', 'version': installed, 'required_version': AGNO_VERSION,
            'available': installed == AGNO_VERSION}


def require():
    from fastapi import HTTPException
    if not status()['available']:
        raise HTTPException(409, 'Agno 团队运行环境尚未就绪，请安装随包提供的固定版本依赖')
