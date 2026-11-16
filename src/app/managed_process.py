"""Bounded process exchange with owned child cleanup, including cancellation."""
import asyncio
import os
import signal
import subprocess
import time
from uuid import uuid4
from app.terminal_process import Shell


def ps_literal(value):
    return "'"+str(value).replace("'","''")+"'"


async def windows_run(argv, *, cwd, text, env, timeout, limit):
    if text is not None:raise ValueError('此受管Windows命令入口不接收标准输入')
    results=[];shell=None
    starting=asyncio.create_task(asyncio.to_thread(Shell,str(cwd),0,lambda *r:results.append(r)))
    try:
        # This controller receives no command until its Windows Job is attached;
        # every Git/hook child therefore inherits ownership from its creation.
        shell=await asyncio.shield(starting)
        prefix=''
        if env is not None:
            for key,value in env.items():
                if os.environ.get(key)!=value:prefix+='[Environment]::SetEnvironmentVariable('+ps_literal(key)+','+ps_literal(value)+",'Process'); "
        shell.run(uuid4().hex,prefix+'& '+' '.join(ps_literal(arg) for arg in argv))
        deadline=time.monotonic()+timeout
        while not shell.finished.is_set():
            state=shell.snapshot()
            if state['limited'] or len(state['output'].encode('utf-8'))>limit:raise RuntimeError('命令输出超过限制，已停止其进程')
            if time.monotonic()>deadline:raise TimeoutError('命令超过运行期限，已停止其进程')
            await asyncio.sleep(.05)
        if not results or results[-1][1]!='complete':raise RuntimeError('命令终态未确认')
        result=results[-1][2]
        if result['limited'] or len(result['output'].encode('utf-8'))>limit:raise RuntimeError('命令输出超过限制')
        return {'code':result['exit_code'],'output':result['output']}
    finally:
        if shell is None:
            try:shell=await starting
            except BaseException:pass
        if shell:await asyncio.to_thread(shell.close)


async def run(argv, *, cwd, text=None, env=None, timeout=45, limit=1_000_000):
    if os.name=='nt':return await windows_run(argv,cwd=cwd,text=text,env=env,timeout=timeout,limit=limit)
    proc=subprocess.Popen(argv,cwd=str(cwd),env=env,stdin=subprocess.PIPE if text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
        start_new_session=os.name!='nt')
    def exchange():
        if text is not None:
            try:proc.stdin.write(text.encode('utf-8'));proc.stdin.close()
            except BrokenPipeError:pass
        output=bytearray()
        while chunk:=os.read(proc.stdout.fileno(),8192):
            output.extend(chunk)
            if len(output)>limit:raise RuntimeError('命令输出超过限制，已停止其进程')
        proc.wait()
        return {'code':proc.returncode,'output':output.decode('utf-8',errors='replace')}
    task=asyncio.create_task(asyncio.to_thread(exchange))
    try:
        return await asyncio.wait_for(asyncio.shield(task),timeout)
    finally:
        if os.name!='nt':
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if proc.poll() is None:proc.kill()
        await asyncio.gather(task,return_exceptions=True)
        proc.wait()
        for pipe in (proc.stdin,proc.stdout):
            if pipe:
                try:pipe.close()
                except OSError:pass
