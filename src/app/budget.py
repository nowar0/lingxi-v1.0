"""按字符限制上下文，保留最近的完整轮次；不是精确 token 计数。"""

MAX_USER_CHARS = 8000
MAX_CONTEXT_CHARS = 24000
MAX_HISTORY_TURNS = 12
MAX_SYSTEM_CHARS = 10000


def stage7_budget(messages, tool_specs, cfg):
    import json
    from app.memory.search import estimated_tokens
    # 包括工具定义和消息协议余量；这仍是估算，不冒充提供方Tokenizer。
    size = estimated_tokens(json.dumps(messages,ensure_ascii=False))
    size += estimated_tokens(json.dumps(tool_specs,ensure_ascii=False)) + 512
    tool_used = sum(estimated_tokens(json.dumps(m, ensure_ascii=False)) for m in messages if m.get('role') == 'tool')
    unused_tool_reserve = max(0, cfg['tool_reserve_tokens'] - tool_used)
    available = cfg['context_tokens'] - cfg['reply_reserve_tokens'] - unused_tool_reserve
    if size>available:
        raise ValueError('上下文估算超过配置容量；请先整理，或调小近期保留轮数/缩短原文/校正模型容量。未静默删除未整理历史。')
    return {'estimated_input_tokens':size,'available_input_tokens':available,
            'counter':'UTF-8字节保守估算，非模型Tokenizer'}


def tool_content(result, limit=4000):
    """Bound the model copy; preserve valid JSON and keep the UI's full evidence."""
    import json
    raw = str(result)
    if len(raw.encode('utf-8')) <= limit:
        return raw
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return raw.encode('utf-8')[:max(0, limit - 60)].decode('utf-8', errors='ignore') + '\n（节选；完整结果见工具记录）'
    original_bytes = len(raw.encode('utf-8'))
    value = {**value, '_truncated': True, '_original_bytes': original_bytes} if isinstance(value, dict) else {
        'data': value, '_truncated': True, '_original_bytes': original_bytes}
    def lists(item):
        if isinstance(item, dict):
            for child in item.values():
                yield from lists(child)
        elif isinstance(item, list):
            if item:
                yield item
    while True:
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode('utf-8')) <= limit:
            return encoded
        candidates = list(lists(value))
        if candidates:
            max(candidates, key=lambda v: len(json.dumps(v, ensure_ascii=False))).pop()
            continue
        # Non-list objects cannot be cut mid-JSON. The preview is explicitly
        # a string, not partially parsed data or a claim that fields are complete.
        preview = raw.encode('utf-8')[:max(0, limit // 2)].decode('utf-8', errors='ignore')
        fallback = {'_truncated': True, '_original_bytes': original_bytes, 'preview': preview}
        while len(json.dumps(fallback, ensure_ascii=False).encode('utf-8')) > limit and fallback['preview']:
            fallback['preview'] = fallback['preview'][:len(fallback['preview']) // 2]
        return json.dumps(fallback, ensure_ascii=False)


def build_stage7(system, history, text, tool_specs, cfg):
    messages = [{'role':'system','content':system}]
    messages.extend({'role':m['role'],'content':m['content'] or ''} for m in history)
    messages.append({'role':'user','content':text})
    stage7_budget(messages,tool_specs,cfg)
    return messages


def build_messages(system, history, text):
    if len(system) > MAX_SYSTEM_CHARS:
        raise ValueError("人设和记忆过长，请缩短人设后重试")
    remaining = MAX_CONTEXT_CHARS - len(system) - len(text)
    pairs = []
    for i in range(len(history) - 1):
        if history[i]["role"] == "user" and history[i + 1]["role"] == "assistant":
            pairs.append([
                {"role": m["role"], "content": m["content"] or ""}
                for m in history[i:i + 2]
            ])
    selected = []
    for pair in reversed(pairs[-MAX_HISTORY_TURNS:]):
        size = sum(len(m["content"]) for m in pair)
        if size > remaining:
            break
        selected.insert(0, pair)
        remaining -= size
    return ([{"role": "system", "content": system}]
            + [m for pair in selected for m in pair]
            + [{"role": "user", "content": text}])
