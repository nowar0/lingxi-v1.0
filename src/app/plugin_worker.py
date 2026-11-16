"""One-shot JSON worker. Run by a trusted Python with -I -u, never as an app.

Process separation is NOT an OS sandbox: imported Python can access the user's OS.
There is intentionally no host RPC, credential access helper, shell, or pip service.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path
import sys

MAX_INPUT = 1_048_576
MAX_OUTPUT = 262_144


def main() -> int:
    request_id = ""
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        # The parent only writes after the process has joined its cleanup Job/group.
        raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT or not raw.endswith(b"\n"):
            raise ValueError("input limit")
        envelope = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
        request_id = envelope.get("request_id", "")
        if envelope.get("protocol") != 1 or not isinstance(request_id, str):
            raise ValueError("protocol")
        root = Path(envelope["package_root"]).resolve(strict=True)
        entry = envelope["entrypoint"]
        path = entry["path"]
        if not isinstance(path, str) or "\\" in path or any(x in ("", ".", "..") for x in path.split("/")):
            raise ValueError("entrypoint")
        target = root.joinpath(*path.split("/"))
        if target.is_symlink() or not target.resolve(strict=True).is_relative_to(root):
            raise ValueError("entrypoint")
        request = {key: envelope[key] for key in (
            "protocol", "request_id", "tool", "args", "config", "state", "data_dir"
        )}
        # Root imports permit bundled modules, but -I and a scrubbed environment avoid
        # accidentally inheriting the host's code/search path or API credentials.
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root))
        with contextlib.redirect_stdout(sys.stderr):
            spec = importlib.util.spec_from_file_location("_lingxi_plugin_entry", target)
            if spec is None or spec.loader is None:
                raise ValueError("entrypoint")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            handler = getattr(module, entry["callable"])
            if not callable(handler):
                raise ValueError("entrypoint")
            result = handler(request)
        response = {"protocol": 1, "request_id": request_id, "ok": True, "result": result}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_OUTPUT:
            raise ValueError("output limit")
    except BaseException:
        # Do not serialize exception text/tracebacks, which may disclose file contents.
        encoded = json.dumps({"protocol": 1, "request_id": request_id, "ok": False,
                              "error": {"code": "plugin_exception", "message": "插件执行或协议失败"}},
                             ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
