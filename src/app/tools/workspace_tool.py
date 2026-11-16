import json
import os
import subprocess
import tempfile
import time

from app import workspace, catalog
from app.tools.capabilities import spec


def scope(ctx, workspace_id):
    if ctx.get('mode') != 'work' or not ctx.get('session_id'):
        raise ValueError('文件工具只供有工作会话的任务使用')
    if ctx.get('workspace_revision') != catalog.get('workspace', workspace_id)['revision']:
        raise ValueError('工作目录绑定已变化，请重新确认')
    return ctx['session_id']


def read(ctx, workspace_id, path):
    with catalog.transaction():
        return json.dumps(workspace.read_file(workspace_id, scope(ctx, workspace_id), path), ensure_ascii=False)


def listing(ctx, workspace_id, path):
    with catalog.transaction():
        return json.dumps(workspace.list_files(workspace_id, scope(ctx, workspace_id), path), ensure_ascii=False)


def write(ctx, workspace_id, path, content, expected_hash):
    with catalog.transaction():
        from app.git_transition import assert_unclaimed
        with catalog.read_connection() as db:assert_unclaimed(db,ctx['session_id'])
        return json.dumps(workspace.write_file(workspace_id, scope(ctx, workspace_id), path, content, expected_hash), ensure_ascii=False)


def edit(ctx,workspace_id,path,old_text,new_text,expected_hash):
    with catalog.transaction():
        from app.git_transition import assert_unclaimed
        with catalog.read_connection() as db:assert_unclaimed(db,ctx['session_id'])
        return json.dumps(workspace.replace_file(workspace_id,scope(ctx,workspace_id),path,old_text,new_text,expected_hash),ensure_ascii=False)


def command(ctx, workspace_id, argv_json):
    root, _ = workspace.resolve(workspace_id, scope(ctx, workspace_id), '.')
    argv = json.loads(argv_json)
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 or any(not isinstance(x, str) or len(x) > 16000 for x in argv):
        raise ValueError('命令必须为非空字符串数组，禁止通过 shell 拼接')
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        start = time.monotonic()
        limited = False
        try:
            while process.poll() is None:
                if time.monotonic() - start > 25 or output.tell() > 2 * 1024 * 1024:
                    limited = True
                    break
                time.sleep(.05)
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
                else:
                    process.kill()
                process.wait(timeout=5)
        output.seek(0)
        text = output.read(100000).decode('utf-8', errors='replace')
    return json.dumps({'argv': argv, 'cwd': str(root), 'exit_code': process.returncode,
                       'output': text, 'limited': limited,
                       'note': '命令在指定目录运行，但不是操作系统沙箱'}, ensure_ascii=False)


TOOLS = []
for name, description, args, run, writing in (
    ('workspace_list', '列出用户绑定到当前工作会话的目录；每次最多500项', ['workspace_id', 'path'], listing, False),
    ('workspace_read', '读取所选项目内 UTF-8 文本及内容指纹', ['workspace_id', 'path'], read, False),
    ('workspace_write', '展示路径与完整内容，经确认后写入；expected_hash 为读取所得指纹，新文件用 new', ['workspace_id', 'path', 'content', 'expected_hash'], write, True),
    ('workspace_edit', '先读取文件取得哈希，提供仅匹配一次的完整旧片段和新片段；审批后原子替换，重复匹配或文件变化拒绝', ['workspace_id','path','old_text','new_text','expected_hash'], edit, True),
    ('workspace_command', '需要用户确认具体命令与工作目录。argv_json为可执行程序及参数数组。最多25秒；工作目录不是沙箱，命令可影响目录外文件。', ['workspace_id', 'argv_json'], command, True),
):
    TOOLS.append({'spec': spec(name, description, {k: {'type': 'string'} for k in args}, args),
                  'write': writing, 'run': run, 'timeout': 35 if run is command else 20})
