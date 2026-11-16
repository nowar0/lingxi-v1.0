"""基础工具：先放一个"查时间"。"""

from datetime import datetime


def now_text(ctx):
    # ctx 用不上，但调用约定要求每个工具先收下它（见 tools/__init__.py 顶部）
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


TOOL = {
    # 给模型看的卡片
    "spec": {
        "type": "function",
        "function": {
            "name": "now_text",
            "description": "查看当前的日期和时间。用户问“现在几点”“今天几号”时使用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "run": now_text,   # 真正干活的函数
    "write": False,    # False = 只读，不改任何数据
    "timeout": 5,      # 最多等 5 秒
}
