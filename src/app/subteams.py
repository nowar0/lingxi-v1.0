"""Managed independent SDK processes; the host owns model credentials and usage."""
import asyncio
import json
import os
from pathlib import Path
import subprocess


def interpreter(engine):
    from app.config import BASE_DIR
    runtime = Path(BASE_DIR).parent / f'{engine}-env' / 'Scripts' / 'python.exe'
    bundled = Path(BASE_DIR) / 'team-runtimes' / engine / 'python.exe'
    selected = bundled if bundled.is_file() else runtime
    if not selected.is_file():
        raise RuntimeError(f'{engine} 独立运行环境未安装')
    return selected


async def run(engine, jid, nid, task):
    from app import jobs,usage
    with usage.scope(session_id=jobs.snapshot(jid)['session_id'],job_id=jid,node_id=nid,agent_id=nid,kind='agent'):
        return await _run(engine,jid,nid,task)


async def _run(engine, jid, nid, task):
    from app import jobs, usage
    from app.llm import router
    from app.db import connect
    definition = next(n['document'] for n in jobs.snapshot(jid)['nodes'] if n['id'] == nid)
    role = definition.get('model') or jobs.select_model()[0]
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE'}}
    env['PYTHONIOENCODING'] = 'utf-8'
    proc = await asyncio.create_subprocess_exec(str(interpreter(engine)), '-B',
        str(Path(__file__).with_name('subteam_worker.py')),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        limit=2**20)
    async def send(value):
        proc.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
        await proc.stdin.drain()
    try:
        await send({'engine': engine, 'node_id': nid, 'task': task})
        for _ in range(100):
            line = await asyncio.wait_for(proc.stdout.readline(), 300)
            if not line:
                raise RuntimeError(f'{engine} 工作进程提前退出')
            message = json.loads(line)
            if jobs.snapshot(jid)['status'] not in ('running', 'pausing'):
                raise asyncio.CancelledError()
            if message['type'] == 'result':
                return str(message['result'])
            if message['type'] == 'error':
                raise RuntimeError(message['error'])
            if message['type'] == 'event':
                with connect() as db:
                    jobs.event(db, jid, nid, 'subteam_event', json.dumps({'engine': engine, **message}))
            elif message['type'] == 'model':
                if jobs.snapshot(jid)['status'] != 'running':
                    raise asyncio.CancelledError()
                with connect() as db:
                    jobs.event(db, jid, nid, 'subteam_message', json.dumps({'speaker': 'input', 'messages': message['messages']}, ensure_ascii=False))
                reply, consumption = None, None
                try:
                    from app.tools import BY_NAME
                    names = definition.get('tools')
                    from app.tools.registry import worker_tool_names
                    host_names = [name for name in worker_tool_names() if names is None or name in names]
                    from app.session_options import get as session_options
                    groups=session_options(jobs.snapshot(jid)['session_id'])['tool_groups']
                    if groups is not None:host_names=[name for name in host_names if BY_NAME[name]['group'] in groups]
                    sdk_specs = message.get('tools') or []
                    specs = sdk_specs + [BY_NAME[name]['spec'] for name in host_names if name not in {s['function']['name'] for s in sdk_specs}]
                    context = message['messages']
                    for _round in range(6):
                        if jobs.snapshot(jid)['status'] != 'running':
                            raise asyncio.CancelledError()
                        reply, consumption = None, None
                        from app.work_quality import compact
                        await compact(role,context,jid,nid)
                        async for kind, value in router.chat_stream(role, context, specs):
                            if kind == 'message': reply = value
                            elif kind == 'usage': consumption = value
                            elif kind == 'provider_switch':
                                with connect() as db:
                                    jobs.event(db, jid, nid, 'provider_switch', json.dumps(value, ensure_ascii=False))
                        usage.record(role, router.generation_full_name(role), consumption)
                        consumption = None
                        if not reply:
                            raise ValueError('模型未返回完整结果')
                        calls = reply.get('tool_calls') or []
                        host_calls = [c for c in calls if c['function']['name'] in host_names]
                        if not host_calls:
                            break
                        if len(host_calls) != len(calls):
                            raise ValueError('请分开调用宿主工具和子团队内部工具')
                        with connect() as db:
                            jobs.event(db, jid, nid, 'member_message', json.dumps({k:v for k,v in reply.items() if k in ('role','content','tool_calls')}, ensure_ascii=False))
                        context = [*context, reply]
                        for call in host_calls:
                            try:
                                output = await jobs.execute_tool(jid, nid, call)
                            except Exception as exc:
                                output = '工具未完成：' + str(exc)
                                with connect() as db:
                                    jobs.event(db, jid, nid, 'tool_rejected', output)
                            context.append({'role':'tool', 'tool_call_id':call['id'], 'content':str(output)[:20000]})
                    else:
                        raise ValueError('子团队宿主工具轮次达到上限')
                    if reply is None:
                        raise ValueError('模型未返回完整结果')
                    with connect() as db:
                        jobs.event(db, jid, nid, 'subteam_message', json.dumps({'speaker': 'assistant', 'message': {k:v for k,v in reply.items() if k in ('role','content','tool_calls')}}, ensure_ascii=False))
                    await send({'message': {k:v for k,v in reply.items() if k in ('role','content','tool_calls')}})
                finally:
                    if consumption is not None:
                        usage.record(role, router.generation_full_name(role), consumption)
        raise RuntimeError('子团队调用轮次已用尽')
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
