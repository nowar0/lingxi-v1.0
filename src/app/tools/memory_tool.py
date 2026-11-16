"""记忆工具：让模型能自己往记忆库写一条事实。"""

from app.memory import store


def remember_fact(ctx, key, value):
    from app.memory import v2
    from app.db import connect
    with connect() as db:
        active = False
        if v2.ready(db):
            row = db.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
            active = bool(row and row[0] == 'v2')
    if active:
        if not ctx.get('batch_id') or not ctx.get('attempt_id'):
            raise ValueError('V2 自动记忆需要已持久化的新队列来源；请使用新聊天队列或四层记忆管理，未写入旧库')
        from app.memory import writer
        return writer.stage_candidate(ctx, key, value, 'fact')
    if ctx.get("schema_version") == 2:
        from app.stage5 import set_fact
        set_fact(key, value, ctx)
    else:
        store.set_fact(key, value, mode=ctx["mode"])
    return f"已记住：{key} = {value}（范围：{ctx['mode']}）"

TOOL = {
    "spec": {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "把关于用户的一条稳定信息写进长期记忆。"
                "只要用户说“记住…”“记一下…”，就必须调用本工具，不要只口头答应。"
                "起名字时【优先复用《长期事实》里已有的名字】"
                "（例如“喜欢的颜色”），不要为同一件事换个说法另起一个名字；"
                "只有确实是全新话题时，才新起一个简短的名字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "这条事实的名字，优先复用已有的"},
                    "value": {"type": "string", "description": "具体内容"},
                },
                "required": ["key", "value"],
            },
        },
    },
    "run": remember_fact,
    "write": True,
    "timeout": 5,
}
