"""Local launcher: bind loopback, reserve a port, open only after readiness."""
import argparse
import socket
import threading
import time
import urllib.request
import webbrowser
import sys
import multiprocessing
import signal
from pathlib import Path
from contextlib import contextmanager


@contextmanager
def server_signals(server):
    """Handle Windows CTRL_BREAK without the CRT abort / signal replay."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, 'SIGBREAK'):
        signals.append(signal.SIGBREAK)
    previous = {sig: signal.signal(sig, server.handle_exit) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

@contextmanager
def data_lock():
    from app.config import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / '.runtime.lock').open('a+b') as lock:
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
        lock.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('此数据目录已有零汐实例运行，请先退出旧实例') from exc
        try:
            yield
        finally:
            if sys.platform == 'win32':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    if sys.argv[1:] == ['--office-worker']:
        from app.office_worker import main as office_main
        office_main()
        return
    if '--speech-worker' in sys.argv:
        from app.static.workers.speech_worker import main as speech_main
        speech_main()
        return
    if '--team-runtime-check' in sys.argv:
        import json
        import subprocess
        from app.team_runtime import status
        from app.subteams import interpreter
        report = {'agno': status()}
        from agno.team import Team
        report['agno']['imported'] = Team is not None
        for engine, package in [('agentscope', 'agentscope'), ('camel', 'camel')]:
            checked = subprocess.run([str(interpreter(engine)), '-I', '-B', '-c',
                f'import {package}; print("ready")'], capture_output=True, text=True,
                timeout=45, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            report[engine] = {'available': checked.returncode == 0}
        print(json.dumps(report))
        return
    from app.model_pdf_extract import dispatch_pdf_worker
    dispatch_pdf_worker()
    if len(sys.argv) > 1 and sys.argv[1] == '--program-manager-ui':
        from app.program_manager_ui import main as program_manager_ui_main
        raise SystemExit(program_manager_ui_main(sys.argv[2:]))
    if '--program-manager-ui' in sys.argv:
        raise SystemExit('--program-manager-ui 必须为第一个固定入口参数；未启动普通服务')
    if len(sys.argv) > 1 and sys.argv[1] == '--program-manager':
        from app.program_manager import main as program_manager_main
        raise SystemExit(program_manager_main(sys.argv[2:]))
    if '--program-manager' in sys.argv:
        raise SystemExit('--program-manager 必须为第一个固定入口参数；未启动服务')
    if '--package-mcp-server' in sys.argv:
        from app.package_mcp_worker import dispatch_server
        dispatch_server(sys.argv[sys.argv.index('--package-mcp-server') + 1:])
        return
    if '--package-mcp-worker' in sys.argv:
        from app.package_mcp_worker import main as package_mcp_worker_main
        package_mcp_worker_main()
        return
    if '--plugin-worker' in sys.argv:
        from app.plugin_worker import main as plugin_worker_main
        plugin_worker_main()
        return
    if '--tool-worker' in sys.argv:
        from app.tools.worker import main as worker_main
        worker_main()
        return
    parser = argparse.ArgumentParser(description='零汐本地服务；由桌面程序启动，Ctrl+C 安全退出')
    parser.add_argument('--port', type=int, default=0, help='0 自动选择空闲端口')
    # 打包成桌面程序后由外层窗口显示页面，不再另开浏览器。
    browser_mode=parser.add_mutually_exclusive_group()
    browser_mode.add_argument('--no-browser',dest='no_browser',action='store_true')
    browser_mode.add_argument('--browser',dest='no_browser',action='store_false',help='明确使用动态端口的已认证浏览器入口')
    parser.set_defaults(no_browser=bool(getattr(sys,'frozen',False)))
    parser.add_argument('--set-key', metavar='REFERENCE', help='在系统凭据管理器保存密钥，不回显')
    parser.add_argument('--backup', type=Path, metavar='ZIP', help='服务退出后导出业务备份')
    parser.add_argument('--restore', type=Path, metavar='ZIP', help='服务退出后恢复到新目录')
    parser.add_argument('--destination', type=Path, help='恢复的新目录')
    args = parser.parse_args()
    if args.set_key:
        import getpass
        from app.config import save_key
        key = getpass.getpass('请输入密钥（不会显示）：')
        if not key:
            parser.error('密钥不能为空')
        save_key(args.set_key, key)
        print('凭据已保存。')
        return
    if args.backup or args.restore:
        import json
        from app.release import export_backup, restore_backup
        if args.restore and not args.destination:
            parser.error('恢复需指定 --destination 新目录')
        if args.backup:
            with data_lock():
                result = export_backup(args.backup)
        else:
            result = restore_backup(args.restore, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    import os
    import secrets
    ui_bootstrap=os.environ.get('LINGXI_UI_BOOTSTRAP') or secrets.token_urlsafe(32)
    os.environ['LINGXI_UI_BOOTSTRAP']=ui_bootstrap
    import uvicorn
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Bind once, pass the reserved socket to uvicorn; no check/use race.
    try:
        sock.bind(('127.0.0.1', args.port))
        sock.listen(128)
        port = sock.getsockname()[1]
        class LocalServer(uvicorn.Server):
            def capture_signals(self):
                return server_signals(self)
        server = LocalServer(uvicorn.Config('app.main:app', host='127.0.0.1', port=port, timeout_graceful_shutdown=3))
        from app.main import app as application
        application.state.request_shutdown = lambda: setattr(server, 'should_exit', True)
        url = f'http://127.0.0.1:{port}'

        def open_when_ready():
            for _ in range(200):
                if server.should_exit:
                    return
                if server.started:
                    try:
                        with urllib.request.urlopen(url + '/health', timeout=2) as response:
                            if response.status == 200:
                                webbrowser.open(url + '/ui/#lingxi-bootstrap=' + ui_bootstrap)
                                return
                    except OSError:
                        pass
                time.sleep(0.1)

        if not args.no_browser:
            threading.Thread(target=open_when_ready, daemon=True).start()
        print(f'零汐：{url}/ui/，关闭此进程即可退出。')
        with data_lock():
            server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
