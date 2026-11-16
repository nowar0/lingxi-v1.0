"""Agno model bridge; credentials and model accounting remain owned by Lingxi.

Import only when the pinned optional Agno runtime has been provisioned.
"""
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from agno.models.base import Model
from agno.models.response import ModelResponse


@dataclass
class LingxiTeamModel(Model):
    provider: str = 'lingxi'
    transport: Any = None
    job_id: str | None = None
    member_id: str | None = None
    requests: int = 0
    max_requests: int = 80

    def invoke(self, *args, **kwargs):
        raise RuntimeError('Lingxi teams require asynchronous execution')

    def invoke_stream(self, *args, **kwargs):
        raise RuntimeError('Lingxi teams require asynchronous execution')

    async def ainvoke(self, messages, tools=None, **kwargs):
        from app import usage
        if self.job_id:
            from app import jobs
            sid = jobs.snapshot(self.job_id)['session_id']
            with usage.scope(session_id=sid,job_id=self.job_id,node_id=self.member_id or '',agent_id=self.member_id or '@supervisor',kind='agent'):
                return await self._ainvoke(messages,tools,**kwargs)
        return await self._ainvoke(messages,tools,**kwargs)

    async def _ainvoke(self, messages, tools=None, **kwargs):
        self.requests += 1
        if self.requests > self.max_requests:
            raise RuntimeError("团队模型调用次数达到上限，已保存成果保留，请检查模型的工具调用能力")
        transport = self.transport
        if transport is None:
            from app.llm.router import chat_stream
            transport = chat_stream
        payload = []
        for message in messages:
            fields = ('role', 'content', 'name', 'tool_calls', 'tool_call_id')
            payload.append({key: getattr(message, key) for key in fields
                            if getattr(message, key, None) is not None})
        if self.job_id:
            import asyncio, json
            from app import jobs
            from app.db import connect
            if jobs.snapshot(self.job_id)['status'] != 'running':
                raise asyncio.CancelledError('任务已暂停或取消')
            with connect() as db:
                jobs.event(db, self.job_id, self.member_id, 'coordination_input', json.dumps(payload, ensure_ascii=False))
        reply = None
        consumption = None
        try:
            async with aclosing(transport(self.id, payload, tools)) as stream:
                async for kind, value in stream:
                    if kind == 'message':
                        reply = value
                    elif kind == 'usage':
                        consumption = value
                    elif kind == 'provider_switch' and self.job_id:
                        with connect() as db:
                            jobs.event(db, self.job_id, self.member_id, 'provider_switch', json.dumps(value, ensure_ascii=False))
            if not isinstance(reply, dict):
                raise ValueError('Lingxi model returned no completed message')
            if self.job_id:
                with connect() as db:
                    jobs.event(db, self.job_id, self.member_id, 'coordination_message', json.dumps(
                        {k:v for k,v in reply.items() if k in ('role','content','tool_calls')}, ensure_ascii=False))
            return ModelResponse(role='assistant' , content=reply.get('content'),
                                 tool_calls=reply.get('tool_calls'))
        finally:
            if self.transport is None:
                from app import usage
                from app.llm import router
                usage.record(self.id, router.generation_full_name(self.id), consumption)

    async def ainvoke_stream(self, *args, **kwargs):
        yield await self.ainvoke(*args, **kwargs)

    def _parse_provider_response(self, response, **kwargs):
        return response

    def _parse_provider_response_delta(self, response):
        return response
