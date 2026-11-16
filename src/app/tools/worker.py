"""只供工具执行器调用；输入经标准输入传递，不拼接 shell 命令。"""

import json
import sys

from app.tools import BY_NAME


def main():
    try:
        data = json.loads(sys.stdin.buffer.read())
        tool = BY_NAME[data["name"]]
        result = tool["run"](data["ctx"], **data["args"])
        reply = {"ok": True, "result": str(result)}
    except Exception as exc:
        reply = {"ok": False, "result": str(exc)}
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
