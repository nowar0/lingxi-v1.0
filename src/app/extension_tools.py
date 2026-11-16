"""Opt-in local Git views and OpenSSH sessions. No background connection or git writes."""

import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field

from app import catalog, config, work_settings, workspace
from app.extensions import Strict, optional, router

_scans = {}
_terminals = {}


async def process(argv, text=None, timeout=25, limit=200000, env=None):
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.PIPE if text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = bytearray()

        async def exchange():
            if text is not None:
                proc.stdin.write(text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError("工具输出超过限制，已停止本地进程；请核对远端状态")
            await proc.wait()

        try:
            await asyncio.wait_for(exchange(), timeout)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return {"code": proc.returncode, "output": output.decode("utf-8", errors="replace")}
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise HTTPException(502, "工具未完成：" + str(exc)) from None


def executable(name):
    path = shutil.which(name)
    if not path:
        raise HTTPException(409, "未安装 " + name + "，请先安装对应系统工具")
    return path


class GitRequest(Strict):
    session_id: int = Field(gt=0)


def project(sid):
    saved = work_settings.get(sid)
    if not saved["path"]:
        raise HTTPException(409, "请先在工作记录中绑定项目目录")
    root = Path(saved["path"]).resolve(strict=True)
    return root


GIT_HOOK_EVENTS = ('applypatch-msg','pre-applypatch','post-applypatch','pre-commit','pre-merge-commit',
    'prepare-commit-msg','commit-msg','post-commit','pre-rebase','post-checkout','post-merge',
    'pre-push','pre-receive','update','proc-receive','post-receive','post-update',
    'reference-transaction','push-to-checkout','pre-auto-gc','post-rewrite','sendemail-validate',
    'fsmonitor-watchman','p4-changelist','p4-prepare-changelist','p4-post-changelist','p4-pre-submit','post-index-change')


def git_argv(root, *args):
    # Directory-only suppression does not cover config-defined hooks (Git 2.54+).
    # Explicit opt-in commit hooks are enabled only after the confirmed inventory.
    disabled = [part for event in GIT_HOOK_EVENTS for part in ('-c', 'hook.' + event + '.enabled=false')]
    return [
        executable("git"),
        "--no-pager",
        *disabled,
        '-c', 'core.fsmonitor=false',
        "-c",
        "core.hooksPath=" + str(config.DATA_DIR / "disabled-hooks"),
        "-C",
        str(root),
        *args,
    ]


@router.post("/git/overview")
async def git_overview(body: GitRequest):
    root = project(body.session_id)
    data = {}
    for key, args in [
        ("history", ("log", "--graph", "--all", "--oneline", "--decorate", "-60")),
        ("branches", ("branch", "--all", "--no-color")),
        ("worktrees", ("worktree", "list", "--porcelain")),
        ("status", ("status", "--short", "--untracked-files=no")),
    ]:
        result = await process(git_argv(root, *args))
        if result["code"]:
            raise HTTPException(409, "无法读取 Git 仓库：" + result["output"][:2000])
        data[key] = result["output"]
    return {"path": str(root), "worktree_home": str(config.DATA_DIR / "worktrees"), **data}


class Worktree(GitRequest):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    confirm: str


@router.post("/git/worktree")
async def create_worktree(body: Worktree):
    if body.confirm != "创建独立工作区":
        raise HTTPException(422, "请确认创建独立工作区和新分支")
    from app.release import safe_name

    try:
        safe_name(body.name)
    except ValueError:
        raise HTTPException(422, "工作区名称不能使用 Windows 保留名称") from None
    root = project(body.session_id)
    parent = config.DATA_DIR / "worktrees"
    if parent.exists() and (parent.is_symlink() or parent.is_junction()):
        raise HTTPException(409, "工作区存放目录不能是链接")
    parent.mkdir(parents=True, exist_ok=True)
    target = (parent / body.name).resolve()
    if not target.is_relative_to(parent.resolve()) or target.exists():
        raise HTTPException(409, "目标已存在或超出工作区目录")
    result = await process(
        git_argv(root, "worktree", "add", "-b", "lingxi/" + body.name, str(target), "HEAD"),
        timeout=60,
    )
    if result["code"]:
        raise HTTPException(409, "Git 未完成创建：" + result["output"][:3000])
    return {
        "path": str(target),
        "branch": "lingxi/" + body.name,
        "output": result["output"],
        "note": "新目录尚未绑定当前会话，请在工作记录中主动选择；不会提交、合并或推送。",
    }


class Host(Strict):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
    identity_file: str = Field(default="", max_length=2000)
    enabled: bool = False
    expected_revision: int = Field(default=0, ge=0)


@router.get("/ssh/hosts")
def hosts():
    return {
        "items": catalog.listing("ssh-host"),
        "auth": "使用系统 OpenSSH 密钥或 ssh-agent；不保存密码、不读取个人 ssh/config",
    }


@router.put("/ssh/hosts/{key}")
def save_host(key: str, body: Host):
    if body.identity_file:
        path = Path(body.identity_file)
        if not path.is_absolute() or not path.is_file():
            raise HTTPException(422, "密钥文件需要存在的绝对路径；私钥内容不导入零汐")
    return catalog.put(
        "ssh-host", key, body.model_dump(exclude={"expected_revision"}), body.expected_revision
    )


def host_config(key):
    item = catalog.get("ssh-host", key)
    Host(**{k: v for k, v in item.items() if k not in ("id", "revision")})
    if not item["enabled"]:
        raise HTTPException(409, "此主机尚未启用")
    return item


def known_hosts():
    return config.DATA_DIR / "ssh" / "known_hosts"


def ssh_argv(host, name="ssh"):
    path = known_hosts()
    if not path.is_file():
        raise HTTPException(409, "请先扫描并核对主机指纹，再明确保存信任")
    args = [
        executable(name),
        "-F",
        "none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(path),
        "-o",
        "GlobalKnownHostsFile=none",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
    ]
    if host["identity_file"]:
        args += ["-i", host["identity_file"], "-o", "IdentitiesOnly=yes"]
    return args


@router.post("/ssh/hosts/{key}/scan")
async def scan(key: str):
    host = host_config(key)
    result = await process(
        [executable("ssh-keyscan"), "-T", "5", "-p", str(host["port"]), host["host"]],
        timeout=20,
        limit=50000,
    )
    lines = []
    fingerprints = []
    for line in result["output"].splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or not parts[1].startswith(("ssh-", "ecdsa-")):
            continue
        try:
            digest = (
                base64.b64encode(hashlib.sha256(base64.b64decode(parts[2], validate=True)).digest())
                .decode()
                .rstrip("=")
            )
        except ValueError:
            continue
        lines.append(line)
        fingerprints.append(parts[1] + " SHA256:" + digest)
    if not lines:
        raise HTTPException(502, "未取得主机公钥，请检查地址、端口与网络")
    token = uuid4().hex
    _scans[token] = {
        "host_id": key,
        "revision": host["revision"],
        "lines": lines,
        "expires": time.monotonic() + 300,
    }
    for old in list(_scans):
        if _scans[old]["expires"] < time.monotonic():
            _scans.pop(old, None)
    return {
        "token": token,
        "fingerprints": fingerprints,
        "note": "扫描结果本身不证明主机身份，请与服务器管理方提供的指纹核对。",
    }


class Trust(Strict):
    token: str
    confirm: str


@router.post("/ssh/hosts/{key}/trust")
def trust(key: str, body: Trust):
    if body.confirm != "我已核对指纹":
        raise HTTPException(422, "请先核对服务器指纹")
    scanned = _scans.pop(body.token, None)
    host = host_config(key)
    if (
        not scanned
        or scanned["host_id"] != key
        or scanned["revision"] != host["revision"]
        or scanned["expires"] < time.monotonic()
    ):
        raise HTTPException(409, "扫描已过期或主机配置改变，请重新扫描")
    path = known_hosts()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dedicated application file; never edit the user's global OpenSSH trust store.
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write("\n" + "\n".join(scanned["lines"]) + "\n")
    return {"saved": True}


class TerminalStart(Strict):
    confirm: str


async def close_terminal(key):
    terminal = _terminals.pop(key, None)
    if not terminal:
        return
    proc = terminal["process"]
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    terminal["reader"].cancel()
    await asyncio.gather(terminal["reader"], return_exceptions=True)


@router.post("/ssh/hosts/{host_id}/terminal")
async def start_terminal(host_id: str, body: TerminalStart):
    if body.confirm != "连接并允许我输入远程命令":
        raise HTTPException(422, "请明确确认打开远程交互会话")
    if len(_terminals) >= 3:
        raise HTTPException(409, "最多同时打开三个远程终端，请先关闭已有终端")
    host = host_config(host_id)
    args = ssh_argv(host) + ["-tt", "-p", str(host["port"]), host["user"] + "@" + host["host"]]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    key = uuid4().hex
    terminal = {
        "process": proc,
        "buffer": bytearray(),
        "offset": 0,
        "last": time.monotonic(),
        "host_id": host_id,
        "revision": host["revision"],
    }
    _terminals[key] = terminal

    async def read():
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            terminal["buffer"].extend(chunk)
            if len(terminal["buffer"]) > 200000:
                removed = len(terminal["buffer"]) - 200000
                del terminal["buffer"][:removed]
                terminal["offset"] += removed

    terminal["reader"] = asyncio.create_task(read())
    return {
        "id": key,
        "host": host["name"],
        "note": "逐行交互终端；不支持 vim/top 等全屏终端绘制。关闭本地连接不保证撤销已执行的远程操作。",
    }


@router.get("/ssh/terminals/{key}")
async def terminal_read(key: str, offset: int = 0):
    terminal = _terminals.get(key)
    if not terminal:
        raise HTTPException(404, "终端已关闭")
    current = host_config(terminal["host_id"])
    if current["revision"] != terminal["revision"]:
        await close_terminal(key)
        raise HTTPException(409, "主机配置已修改，请重新连接")
    terminal["last"] = time.monotonic()
    start = max(0, offset - terminal["offset"])
    raw = bytes(terminal["buffer"][start:])
    return {
        "output": raw.decode("utf-8", errors="replace"),
        "offset": terminal["offset"] + len(terminal["buffer"]),
        "truncated": offset < terminal["offset"],
        "exit_code": terminal["process"].returncode,
    }


class TerminalInput(Strict):
    text: str = Field(min_length=1, max_length=8000)


@router.post("/ssh/terminals/{key}/input")
async def terminal_input(key: str, body: TerminalInput):
    terminal = _terminals.get(key)
    if not terminal or terminal["process"].returncode is not None:
        raise HTTPException(409, "终端未连接")
    host = host_config(terminal["host_id"])
    if host["revision"] != terminal["revision"]:
        await close_terminal(key)
        raise HTTPException(409, "主机配置已变化")
    terminal["last"] = time.monotonic()
    try:
        terminal["process"].stdin.write(body.text.encode("utf-8"))
        await terminal["process"].stdin.drain()
    except ConnectionError, OSError:
        raise HTTPException(409, "终端连接已断开；未自动重发输入") from None
    return {"sent": True}


@router.delete("/ssh/terminals/{key}")
async def terminal_close(key: str):
    await close_terminal(key)
    return {"closed": True}


class Transfer(Strict):
    session_id: int = Field(gt=0)
    action: str = Field(pattern=r"^(list|get|put)$")
    remote: str = Field(min_length=1, max_length=1000)
    local: str = Field(default="", max_length=1000)
    confirm: bool = False
    operation_id: UUID


def sftp_quote(value):
    if any(c in value for c in "\x00\r\n*?[]{}"):
        raise HTTPException(422, "文件路径不能包含换行、控制字符或通配符")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@router.post("/ssh/hosts/{key}/sftp")
async def transfer(key: str, body: Transfer):
    host = host_config(key)
    if not body.remote.startswith("/"):
        raise HTTPException(422, "远程路径需要从 / 开始的完整路径")
    command = "ls -la " + sftp_quote(body.remote)
    target = None
    if body.action != "list":
        if not body.confirm:
            raise HTTPException(422, "请确认文件传输方向和确切路径")
        root = project(body.session_id)
        try:
            _, target = workspace.resolve(
                "session_" + str(body.session_id), body.session_id, body.local
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        if body.action == "get":
            if target.exists() or not target.parent.is_dir() or not target.is_relative_to(root):
                raise HTTPException(409, "下载目标需为项目中不存在的新文件，且父目录已存在")
            command = "get " + sftp_quote(body.remote) + " " + sftp_quote(target.as_posix())
        else:
            if not target.is_file() or target.stat().st_size > 16 * 1024 * 1024:
                raise HTTPException(422, "上传文件不存在或超过 16 MiB")
            command = "put " + sftp_quote(target.as_posix()) + " " + sftp_quote(body.remote)
    operation = str(body.operation_id)
    fingerprint = hashlib.sha256(
        json.dumps({"host": host, "request": body.model_dump(mode="json")}, sort_keys=True).encode()
    ).hexdigest()
    old = optional("sftp-operation", operation)
    if old["revision"]:
        if old.get("fingerprint") != fingerprint:
            raise HTTPException(409, "操作编号冲突")
        if old.get("status") == "complete":
            return old["result"]
        raise HTTPException(409, "之前的传输结果未确认，不能自动重放；请核对远端和本地文件")
    catalog.put("sftp-operation", operation, {"fingerprint": fingerprint, "status": "running"}, 0)
    address = host["host"]
    if ":" in address:
        address = "[" + address + "]"
    try:
        result = await process(
            ssh_argv(host, "sftp")
            + ["-P", str(host["port"]), "-b", "-", host["user"] + "@" + address],
            text=command + "\nbye\n",
            timeout=120,
        )
        catalog.put(
            "sftp-operation",
            operation,
            {"fingerprint": fingerprint, "status": "complete", "result": result},
            1,
        )
        return result
    except BaseException:
        catalog.put(
            "sftp-operation", operation, {"fingerprint": fingerprint, "status": "unknown"}, 1
        )
        raise


async def reap():
    for key, item in list(_terminals.items()):
        if time.monotonic() - item["last"] > 600:
            await close_terminal(key)


async def shutdown():
    for key in list(_terminals):
        await close_terminal(key)
