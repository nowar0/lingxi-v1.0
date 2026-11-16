"""Dedicated frozen program-manager entry; never starts Lingxi's chat/server mode."""
from multiprocessing import freeze_support
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--rehearse':
        from app.program_manager_rehearsal import main as rehearsal_main
        return rehearsal_main(sys.argv[2:])
    if '--rehearse' in sys.argv:
        raise SystemExit('--rehearse 必须为第一个固定入口参数；未执行其他管理操作')
    if len(sys.argv) > 1 and sys.argv[1] == '--ui':
        from app.program_manager_ui import main as ui_main
        return ui_main(sys.argv[2:])
    from app.program_manager import main as cli_main
    return cli_main(sys.argv[1:])


if __name__ == '__main__':
    freeze_support()
    raise SystemExit(main())
