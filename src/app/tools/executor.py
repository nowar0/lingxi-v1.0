"""工具在独立进程中执行，超时/断开时终止进程，不留下继续写入的线程。"""

import asyncio
import json
import os
import subprocess
import sys

from app.config import BASE_DIR


async def execute(name, args, ctx, timeout):
    from app.tools import BY_NAME
    descriptor=BY_NAME.get(name)
    if descriptor is None: raise ValueError('未登记的能力')
    if ctx.get('read_only') and descriptor['write']: raise ValueError('计划或只读模式禁止执行写入工具')
    from app.execution_permissions import check
    check(name,descriptor,args,ctx)
    role=ctx.get('agent_role','chat')
    if role not in descriptor['roles']: raise ValueError('此角色不可调用该能力')
    if name=='request_user_input':raise ValueError('提问卡片需要实时对话界面')
    group=descriptor['group']
    if group in ('task','ui'):
        from app.domain_tools import execute as run_domain
        return json.dumps(await run_domain(name,args,ctx),ensure_ascii=False)
    if name=='suggest_work_task':
        from app.handoff import suggest
        return json.dumps(suggest(ctx,**args),ensure_ascii=False)
    if name=='describe_image':
        from app import media
        row,_=media.read(args['attachment_id'],ctx['session_id'])
        if not row['mime'].startswith('image/'): raise ValueError('此会话附件不是图片')
        value=await media.invoke('vision',media.Request(session_id=ctx['session_id'],attachment_id=args['attachment_id'],text=args.get('prompt','请描述图片')))
        return json.dumps(value,ensure_ascii=False)
    if name=='search_project_docs':
        from app.project_docs import search
        if ctx.get('mode')!='work': raise ValueError('资料库仅用于当前工作会话')
        return json.dumps(await asyncio.to_thread(search,ctx['session_id'],args['query'],int(args.get('top_k','5'))),ensure_ascii=False)
    if name=='load_context_source':
        from app.work_quality import load_source
        return load_source(ctx,**args)
    if name == 'dedicated_browser':
        from app import browser
        return json.dumps(await browser.execute(**args), ensure_ascii=False)
    if name in ('work_browser', 'office_artifact'):
        if ctx.get('mode') != 'work' or not ctx.get('session_id'):
            raise ValueError('工作台工具只能用于当前工作会话')
        if name == 'work_browser':
            from app.browser_enhanced import execute_tool
        else:
            from app.office_tools import execute_tool
        return json.dumps(await execute_tool(ctx['session_id'], args), ensure_ascii=False)
    # Windows 下 uvicorn --reload 使用 SelectorEventLoop，不支持 asyncio 子进程。
    # 用 Popen + 等待线程兼容它；超时真正 kill 进程，不让线程继续执行工具。
    command = ([sys.executable, '--tool-worker'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-B', '-m', 'app.tools.worker'])
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(BASE_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    payload = json.dumps({"name": name, "args": args, "ctx": ctx}).encode()
    task = asyncio.create_task(asyncio.to_thread(proc.communicate, payload))
    try:
        out, _ = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        if proc.returncode:
            raise RuntimeError("工具进程异常退出")
        data = json.loads(out)
        if not data["ok"]:
            raise RuntimeError(data["result"])
        return data["result"]
    finally:
        if proc.poll() is None:
            if os.name == 'nt':
                await asyncio.to_thread(subprocess.run, ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                proc.kill()
        await task
