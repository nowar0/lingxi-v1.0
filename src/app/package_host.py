"""Bind the independent plugin manager to the selected, isolated Lingxi data root.

No manager is constructed and no files are opened during module import. This is
important for first start, synthetic tests, and data-directory switches.
"""
from pathlib import Path
import sys
from threading import RLock

from fastapi import APIRouter

from app import config
from app.plugin_api import create_router
from app.plugin_runtime import PluginRuntime
from app.plugin_store import PluginManager

_lock = RLock()
_manager = None
_manager_root = None
_model_request_provider = None


def configure_model_request_provider(provider):
    """Trusted in-process composition only, before manager construction."""
    global _model_request_provider
    if not callable(provider):raise TypeError('host model request provider required')
    with _lock:
        if _manager is not None:raise RuntimeError('model provider must be configured before plugin host starts')
        _model_request_provider=provider



def _runtime_selection():
    """Personal edition: direct worker, no publisher trust or AppContainer setup."""
    command = [sys.executable, '--plugin-worker'] if getattr(sys, 'frozen', False) else None
    return PluginRuntime(worker_command=command)


def initialize():
    global _manager, _manager_root
    root = (config.DATA_DIR / 'plugin-packages').resolve()
    with _lock:
        if _manager is not None and _manager_root == root:
            return _manager
        if _manager is not None:
            _manager.close()
        manager = PluginManager(root, runtime=_runtime_selection(), personal_use=True)
        try:
            manager.initialize()
        except BaseException:
            manager.close()
            raise
        _manager, _manager_root = manager, root
        return manager


def shutdown():
    global _manager, _manager_root
    with _lock:
        if _manager is not None:
            _manager.close()
        _manager, _manager_root = None, None


class ManagerProxy:
    def __getattr__(self, name):
        return getattr(initialize(), name)


router = APIRouter()
router.include_router(create_router(ManagerProxy()))
metadata_router = APIRouter(prefix='/v11/plugin-host', tags=['plugin-host'])


@metadata_router.get('/sources')
def sources():
    source_root = Path(__file__).resolve().parent.parent
    candidates = [source_root / 'plugin-packages', source_root.parent / 'plugins']
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        release_root = executable.parent.parent.parent.parent
        candidates.extend([release_root / 'plugins', release_root / 'plugin-packages'])
    paths = list(dict.fromkeys(str(path) for path in candidates if path.is_dir()))
    return {'directories': paths, 'auto_scan': False, 'auto_install': False,
            'data_root': str(config.DATA_DIR.resolve()),
            'edition': 'personal', 'publisher_trust_required': False,
            'note': '本地个人插件；无需发布者信任或沙箱设置。保留安装、启停与运行记录。'}
