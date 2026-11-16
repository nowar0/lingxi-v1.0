"""Fixed minimal frozen worker entry; never imports host application configuration."""
from multiprocessing import freeze_support
import sys


def main():
    if sys.argv[1:] == ['--plugin-worker']:
        from app.plugin_sandbox_worker import main as worker_main
        return worker_main(['--plugin-worker'])
    if sys.argv[1:] == ['--plugin-stage-worker']:
        from app.plugin_stage_worker import main as stage_main
        return stage_main()
    if len(sys.argv) == 4 and sys.argv[1] == '--package-mcp-server':
        from app.plugin_mcp_sandbox_worker import main as mcp_main
        return mcp_main(sys.argv[1:])
    return 2


if __name__ == '__main__':
    freeze_support()
    raise SystemExit(main())
