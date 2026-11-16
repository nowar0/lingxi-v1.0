"""Trusted bounded MCP worker and hash-gated Python server launcher.

The parent PluginRuntime assigns this process to a kill-on-close Job/process group
before sending input. stdio servers inherit that group/Job. Arbitrary server Python
still has the user's OS privileges; no sandbox claim is made.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

MAX_INPUT = 1_048_576
MAX_OUTPUT = 245760


class LimitedOutput:
    """Bound normal Python MCP server writes before SDK buffering (not malicious OS writes)."""
    def __init__(self, target, budget):
        self.target, self.budget = target, budget
        self.encoding, self.errors = 'utf-8', 'strict'
        self.buffer = self
    def write(self, value):
        raw = value if isinstance(value, bytes) else str(value).encode('utf-8')
        self.budget[0] += len(raw)
        if self.budget[0] > MAX_OUTPUT:
            raise ValueError('MCP server output limit')
        self.target.buffer.write(raw)
        self.target.buffer.flush()
        return len(value)
    def flush(self): self.target.flush()
    def isatty(self): return False


def _env():
    env = {'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    if os.environ.get('SystemRoot'):
        env['SystemRoot'] = os.environ['SystemRoot']
    return env


def _entry(root, declaration, target):
    path = declaration.get('entrypoint')
    if not isinstance(path, str) or '\\' in path or any(part in ('', '.', '..') for part in path.split('/')):
        raise ValueError('entry')
    file = root.joinpath(*path.split('/'))
    if (file.suffix.lower() != '.py' or file.is_symlink()
            or any(parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400)
                   for parent in (file, *file.parents) if parent.is_relative_to(root))
            or not file.resolve(strict=True).is_relative_to(root)):
        raise ValueError('entry')
    if str(file.resolve()) != target['entrypoint'] or hashlib.sha256(file.read_bytes()).hexdigest() != target['sha256']:
        raise ValueError('hash')
    return file.resolve()


def server_main():
    """Second trusted child launched only for the approved package .py entry."""
    raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT or not raw.endswith(b'\n'):
        raise ValueError('gate')
    gate = json.loads(raw)
    root = Path(gate['package_root']).resolve(strict=True)
    entry = _entry(root, gate['declaration'], gate['target'])
    os.chdir(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    sys.argv = [str(entry)]
    budget = [0]
    sys.stdout, sys.stderr = LimitedOutput(sys.stdout, budget), LimitedOutput(sys.stderr, budget)
    runpy.run_path(str(entry), run_name='__main__')


@contextlib.asynccontextmanager
async def _connected(request):
    import httpx2
    from urllib.parse import urlsplit
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    from mcp.client.streamable_http import streamable_http_client
    declaration, target = request['declaration'], request['target']
    root = Path(request['package_root']).resolve(strict=True)
    if declaration['transport'] == 'http':
        if target.get('url') != declaration['url']:
            raise ValueError('target')
        approved = urlsplit(declaration['url'])
        async def exact_target(http_request):
            destination = urlsplit(str(http_request.url))
            if (destination.scheme, destination.hostname, destination.port or (443 if destination.scheme == 'https' else 80), destination.path) != (
                    approved.scheme, approved.hostname, approved.port or (443 if approved.scheme == 'https' else 80), approved.path):
                raise ValueError('MCP session endpoint differs from approved target')
        received = [0]
        class BoundedStream(httpx2.AsyncByteStream):
            def __init__(self, stream): self.stream = stream
            async def __aiter__(self):
                async for chunk in self.stream:
                    received[0] += len(chunk)
                    if received[0] > MAX_OUTPUT:
                        raise ValueError('MCP response body limit')
                    yield chunk
            async def aclose(self): await self.stream.aclose()
        async def bounded_response(response):
            length = response.headers.get('content-length')
            if length and int(length) > MAX_OUTPUT:
                await response.aclose()
                raise ValueError('MCP response body limit')
            response.stream = BoundedStream(response.stream)
        async with httpx2.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                     event_hooks={'request':[exact_target], 'response':[bounded_response]}) as http:
            async with Client(streamable_http_client(declaration['url'], http_client=http), read_timeout_seconds=20) as client:
                yield client
    else:
        _entry(root, declaration, target)
        # This package entry gets a direct interpreter command, but the worker's
        # pre-input Job gate has already authorized and bounded the entire tree.
        # Frozen host cannot execute arbitrary -c/-I; use a fixed trusted dispatch.
        if getattr(sys, 'frozen', False):
            command, arguments = sys.executable, ['--package-mcp-server', str(root), declaration['entrypoint'], target['sha256']]
        else:
            from app.plugin_runtime import python_worker_command
            worker = python_worker_command('package_mcp_worker')
            command, arguments = worker[0], worker[1:] + ['--server', str(root), declaration['entrypoint'], target['sha256']]
        async with Client(StdioServerParameters(command=command, args=arguments, env=_env(), cwd=str(root)), read_timeout_seconds=20) as client:
            yield client


async def perform(request):
    import jsonschema
    async with asyncio.timeout(40):
        async with _connected(request) as client:
            found, cursor = [], None
            for _ in range(20):
                page = await client.list_tools(cursor=cursor)
                found.extend(tool.model_dump(by_alias=True, mode='json') for tool in page.tools)
                if len(found) > 100 or len(json.dumps(found).encode()) > MAX_OUTPUT:
                    raise ValueError('catalog')
                cursor = page.next_cursor
                if not cursor:
                    break
            else:
                raise ValueError('pagination')
            if request['operation'] == 'discover':
                return {'tools': found}
            definition = next((item for item in found if item['name'] == request['tool']), None)
            if definition is None or definition.get('inputSchema') != request['tool_schema']:
                raise ValueError('changed schema')
            jsonschema.validate(request['args'], request['tool_schema'])
            result = await client.call_tool(request['tool'], request['args'])
            return {'result': result.model_dump(by_alias=True, mode='json')}


def dispatch_server(argv):
    if len(argv) != 3:
        raise ValueError('server gate arguments')
    root = Path(argv[0]).resolve(strict=True)
    declaration = {'entrypoint': argv[1]}
    target = {'entrypoint': str(root.joinpath(*argv[1].split('/')).resolve()), 'sha256': argv[2]}
    entry = _entry(root, declaration, target)
    os.chdir(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    sys.argv = [str(entry)]
    budget = [0]
    sys.stdout, sys.stderr = LimitedOutput(sys.stdout, budget), LimitedOutput(sys.stderr, budget)
    runpy.run_path(str(entry), run_name='__main__')


def main():
    if '--server' in sys.argv:
        return dispatch_server(sys.argv[sys.argv.index('--server') + 1:])
    request_id = ''
    sys.stdout.reconfigure(encoding='utf-8', errors='strict')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    try:
        raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT or not raw.endswith(b'\n'):
            raise ValueError('input')
        request = json.loads(raw)
        request_id = request.get('request_id', '')
        if request.get('protocol') != 1 or request.get('operation') not in ('discover', 'execute'):
            raise ValueError('protocol')
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(perform(request))
        response = {'protocol': 1, 'request_id': request_id, 'ok': True, 'result': result}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_OUTPUT:
            raise ValueError('output')
    except BaseException:
        encoded = json.dumps({'protocol': 1, 'request_id': request_id, 'ok': False,
                              'error': {'code': 'mcp_failed', 'message': 'MCP worker 操作失败或结果未知'}})
    sys.stdout.write(encoded + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
