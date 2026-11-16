"""Git views and explicit mutations for the ported DSH workbench components."""
import asyncio
import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import config
from app.extension_tools import git_argv, process, project

router = APIRouter(prefix='/v8/workbench/git')
_locks = {}


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    name: str = Field(default='', max_length=200)
    base: str = Field(default='HEAD', max_length=200)
    path: str = Field(default='', max_length=3000)
    force: bool = False
    deleteBranch: bool = False
    expected_root: str = Field(default='',max_length=3000)
    expected_head: str = Field(default='',max_length=64)


def reject(code, message):
    return {'ok': False, 'error': {'code': code, 'message': message}}


async def git(root, *args, limit=2000000):
    return await process(git_argv(root, *args), timeout=45, limit=limit)


async def root_for(sid):
    folder = project(sid)
    result = await git(folder, 'rev-parse', '--show-toplevel')
    if result['code']:
        raise HTTPException(409, '绑定目录不是 Git 工作区')
    return Path(result['output'].strip()).resolve(strict=True)


async def status(root):
    branch = await git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    head = await git(root, 'rev-parse', '--short', 'HEAD')
    raw = await git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal')
    entries = raw['output'].split('\0')
    dirty = untracked = conflicts = 0
    skip = False
    for item in entries:
        if skip:
            skip = False
            continue
        if not item:
            continue
        flag = item[:2]
        if flag == '??':
            untracked += 1
        else:
            dirty += 1
        conflicts += flag in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU')
        skip = 'R' in flag or 'C' in flag
    gd = await git(root, 'rev-parse', '--absolute-git-dir')
    gitdir = Path(gd['output'].strip())
    operation = any((gitdir / p).exists() for p in
                    ('MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG'))
    return {'root': str(root), 'branch': branch['output'].strip() if not branch['code'] else '',
            'head': head['output'].strip() if not head['code'] else '', 'dirtyFiles': dirty,
            'untrackedFiles': untracked, 'conflicts': conflicts, 'operationInProgress': operation}


async def worktrees(root):
    result = await git(root, 'worktree', 'list', '--porcelain', '-z')
    if result['code']:
        raise HTTPException(409, result['output'][:2000])
    rows, current = [], None
    for line in result['output'].split('\0'):
        if line.startswith('worktree '):
            if current:
                rows.append(current)
            current = {'path': line[9:], 'head': '', 'branch': '', 'main': not rows}
        elif current is not None and line.startswith('HEAD '):
            current['head'] = line[5:]
        elif current is not None and line.startswith('branch refs/heads/'):
            current['branch'] = line[18:]
        elif current is not None and line.startswith('locked'):
            current['locked'] = True
    if current:
        rows.append(current)
    home = (config.DATA_DIR / 'worktrees').resolve()
    for row in rows:
        p = Path(row['path']).resolve()
        row['managed'] = not row['main'] and p.is_relative_to(home) and p != home
    return rows


@router.get('/status')
async def read_status(session_id: int):
    return await status(await root_for(session_id))


@router.get('/branches')
async def branches(session_id: int):
    root = await root_for(session_id)
    snapshot = await status(root)
    result = await git(root, 'for-each-ref', 'refs/heads', '--format=%(refname:short)%00%(HEAD)')
    from app.git_transition import switch_boundary
    reason=switch_boundary(root,session_id)
    if not reason and (snapshot['dirtyFiles'] or snapshot['untrackedFiles']):reason='存在未提交或未跟踪的文件，请先提交或处理改动'
    if not reason and (snapshot['conflicts'] or snapshot['operationInProgress']):reason='请先处理冲突或未结束的Git操作'
    snapshot.update(canSwitch=not reason,switchReason=reason)
    snapshot['branches'] = [{'name': line.split('\0')[0], 'current': line.endswith('\0*')}
                            for line in result['output'].splitlines() if '\0' in line]
    return snapshot


@router.get('/diff')
async def diff(session_id: int):
    root = await root_for(session_id)
    working = await git(root, 'diff', '--no-ext-diff', '--no-textconv', '--no-color', '--', limit=1000000)
    staged = await git(root, 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--no-color', '--', limit=1000000)
    changes = await git(root, 'status', '--short', '--untracked-files=normal', limit=100000)
    if working['code'] or staged['code'] or changes['code']:
        raise HTTPException(409, '无法读取当前项目差异')
    return {'status': changes['output'], 'working': working['output'], 'staged': staged['output']}


@router.get('/graph')
async def graph(session_id: int, limit: int = Query(200, ge=1, le=2000)):
    root = await root_for(session_id)
    snapshot = await status(root)
    result = await git(root, 'log', '--all', '--topo-order', '-z', '-n', str(limit + 1),
                       '--format=%H%x1f%P%x1f%s%x1f%an%x1f%at%x1f%D')
    if result['code'] and snapshot['head']:
        raise HTTPException(409, result['output'][:2000])
    commits = []
    for row in result['output'].split('\0'):
        fields = row.strip('\r\n').split('\x1f')
        if len(fields) != 6:
            continue
        oid, parents, subject, author, timestamp, refs = fields
        commits.append({'oid': oid, 'parents': parents.split(), 'subject': subject, 'author': author,
                        'authorTime': int(timestamp), 'refs': [r.strip() for r in refs.split(',') if r.strip()]})
    return {'root': str(root), 'branch': snapshot['branch'], 'commits': commits[:limit], 'hasMore': len(commits) > limit}


@router.get('/commit')
async def commit(session_id: int, oid: str):
    if not re.fullmatch(r'[a-fA-F0-9]{7,64}', oid):
        raise HTTPException(422, '提交编号无效')
    root = await root_for(session_id)
    result = await git(root, '-c', 'diff.external=', 'show', '--no-ext-diff', '--no-textconv',
                       '--format=fuller', '--stat', '--patch', oid, '--', limit=1000000)
    if result['code']:
        raise HTTPException(409, result['output'][:1000])
    return {'oid': oid, 'diff': result['output']}


@router.get('/worktrees')
async def list_worktrees(session_id: int):
    root = await root_for(session_id)
    return {'root': str(root), 'worktrees': await worktrees(root)}


def lock(root):
    return _locks.setdefault(str(root), asyncio.Lock())


async def valid_branch(root, name):
    return bool(name and not name.startswith('-') and
                not (await git(root, 'check-ref-format', '--branch', name))['code'])


@router.post('/switch')
async def switch(body: Request):
    root = await root_for(body.session_id)
    async with lock(root):
        from app.git_transition import reserve
        with reserve(root,body.session_id):
            if not await valid_branch(root, body.name):
                return reject('invalid-branch-name', '分支名不合法')
            state = await status(root)
            if body.expected_root and str(root)!=str(Path(body.expected_root).resolve()):return reject('project-changed','项目目录已改变，请刷新后重新确认')
            if body.expected_head and state['head']!=body.expected_head:return reject('head-changed','当前提交已改变，请刷新后重新确认')
            if state['dirtyFiles'] or state['untrackedFiles']:return reject('uncommitted-changes','存在未提交或未跟踪的文件，请先提交或处理改动')
            if state['conflicts'] or state['operationInProgress']:
                return reject('operation-in-progress', '请先处理未结束的 Git 操作')
            result = await git(root, 'switch', '--no-guess', body.name)
            if result['code']:
                return reject('tracked-changes-would-be-overwritten', result['output'][:2000])
            return {'ok': True, 'branch': body.name}


@router.post('/branch')
async def create_branch(body: Request):
    root = await root_for(body.session_id)
    async with lock(root):
        from app.git_transition import reserve
        with reserve(root,body.session_id):
            if not await valid_branch(root, body.name):
                return reject('invalid-branch-name', '分支名不合法')
            state = await status(root)
            if body.expected_root and str(root)!=str(Path(body.expected_root).resolve()):return reject('project-changed','项目目录已改变，请刷新后重新确认')
            if body.expected_head and state['head']!=body.expected_head:return reject('head-changed','当前提交已改变，请刷新后重新确认')
            if state['dirtyFiles'] or state['untrackedFiles']:return reject('uncommitted-changes','存在未提交或未跟踪的文件，请先提交或处理改动')
            if state['conflicts'] or state['operationInProgress']:
                return reject('operation-in-progress', '请先处理未结束的 Git 操作')
            result = await git(root, 'switch', '-c', body.name)
            if result['code']:
                return reject('branch-already-exists', result['output'][:2000])
            return {'ok': True, 'branch': body.name}


@router.post('/branch-delete')
async def delete_branch(body: Request):
    root = await root_for(body.session_id)
    async with lock(root):
        if not await valid_branch(root, body.name):
            raise HTTPException(422, '分支名不合法')
        # No force-delete: unmerged branches remain recoverable.
        result = await git(root, 'branch', '-d', '--', body.name)
        if result['code']:
            raise HTTPException(409, result['output'][:2000])
        return {'ok': True}


@router.post('/worktree-add')
async def add_worktree(body: Request):
    root = await root_for(body.session_id)
    async with lock(root):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', body.name):
            return reject('invalid-worktree-name', '名称只用英文、数字、下划线和短横线')
        from app.release import safe_name
        try:
            safe_name(body.name)
        except ValueError:
            return reject('invalid-worktree-name', '不能使用 Windows 保留名称')
        if body.base.startswith('-') or (await git(root, 'rev-parse', '--verify', body.base + '^{commit}'))['code']:
            return reject('base-ref-not-found', '基线提交不存在')
        parent = config.DATA_DIR / 'worktrees'
        if parent.exists() and (parent.is_symlink() or parent.is_junction()):
            raise HTTPException(409, 'worktree 根目录不能是链接')
        repo = hashlib.sha256(str(root).casefold().encode()).hexdigest()[:12]
        home = parent / repo
        home.mkdir(parents=True, exist_ok=True)
        if home.is_symlink() or home.is_junction():
            raise HTTPException(409, 'worktree 项目目录不能是链接')
        target = home / body.name
        if target.exists():
            return reject('worktree-already-exists', '目标目录已存在')
        branch = 'lingxi/' + body.name
        result = await git(root, 'worktree', 'add', '-b', branch, str(target), body.base)
        if result['code']:
            return reject('internal', result['output'][:2000])
        return {'ok': True, 'path': str(target), 'branch': branch, 'name': body.name}


@router.post('/worktree-remove')
async def remove_worktree(body: Request):
    root = await root_for(body.session_id)
    async with lock(root):
        rows = await worktrees(root)
        target = Path(body.path).resolve()
        item = next((r for r in rows if Path(r['path']).resolve() == target), None)
        if item is None:
            return reject('worktree-not-found', '不是此仓库的 worktree')
        if item['main'] or not item['managed'] or target == root:
            return reject('worktree-is-main', '只允许删除零汐托管且未绑定当前会话的独立工作区')
        if item.get('locked') or target.is_symlink() or target.is_junction():
            return reject('internal', '工作区已锁定或目录是链接')
        # A different session may actively own this directory.
        from app import catalog
        for saved in catalog.listing('work-settings'):
            if saved.get('path') and Path(saved['path']).resolve() == target:
                return reject('internal', '此 worktree 仍绑定工作会话，请先解除绑定')
        dirty = await git(target, 'status', '--porcelain=v1', '--untracked-files=all')
        if dirty['output'].strip() and not body.force:
            return reject('worktree-dirty', '目录包含未提交或未跟踪文件')
        result = await git(root, 'worktree', 'remove', *(('--force',) if body.force else ()), str(target))
        if result['code']:
            return reject('internal', result['output'][:2000])
        warning = None
        if body.deleteBranch and item['branch'].startswith(('lingxi/', 'wt/')):
            deleted = await git(root, 'branch', '-d', '--', item['branch'])
            if deleted['code']:
                warning = '目录已移除，未合并的分支予以保留：' + item['branch']
        return {'ok': True, 'warning': warning}
