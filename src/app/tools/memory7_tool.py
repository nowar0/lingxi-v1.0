"""阶段7候选与主动历史查询，范围由服务端上下文确定。"""

import asyncio
import json

from app.memory.writer import stage_candidate


def propose(ctx, key, value, category, occurred_at=None):
    return stage_candidate(ctx, key, value, category, occurred_at)


def search(ctx, query):
    from app.memory.search import search as find
    from app.memory import recent_rag, stage7

    if ctx.get("mode") not in ("work", "companion"):
        raise ValueError("需要有效聊天范围")
    result = asyncio.run(find(query, ctx["mode"], ctx.get("session_id"), limit=5))
    historical = recent_rag.tool_search(query, ctx, limit=3)
    result["history"] = historical
    # 工具结果完整JSON且有界；完整正文可通过ID在记忆页读取。
    result["items"] = [
        {
            k: i[k]
            for k in ("id", "version_id", "scope", "key", "value", "occurred_at", "match_methods")
        }
        for i in result["items"]
    ]
    for item in result["items"]:
        if len(item["value"]) > 300:
            item["value"] = item["value"][:300] + "（节选；完整内容在记忆页）"
    # Retain source metadata while respecting the tool reply reserve.
    cap = max(1024, min(8000, stage7.settings()["tool_reserve_tokens"]))
    for item in result["history"]["items"]:
        item["value"] = item["value"][:240]
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > cap:
        if result["items"]:
            result["items"].pop()
        elif result["history"]["items"]:
            result["history"]["items"].pop()
        else:
            break
    return json.dumps(result, ensure_ascii=False)


PROPOSE = {
    "spec": {
        "type": "function",
        "function": {
            "name": "propose_memory",
            "description": "当用户明确确认稳定事实、偏好、事件或约定时主动提出候选。不是立即生效；不能把猜测、助手建议、临时情绪或角色扮演写成用户事实。不要保存密钥。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "稳定名称，同一事项沿用同一名称"},
                    "value": {"type": "string", "description": "保持否定、人物与时间的简短内容"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "fact",
                            "preference",
                            "event",
                            "task",
                            "communication_preference",
                            "writing_preference",
                            "habit",
                            "project",
                        ],
                    },
                    "occurred_at": {
                        "type": "string",
                        "description": "只有用户明确说明的发生时间才填写ISO日期；不知道则省略，不能编造",
                    },
                },
                "required": ["key", "value", "category"],
                "additionalProperties": False,
            },
        },
    },
    "run": propose,
    "write": True,
    "staged_memory": True,
    "timeout": 5,
}
SEARCH = {
    "spec": {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "近期聊天已作为原文上下文携带。需要回忆更早的人、事件或约定时，查询有效记忆与近期窗口之外的历史原文，使用本地离线向量。返回来源、角色、时间；助手历史回复不是用户已确认事实。检索失败不能猜答案。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    "run": search,
    "write": False,
    "timeout": 120,
}
