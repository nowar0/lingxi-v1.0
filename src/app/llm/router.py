"""模型调用层：把「角色名」翻译成「某家的某个模型」，然后发请求。

这一层是唯一知道 base_url / API key 的地方。
以后换厂商、加厂商，都只动这里和 models.yaml，别处不用改。
"""

from openai import AsyncOpenAI
from contextvars import ContextVar
from urllib.parse import urlsplit

from app.config import load_key, load_models, think_enabled

output_limit = ContextVar('lingxi_output_limit',default=None)
# 推理强度档位：由对话入口按当前设置写入，供 extra_body 使用。
effort_level = ContextVar('lingxi_effort', default=None)
review_route = ContextVar('lingxi_approved_review_route', default=None)
attempt_route = ContextVar('lingxi_attempt_route', default=None)
last_generation_route = ContextVar('lingxi_last_generation_route', default=None)


def list_models():
    """列出所有可用的模型全名，返回 ['deepseek/deepseek-flash', ...]"""
    cfg = load_models()
    names = []
    for provider in cfg["providers"]:
        for model in provider["models"]:
            names.append(f"{provider['id']}/{model['id']}")
    from app.provider_failover import profile_routes
    names.extend('profile_' + item['id'] + '/' + item['model'] for item in profile_routes())
    return names


def list_roles():
    """列出角色 → 模型的对照表"""
    roles = dict(load_models()["roles"])
    for item in module_models():
        roles[item['id']] = 'module_' + item['id'] + '/' + item['model']
    return roles


def module_models():
    from app import catalog, db
    import sqlite3
    if not db.DB_PATH.is_file():
        return []
    try:
        return [item for item in catalog.listing('module')
                if item['enabled'] and 'text' in item['capabilities']
                and item['base_url'] and item['model']]
    except sqlite3.OperationalError:
        return []


def resolve(role_or_model):
    """把 'chat' 这种角色名（或 'deepseek/deepseek-flash' 这种全名）
    翻译成 (厂商配置, 模型 id)
    """
    pinned = review_route.get()
    if pinned and role_or_model == pinned['role']:
        return dict(pinned['provider']), pinned['model']
    cfg = load_models()

    # 是角色名就查表；不是的话，就当模型全名直接使用
    target = list_roles().get(role_or_model, role_or_model)

    if "/" not in target:
        raise ValueError(
            f"不认识 '{role_or_model}'。"
            f"可用的角色：{sorted(cfg['roles'])}；"
            f"或者写全名，当前可用：{list_models()}"
        )

    provider_id, model_id = target.split("/", 1)
    if provider_id.startswith('profile_'):
        from app.provider_failover import resolve_target
        return resolve_target(target)
    for item in module_models():
        if provider_id == 'module_' + item['id'] and model_id == item['model']:
            from app import reasoning
            capability = reasoning.for_module(item)
            return {'id': provider_id, 'base_url': item['base_url'],
                    'credential_ref': item['credential_ref'], 'local': item['local'],
                    'temperature': item.get('temperature', 0.7),
                    'timeout': item.get('timeout', 90),
                    'top_p':item.get('top_p'),'stop':item.get('stop',[]),'max_output_tokens':item.get('max_output_tokens'),
                    'reasoning_effort': item.get('reasoning_effort', 'auto'),
                    'reasoning': capability,
                    'protocol': item.get('protocol', 'openai'),
                    # Preserve reasoning control for model-library assignments too.
                    # Only the verified official host gets implicit support.
                    'supports_effort': item.get('supports_effort',
                        urlsplit(item['base_url']).hostname == 'api.deepseek.com')}, model_id
    for provider in cfg["providers"]:
        if provider["id"] == provider_id:
            return provider, model_id

    raise ValueError(
        f"models.yaml 里没有 id 叫 '{provider_id}' 的厂商。"
        f"现在有的：{[p['id'] for p in cfg['providers']]}"
    )


def freeze_route(role):
    """Safe immutable routing metadata; secret values remain in the keyring."""
    import copy
    provider, model = resolve(role)
    fields = ('id', 'base_url', 'credential_ref', 'local', 'temperature', 'timeout',
              'protocol', 'reasoning', 'reasoning_effort', 'supports_effort','top_p','stop','max_output_tokens')
    safe = {key: copy.deepcopy(provider[key]) for key in fields if key in provider}
    safe['legacy_think'] = provider.get('legacy_think', think_enabled(role))
    return {'role': role, 'model': model, 'provider': safe}


def full_name(role_or_model):
    """把角色名翻译成模型全名，例如 'chat' -> 'deepseek/deepseek-flash'。

    已经写全名的，原样返回。认不出来时抛 ValueError（和 resolve 一样）。
    """
    provider, model_id = resolve(role_or_model)
    return f"{provider['id']}/{model_id}"


def generation_full_name(role_or_model):
    """Accounting uses the actual attempted route without changing role resolution."""
    actual = last_generation_route.get()
    return actual[1] if actual and actual[0] == role_or_model else full_name(role_or_model)


def _thinking_off(role_or_model):
    """需要"想"的角色返回 None（不加任何参数）；不需要想的返回关掉思考的参数。

    thinking={"type": "disabled"} 是探针实测唯一两家都认的写法(2026-09-29)。
    ⚠️ 厂商对不认识的参数是【静默忽略】：不报错，只是没生效。
       所以加新厂商/新模型前，先重跑一遍探针。
    """
    if think_enabled(role_or_model):
        return None
    return {"thinking": {"type": "disabled"}}


def _effort_body(role_or_model, provider):
    """推理强度参数。

    探针实测（deepseek-flash）：reasoning_effort 接受
    none/minimal/low/medium/high/max，推理量依次递增（0 → 85 → 108 → 169 → 227 → 546 token）；
    'none' 会走非思考模式，与 thinking={"type":"disabled"} 效果一致。
    不支持的厂商仍按原来的 thinking 开关走，避免把未知参数发给别家。
    """
    from app import reasoning
    effort = effort_level.get()
    if effort in (None, 'auto'):
        effort = provider.get('reasoning_effort', 'auto')
    capability = provider.get('reasoning')
    if capability is None:
        try: _, model = attempt_route.get() or resolve(role_or_model)
        except ValueError: model = ''
        capability = reasoning.describe(model, provider, provider.get('supports_effort', False))
    if effort in (None, 'auto') and not provider.get('legacy_think', think_enabled(role_or_model)):
        if 'none' in capability.get('levels', []):
            effort = 'none'
        elif capability.get('levels'):
            raise ValueError('此模型不支持关闭思考；请更改原思考开关或明确选择支持的强度')
    if capability.get('levels'):
        if effort not in (None,'auto') and reasoning.coerce(effort,capability)=='auto':
            effort=provider.get('reasoning_effort','auto')
        return reasoning.parameters(effort, capability,provider.get('protocol','openai'))
    if effort and provider.get('supports_effort'):
        # Backward compatibility with explicit provider declarations.
        return {"reasoning_effort": effort} if effort != 'auto' else {}
    if 'legacy_think' in provider:
        return None if provider['legacy_think'] else {'thinking': {'type': 'disabled'}}
    return _thinking_off(role_or_model)


async def _chat_stream(role_or_model, messages, tools=None):
    """流式读取文字与工具参数碎片，结束时给出完整的 assistant 消息。"""
    provider, model_id = attempt_route.get() or resolve(role_or_model)
    from app.session_options import temperature as session_temperature
    if session_temperature.get() is not None and review_route.get() is None:
        provider = {**provider, 'temperature': session_temperature.get()}
    last_generation_route.set((role_or_model, f"{provider['id']}/{model_id}"))
    key = (load_key(provider.get('credential_ref') or provider['id'])
           if not (provider.get('local') and not provider.get('credential_ref')) else 'local-no-key')
    if not key:
        raise RuntimeError(f"没找到 {provider['id']} 的密钥，请先运行 set_key.py 配置")
    kwargs = {"tools": tools} if tools else {}
    from app import reasoning as effort_metadata
    extra = _effort_body(role_or_model, provider)
    capability = provider.get('reasoning') or effort_metadata.describe(model_id, provider, provider.get('supports_effort', False))
    openai_reasoning = capability.get('family') == 'openai' and effort_metadata.openai_model(model_id)
    from app.generation_settings import request_parameters
    sampling=request_parameters(provider, openai_reasoning)
    output_max=provider.get('max_output_tokens')
    limit=min(output_limit.get(),output_max) if output_limit.get() and output_max else output_limit.get() or output_max
    from app.context_capacity import describe as context_capacity
    from app.memory.search import estimated_tokens
    import json
    from app.memory import stage7
    window = context_capacity(provider, model_id, stage7.settings()['context_tokens'])
    limit = min(limit or stage7.settings()['reply_reserve_tokens'], max(256, window['tokens'] // 4))
    input_size = estimated_tokens(json.dumps(messages, ensure_ascii=False)) + estimated_tokens(json.dumps(tools or [], ensure_ascii=False)) + 512
    if input_size + (limit or 4096) > window['tokens']:
        raise ValueError('当前实际模型的上下文容量不足；请整理上下文或校正模型容量。请求尚未发送。')
    if provider.get('protocol') == 'anthropic':
        from app.llm import anthropic_bridge
        from contextlib import aclosing
        async with aclosing(anthropic_bridge.stream(provider, key, model_id, messages, tools, extra or {}, limit)) as events:
            async for event in events: yield event
        return
    if 'temperature' in provider and capability.get('family') not in ('gemini','kimi') and (not openai_reasoning or (extra or {}).get('reasoning_effort') == 'none'):
        kwargs['temperature'] = provider['temperature']
    kwargs.update(sampling)
    if limit is not None:
        kwargs['max_completion_tokens' if openai_reasoning else 'max_tokens'] = limit
    async with AsyncOpenAI(api_key=key, base_url=provider["base_url"],
                           timeout=float(provider.get('timeout', 90)), max_retries=0) as client:
        from app.llm import responses_bridge
        if responses_bridge.required(model_id, tools, (extra or {}).get('reasoning_effort')):
            from contextlib import aclosing
            async with aclosing(responses_bridge.stream(client, model_id, messages, tools,
                    (extra or {}).get('reasoning_effort'), limit)) as events:
                async for event in events:
                    yield event
            return
        stream = await client.chat.completions.create(
            model=model_id, messages=messages, stream=True,
            stream_options={"include_usage": True},
            extra_body=extra, **kwargs)
        content, reasoning, calls = [], [], {}
        finish = None
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    yield "usage", chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish = choice.finish_reason or finish
                delta = choice.delta
                think = getattr(delta, "reasoning_content", None)
                if think:
                    reasoning.append(think)
                    yield "think", think
                if delta.content:
                    content.append(delta.content)
                    yield "answer", delta.content
                for fragment in delta.tool_calls or []:
                    call = calls.setdefault(fragment.index, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
                    if fragment.id:
                        call["id"] = fragment.id
                    if fragment.function:
                        call["function"]["name"] += fragment.function.name or ""
                        call["function"]["arguments"] += fragment.function.arguments or ""
                    if len(call["function"]["arguments"]) > 12000 or len(calls) > 8:
                        raise RuntimeError("工具参数或工具数量超过限制")
        finally:
            await stream.close()
        if finish not in ("stop", "tool_calls"):
            raise RuntimeError(f"模型没有正常完成回复（{finish or '连接中断'}），本轮未保存")
        if finish == "tool_calls" and not calls:
            raise RuntimeError("模型返回了不完整的工具调用")
        message = {"role": "assistant", "content": "".join(content) or None}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
            if any(not c["id"] or not c["function"]["name"] for c in calls.values()):
                raise RuntimeError("工具调用缺少名称或编号")
        yield "message", message


async def _reserved_stream(role_or_model, messages, tools=None):
    """Task-only reservations leave existing chat behavior and unknown usage intact."""
    from contextlib import aclosing
    from app.extension_jobs import reserve, finish_reservation
    from app import reminders
    proactive_reservation = reminders.reserve(messages, tools)
    reservation = reserve(messages, tools)
    token = None
    if reservation:
        old = output_limit.get()
        token = output_limit.set(min(old, reservation['output']) if old else reservation['output'])
    if proactive_reservation:
        old=output_limit.get()
        if token is not None: output_limit.reset(token)
        token=output_limit.set(min(old,1024) if old else 1024)
    actual = None
    import time
    started=time.monotonic();state='incomplete';reason=None;first_answer=None
    try:
        async with aclosing(_chat_stream(role_or_model, messages, tools)) as stream:
            async for kind, value in stream:
                if kind == 'usage':
                    actual = value
                if kind == 'answer' and value and first_answer is None:first_answer=time.monotonic()
                if kind == 'message': state='complete'
                yield kind, value
    except BaseException as error:
        state='cancelled' if isinstance(error,BaseException) and not isinstance(error,Exception) else 'failed'
        from app.provider_failover import retry_reason
        reason=retry_reason(error) or type(error).__name__
        raise
    finally:
        try:
            finish_reservation(reservation, actual)
            reminders.finish(proactive_reservation, actual)
            from app.evolution_api import record_model
            selected=attempt_route.get()
            label=(selected[0]['id']+'/'+selected[1]) if selected else role_or_model
            used=last_generation_route.get()
            if used and used[0]==role_or_model: label=used[1]
            try: record_model(role_or_model,label,state,started,actual,reason, None if first_answer is None else (first_answer-started)*1000, None if first_answer is None else (time.monotonic()-first_answer)*1000)
            except Exception as audit_error:
                import sqlite3
                if not isinstance(audit_error,sqlite3.OperationalError) or 'no such table: model_requests' not in str(audit_error):
                    raise
        finally:
            if token is not None: output_limit.reset(token)


async def chat_stream(role_or_model, messages, tools=None):
    """Retry generation only, before any content/tool fragments have escaped.

Each attempt owns its budget reservation. Rejected approvals and tool execution
are outside this function; it cannot replay external operations.
"""
    from contextlib import aclosing
    from app import provider_failover, usage
    last_generation_route.set(None)
    routes = provider_failover.candidates(role_or_model, messages) or [None]
    for index, route in enumerate(routes):
        token = attempt_route.set(route)
        emitted, actual = False, None
        try:
            async with aclosing(_reserved_stream(role_or_model, messages, tools)) as stream:
                async for kind, value in stream:
                    emitted = True
                    if kind == 'usage':
                        actual = value
                    yield kind, value
            return
        except Exception as error:
            reason = provider_failover.retry_reason(error)
            if emitted or not reason or index + 1 >= len(routes):
                raise
            current, following = routes[index], routes[index + 1]
            source = f"{current[0]['id']}/{current[1]}"
            target = f"{following[0]['id']}/{following[1]}"
            # Failed attempts consume unknown usage, never a fabricated zero.
            usage.record(role_or_model, source, actual)
            event = provider_failover.record_switch(role_or_model, source, target, reason)
            yield 'provider_switch', event
        finally:
            attempt_route.reset(token)
