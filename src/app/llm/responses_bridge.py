"""Preserve the existing local tool loop for models requiring Responses tools."""
from openai.types import CompletionUsage


def required(model, tools, effort):
    name = model.lower().split('/')[-1]
    if not tools: return False
    if name.startswith(('gpt-6-astra', 'gpt-6.1-sol')): return True
    return name.startswith(('gpt-6-sol', 'gpt-6-luna')) and effort != 'none'


def inputs(messages):
    result = []
    for message in messages:
        role = message['role']
        if role == 'tool':
            result.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': str(message.get('content') or '')})
        elif role == 'assistant' and message.get('_responses_items'):
            result.extend(message['_responses_items'])
        else:
            if message.get('content'):
                result.append({'role': role, 'content': message['content']})
            for call in message.get('tool_calls', []):
                result.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
    return result


async def stream(client, model, messages, tools, effort=None, output_limit=None):
    kwargs = {'model': model, 'input': inputs(messages), 'stream': True, 'store': False,
              'include': ['reasoning.encrypted_content']}
    if effort: kwargs['reasoning'] = {'effort': effort}
    if output_limit: kwargs['max_output_tokens'] = output_limit
    if tools:
        kwargs['tools'] = [{'type': 'function', **tool['function'], 'strict': False} for tool in tools]
        kwargs['parallel_tool_calls'] = False
    events = await client.responses.create(**kwargs)
    final = None
    try:
        async for event in events:
            if event.type == 'response.output_text.delta': yield 'answer', event.delta
            elif event.type == 'response.reasoning_summary_text.delta': yield 'think', event.delta
            elif event.type == 'response.completed': final = event.response
            elif event.type in ('response.failed', 'response.incomplete', 'error'):
                raise RuntimeError('模型响应未完成，请重试；本轮未标为成功')
    finally:
        await events.close()
    if final is None or final.status != 'completed': raise RuntimeError('模型连接中断，本轮未保存为完整回复')
    calls, content = [], []
    output = [item.model_dump(exclude_none=True) for item in final.output]
    for item in output:
        if item['type'] == 'function_call':
            if not item.get('call_id') or not item.get('name') or len(item.get('arguments', '')) > 12000:
                raise RuntimeError('工具调用缺少名称、编号或参数超过限制')
            calls.append({'id': item['call_id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': item.get('arguments', '{}')}})
        elif item['type'] == 'message':
            content.extend(part['text'] for part in item.get('content', []) if part.get('type') == 'output_text')
    if len(calls) > 8: raise RuntimeError('工具数量超过限制')
    if final.usage:
        data = final.usage.model_dump()
        yield 'usage', CompletionUsage(prompt_tokens=data['input_tokens'], completion_tokens=data['output_tokens'],
            total_tokens=data['total_tokens'], prompt_tokens_details=data.get('input_tokens_details'),
            completion_tokens_details=data.get('output_tokens_details'))
    message = {'role': 'assistant', 'content': ''.join(content) or None, '_responses_items': output}
    if calls: message['tool_calls'] = calls
    yield 'message', message
