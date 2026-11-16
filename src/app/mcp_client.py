"""MCP 2.x client; bounded sessions, no automatic retries of external actions."""
import asyncio
from contextlib import asynccontextmanager

import httpx2
import jsonschema
from fastapi import HTTPException
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from pydantic import BaseModel, ConfigDict, Field

from app import catalog, config, mcp_access


@asynccontextmanager
async def connected(plugin):
    if not plugin['enabled']:
        raise ValueError('插件已关闭')
    approved = plugin.get('_approved_snapshot')
    if approved is not None and mcp_access.snapshot(plugin) != approved:
        raise ValueError('已批准的可执行程序发生变化，请重新预览')
    if plugin['transport'] == 'stdio':
        command = approved['executable'] if approved is not None else plugin['command']
        async with Client(StdioServerParameters(command=command, args=plugin['args'], env=mcp_access.process_environment()), read_timeout_seconds=20) as client:
            yield client
    else:
        headers = {}
        if plugin['credential_ref']:
            key = config.load_key(plugin['credential_ref'])
            if not key:
                raise ValueError('插件凭据尚未配置')
            headers['Authorization'] = 'Bearer ' + key
        async with httpx2.AsyncClient(headers=headers, timeout=20, follow_redirects=False) as http:
            async with Client(streamable_http_client(plugin['url'], http_client=http), read_timeout_seconds=20) as client:
                yield client


async def tools_for(client):
    found, cursor = [], None
    for _ in range(20):
        page = await client.list_tools(cursor=cursor)
        found.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return found
    raise ValueError('工具目录分页超过限制')


async def discover(plugin_id, *, approved_plugin=None):
    plugin = approved_plugin if approved_plugin is not None else catalog.get('plugin', plugin_id)
    async with asyncio.timeout(30):
        async with connected(plugin) as client:
            items = await tools_for(client)
            return {'plugin_id': plugin_id, 'revision': plugin['revision'],
                    'tools': [t.model_dump(by_alias=True, mode='json') for t in items]}


async def call(plugin_id, revision, tool_name, arguments):
    plugin = catalog.get('plugin', plugin_id)
    if plugin['revision'] != revision:
        raise ValueError('插件配置已改变，请重新确认工具调用')
    async with asyncio.timeout(40):
        async with connected(plugin) as client:
            tools = await tools_for(client)
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                raise ValueError('插件没有此工具')
            schema = tool.model_dump(by_alias=True)['inputSchema']
            jsonschema.validate(arguments, schema)
            current = catalog.get('plugin', plugin_id)
            if not current['enabled'] or current['revision'] != revision:
                raise ValueError('插件已关闭或改变')
            result = await client.call_tool(tool_name, arguments)
            return result.model_dump(by_alias=True, mode='json')


class ConnectionConsent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=20, max_length=100)
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    allowed: bool = Field(strict=True)


@catalog.router.post('/plugins/{plugin_id}/discover-preview')
def discover_preview(plugin_id: str):
    return mcp_access.preview(plugin_id)


@catalog.router.post('/plugins/{plugin_id}/discover')
async def discover_endpoint(plugin_id: str, body: ConnectionConsent):
    approved_plugin = mcp_access.consume(plugin_id, body.token, body.fingerprint, body.allowed, frozen=True)
    if not approved_plugin:
        return {'cancelled': True, 'tools': []}
    try:
        result = await discover(plugin_id, approved_plugin=approved_plugin)
        mcp_access.audit_result(plugin_id, body.fingerprint, 'complete')
        return result
    except (TimeoutError, ValueError) as exc:
        mcp_access.audit_result(plugin_id, body.fingerprint, 'failed')
        raise HTTPException(409, '连接超时或配置不可用；未自动重试') from exc
    except HTTPException:
        mcp_access.audit_result(plugin_id, body.fingerprint, 'failed')
        raise
    except Exception as exc:
        # SDK errors can contain request headers/URLs. Do not expose raw errors.
        mcp_access.audit_result(plugin_id, body.fingerprint, 'failed')
        raise HTTPException(502, 'MCP 连接失败，请检查服务、凭据引用及地址') from exc
