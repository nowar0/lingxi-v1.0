"""Select the desktop bundle's Node runtime for Playwright's driver.

The Electron build reuses Lingxi.exe. The WebView2 build carries a standalone
node.exe; its driver must not receive ELECTRON_RUN_AS_NODE.
"""
from pathlib import Path
import sys
from threading import Lock

_lock = Lock()
_configured = False


def configure_playwright_driver() -> None:
    global _configured
    if not getattr(sys, 'frozen', False):
        return
    internal = Path(sys._MEIPASS).resolve(strict=True)
    shell = internal.parents[2]
    standalone = internal / 'playwright' / 'driver' / 'node.exe'
    external_node = shell / 'node.exe'
    electron = shell / 'Lingxi.exe'
    if standalone.is_file():
        runtime, is_electron = standalone, False
    elif external_node.is_file():
        runtime, is_electron = external_node, False
    elif electron.is_file():
        runtime, is_electron = electron, True
    else:
        raise RuntimeError('零汐浏览器运行组件缺失')
    with _lock:
        if _configured:
            return
        from playwright._impl import _transport
        original_executable = _transport.compute_driver_executable
        original_env = _transport.get_driver_env

        def driver_executable():
            _, entrypoint = original_executable()
            return str(runtime), entrypoint

        def driver_env():
            env = original_env()
            if is_electron:
                env['ELECTRON_RUN_AS_NODE'] = '1'
            else:
                env.pop('ELECTRON_RUN_AS_NODE', None)
            return env

        _transport.compute_driver_executable = driver_executable
        _transport.get_driver_env = driver_env
        _configured = True
