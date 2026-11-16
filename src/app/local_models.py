"""Local model services. Only loopback HTTP APIs; no shell or filesystem mutation."""
import asyncio
import json
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit, unquote
from uuid import uuid4

import httpx2
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from typing import Literal
from app import catalog, config, credential_lifecycle, modules, model_profiles, ui_auth

router = APIRouter(prefix='/v11/local-models', tags=['local-models'])
_locks = {}
_tasks = {}
_jobs = {}
_previews = {}
_MODEL = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}$')


def address(value):
    try:
        u = urlsplit(value.strip())
        port = u.port
        path = unquote(u.path).rstrip('/')
        if (u.scheme not in ('http', 'https') or u.hostname not in ('localhost', '127.0.0.1', '::1')
                or u.username or u.password or u.query or u.fragment or '%' in path or '\\' in path
                or any(p in ('.', '..') for p in path.split('/')) or any(ord(c) < 33 for c in path)):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, '请输入本机服务地址，如 http://127.0.0.1:11434') from None
    host = '[::1]' if u.hostname == '::1' else '127.0.0.1'
    if path.endswith('/v1'): path = path[:-3]
    if port == (443 if u.scheme == 'https' else 80): port = None
    return urlunsplit((u.scheme, host + (':' + str(port) if port else ''), path, '', ''))


def model_name(value):
    if not _MODEL.fullmatch(value) or '..' in value or '://' in value:
        raise HTTPException(422, '模型名称格式有误')
    return value


def lock(sid):
    return _locks.setdefault(sid, asyncio.Lock())


def service(sid, revision=None):
    item = catalog.get('local-model-service', sid)
    if item.get('deleted'):
        raise HTTPException(404, '服务已移除')
    if revision is not None and item['revision'] != revision:
        raise HTTPException(409, '服务配置已变化，请刷新')
    return item


def public(item):
    return {k: v for k, v in item.items() if k != 'credential_ref'} | {'has_key': bool(item.get('credential_ref'))}


def headers(item):
    ref = item.get('credential_ref')
    if not ref:
        return {}
    key = config.load_key(ref)
    if not key:
        raise HTTPException(409, '服务凭据不可用，请重新保存')
    return {'Authorization': 'Bearer ' + key}


def client(item, timeout=20):
    return httpx2.AsyncClient(base_url=address(item['base_url']), headers={**headers(item), 'Accept-Encoding': 'identity'},
                              timeout=timeout, follow_redirects=False, trust_env=False)


async def request_json(item, method, path, body=None, timeout=20):
    try:
        async with client(item, timeout) as c:
            async with c.stream(method, address(item['base_url']).rstrip('/') + path, json=body) as r:
                if r.status_code >= 300:
                    raise HTTPException(502, f'本地服务返回 HTTP {r.status_code}，请检查服务与模型')
                data = bytearray()
                async for block in r.aiter_bytes():
                    data.extend(block)
                    if len(data) > 4 * 1024 * 1024:
                        raise HTTPException(502, '服务返回内容过大')
                value = json.loads(data or b'{}')
                if not isinstance(value, dict) or value.get('error'):
                    raise HTTPException(502, '本地服务未能完成操作，请检查服务日志')
                return value
    except (httpx2.HTTPError, ValueError, OSError):
        raise HTTPException(502, '连接失败，请检查本地服务是否启动、端口和凭据是否正确') from None


def active_jobs(sid):
    return [j for j in _jobs.values() if j['service_id'] == sid and j['status'] in ('queued', 'downloading')]


def idle(sid):
    if active_jobs(sid):
        raise HTTPException(409, '请先完成或取消此服务的下载')


class ServiceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['ollama', 'lmstudio', 'openai']
    base_url: str = Field(max_length=500)
    api_key: SecretStr | None = Field(default=None, max_length=10000)
    clear_key: bool = False
    expected_revision: int = Field(default=0, ge=0)


@router.get('/services')
def services():
    return {'items': [public(i) for i in catalog.listing('local-model-service')]}


def drop_unused_key(ref):
    if ref and not any(i.get('credential_ref') == ref for kind in ('local-model-service', 'model-profile', 'module', 'plugin', 'model-key') for i in catalog.listing(kind)):
        credential_lifecycle.delete_key(ref)


@credential_lifecycle.atomic
def save(sid, body):
    old = service(sid, body.expected_revision) if sid else {}
    ref = '' if body.clear_key else old.get('credential_ref', '')
    if body.api_key is not None:
        secret = body.api_key.get_secret_value().strip()
        if not secret or '\n' in secret or '\r' in secret:
            raise HTTPException(422, '凭据不能为空或包含换行')
        ref = 'localmodel_' + uuid4().hex
        try:
            credential_lifecycle.save_key(ref, secret, fresh=True)
        except Exception:
            raise HTTPException(503, '系统凭据保存失败，请重试') from None
    if not sid and body.expected_revision:
        raise HTTPException(422, '新服务版本应为零')
    doc = {'name': body.name.strip(), 'kind': body.kind, 'base_url': address(body.base_url), 'credential_ref': ref}
    if not doc['name']:
        raise HTTPException(422, '请输入服务名称')
    if old and any(old.get(k) != doc[k] for k in ('kind', 'base_url', 'credential_ref')):
        from app import model_references
        model_references.assert_idle()
    saved = catalog.put('local-model-service', sid or uuid4().hex, doc, body.expected_revision)
    for profile in catalog.listing('model-profile') if sid else []:
        if profile.get('local_service_id') != sid: continue
        if profile.get('enabled') and all(old.get(k) == doc[k] for k in ('kind', 'base_url', 'credential_ref')): continue
        fields = {k: v for k, v in profile.items() if k not in ('id', 'revision')}
        fields.update(base_url=doc['base_url'] + '/v1', credential_ref=ref, enabled=True)
        catalog.put('model-profile', profile['id'], fields, profile['revision'])
        for module in catalog.listing('module'):
            if module.get('profile_id') != profile['id']: continue
            value = {k: v for k, v in module.items() if k not in ('id', 'revision')}
            value.update(base_url=fields['base_url'], credential_ref=ref, connection_status='untested')
            catalog.put('module', module['id'], value, module['revision'])
    drop_unused_key(old.get('credential_ref'))
    return public(saved)


def reconcile_profiles():
    """Repair only profiles produced by the old local-model assignment flow."""
    with credential_lifecycle.mutation():
        services={item['id'] for item in catalog.listing('local-model-service')}
        for profile in catalog.listing('model-profile'):
            if profile.get('local_service_id') and profile['local_service_id'] not in services:
                value = {k:v for k,v in profile.items() if k not in ('id','revision','local_service_id')}
                catalog.put('model-profile',profile['id'],value,profile['revision'])
                continue
            if profile.get('local_service_id') and not profile.get('enabled'):
                value = {k: v for k, v in profile.items() if k not in ('id', 'revision')}
                catalog.put('model-profile', profile['id'], {**value, 'enabled': True}, profile['revision'])


@router.post('/services')
def add_service(body: ServiceBody):
    return save(None, body)


@router.put('/services/{sid}')
async def edit_service(sid: str, body: ServiceBody):
    async with lock(sid):
        idle(sid)
        return save(sid, body)


@router.delete('/services/{sid}')
async def remove_service(sid: str, expected_revision: int):
    async with lock(sid):
        idle(sid)
        with credential_lifecycle.mutation():
            old = service(sid, expected_revision)
            from app import model_references
            model_references.assert_idle()
            # Keep existing assignments usable, but detach their removed owner so
            # customers can maintain these profiles from the model library.
            for profile in catalog.listing('model-profile'):
                if profile.get('local_service_id') != sid:
                    continue
                value = {k:v for k,v in profile.items() if k not in ('id','revision','local_service_id')}
                catalog.put('model-profile',profile['id'],value,profile['revision'])
            catalog.put('local-model-service', sid, {'deleted': True}, expected_revision)
            drop_unused_key(old.get('credential_ref'))
        return {'removed': True}


async def inventory(item):
    if item['kind'] == 'ollama':
        tags = await request_json(item, 'GET', '/api/tags')
        running = await request_json(item, 'GET', '/api/ps')
        if not isinstance(tags.get('models'), list) or not isinstance(running.get('models'), list):
            raise HTTPException(502, '服务返回的模型列表格式有误')
        loaded = {i.get('name') or i.get('model'): i for i in running['models'] if isinstance(i, dict)}
        rows = []
        for m in tags.get('models', [])[:5000]:
            if not isinstance(m, dict):
                continue
            name = m.get('name') or m.get('model')
            if not isinstance(name, str) or not _MODEL.fullmatch(name):
                continue
            run = loaded.get(name)
            rows.append({'id': name, 'size': m.get('size'), 'digest': m.get('digest', ''), 'loaded': run is not None,
                         'memory': run.get('size_vram') if run else None, 'modified_at': m.get('modified_at', '')})
        return rows
    data = await request_json(item, 'GET', '/v1/models')
    if not isinstance(data.get('data'), list):
        raise HTTPException(502, '服务未返回兼容的模型列表，请检查地址')
    return [{'id': m['id'], 'size': None, 'loaded': None, 'digest': '', 'memory': None}
            for m in data.get('data', [])[:5000] if isinstance(m, dict) and isinstance(m.get('id'), str) and _MODEL.fullmatch(m['id'])]


@router.get('/services/{sid}/models')
async def models_list(sid: str):
    async with lock(sid):
        item = service(sid)
        rows = await inventory(item)
        return {'items': rows, 'connected': True, 'service_revision': item['revision'], 'can_manage': item['kind'] == 'ollama'}


class ModelBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


def ollama(item):
    if item['kind'] != 'ollama':
        raise HTTPException(422, '此操作需要 Ollama 服务')


@router.post('/services/{sid}/load')
async def load(sid: str, body: ModelBody):
    return await loading(sid, body, True)


@router.post('/services/{sid}/unload')
async def unload(sid: str, body: ModelBody):
    return await loading(sid, body, False)


async def loading(sid, body, loaded):
    async with lock(sid):
        item = service(sid, body.expected_revision); ollama(item)
        name = model_name(body.model)
        if not any(m['id'] == name for m in await inventory(item)):
            raise HTTPException(404, '模型尚未下载')
        await request_json(item, 'POST', '/api/generate', {'model': name, 'prompt': '', 'stream': False, 'keep_alive': '5m' if loaded else 0}, timeout=180)
        return {'requested': True}


class ConfigureBody(ModelBody):
    name: str = Field(min_length=1, max_length=100)
    capabilities: list[Literal['text', 'vision']] = Field(min_length=1, max_length=2)
    profile_id: str | None = None
    profile_revision: int = Field(default=0, ge=0)


def linked_profile(sid, name, item):
    matches = [p for p in catalog.listing('model-profile')
               if p.get('local_service_id') == sid and p.get('model') == name
               and p.get('credential_ref') == item.get('credential_ref', '')
               and p.get('base_url') == item['base_url'] + '/v1']
    return matches[-1] if matches else None


@router.post('/services/{sid}/configure')
async def configure(sid: str, body: ConfigureBody):
    async with lock(sid):
        item = service(sid, body.expected_revision)
        name = model_name(body.model)
        if not any(m['id'] == name for m in await inventory(item)):
            raise HTTPException(404, '服务未提供此模型，请刷新')
        title = body.name.strip()
        if not title:
            raise HTTPException(422, '请输入配置名称')
        caps = list(dict.fromkeys(body.capabilities))
        existing = linked_profile(sid, name, item)
        with credential_lifecycle.mutation():
            service(sid, body.expected_revision)
            if body.profile_id:
                if not existing or existing['id'] != body.profile_id or existing['revision'] != body.profile_revision:
                    raise HTTPException(409, '模型配置已变化，请刷新')
                update = model_profiles.ProfileUpdate(
                    name=title, expected_revision=body.profile_revision,
                    temperature=existing.get('temperature', .7),
                    timeout=existing.get('timeout', 90),
                    capabilities=caps, model=name,
                    base_url=existing['base_url'],
                    protocol=existing.get('protocol', 'openai'))
                return model_profiles.update_configuration(existing['id'], update)
            if existing:
                raise HTTPException(409, '模型已配置，请刷新后编辑')
            doc = modules.Module(base_url=item['base_url'] + '/v1', model=name, local=True,
                                 credential_ref=item.get('credential_ref', ''), capabilities=caps).model_dump(exclude={'expected_revision'})
            doc.update(name=title, enabled=True, capability_status='unverified',
                       capability_source='manual', local_service_id=sid)
            return model_profiles.public(catalog.put('model-profile', uuid4().hex, doc, 0))


class AssignBody(ModelBody):
    module: Literal['companion', 'work', 'orchestrator', 'vision']
    module_revision: int = Field(ge=0)
    vision: bool = False


@router.post('/services/{sid}/assign')
async def assign(sid: str, body: AssignBody):
    async with lock(sid):
        item = service(sid, body.expected_revision)
        name = model_name(body.model)
        if not any(m['id'] == name for m in await inventory(item)):
            raise HTTPException(404, '服务未提供此模型，请刷新')
        configured = linked_profile(sid, name, item)
        caps = configured.get('capabilities', []) if configured else ['text'] + (['vision'] if body.vision else [])
        source = configured.get('capability_source', 'manual') if configured else 'manual'
        if not configured and item['kind'] == 'ollama':
            detail = await request_json(item, 'POST', '/api/show', {'model': name})
            reported = detail.get('capabilities')
            if isinstance(reported, list) and reported:
                caps = (['text'] if 'completion' in reported else []) + (['vision'] if 'vision' in reported else [])
                source = 'service'
        if modules.KINDS[body.module] not in caps:
            raise HTTPException(422, '此模型未声明所需能力，请选择兼容模型')
        # Profile creation and assignment must roll back together on stale module state.
        with credential_lifecycle.mutation():
            service(sid, body.expected_revision)
            doc = modules.Module(base_url=item['base_url'] + '/v1', model=name, local=True,
                                 credential_ref=item.get('credential_ref', ''), capabilities=caps).model_dump(exclude={'expected_revision'})
            doc.update(name=(item['name'] + ' · ' + name)[:100], enabled=True, capability_status='unverified', capability_source=source,
                       local_service_id=sid)
            profile = configured if configured else catalog.put('model-profile', uuid4().hex, doc, 0)
            result = model_profiles.assign(body.module, model_profiles.Assignment(profile_id=profile['id'], expected_revision=body.module_revision))
        return {'assigned': True, 'module': result}


def owner(request):
    return ui_auth.sessions.principal(request).owner_session_id


@router.post('/services/{sid}/delete-preview')
async def delete_preview(sid: str, body: ModelBody, request: Request):
    async with lock(sid):
        item = service(sid, body.expected_revision); ollama(item); idle(sid)
        name = model_name(body.model)
        row = next((m for m in await inventory(item) if m['id'] == name), None)
        if not row:
            raise HTTPException(404, '模型不存在')
        check_usage(item, name)
        now = time.monotonic()
        for token, value in list(_previews.items()):
            if value['expires'] < now:
                _previews.pop(token, None)
        if len(_previews) >= 128:
            raise HTTPException(429, '待确认操作过多，请稍后重试')
        token = secrets.token_urlsafe(32)
        _previews[token] = {'owner': owner(request), 'expires': now + 120, 'sid': sid, 'revision': item['revision'], 'model': name, 'digest': row['digest']}
        return {'token': token, 'model': name, 'size': row['size'], 'loaded': row['loaded']}


def check_usage(item, name):
    from app import model_references
    model_references.assert_idle()
    def same_model(value):
        def canonical(n): return n if ':' in n.rsplit('/', 1)[-1] else n + ':latest'
        return canonical(value) == canonical(name) if item['kind'] == 'ollama' else value == name
    profiles = []
    for p in catalog.listing('model-profile'):
        try: same = address(p.get('base_url', '')) == item['base_url']
        except HTTPException: same = False
        if same and same_model(p.get('model', '')): profiles.append(p)
    routes = {'profile_' + p['id'] + '/' + p['model'] for p in profiles}
    for m in modules.modules()['items']:
        if not same_model(m.get('model', '')):
            continue
        try:
            same_service = address(m.get('base_url', '')) == item['base_url']
        except HTTPException:
            same_service = False
        if same_service:
            raise HTTPException(409, '仍有模块使用此模型，请先为它选择其他模型')
    model_references.assert_unused(routes, [p['id'] for p in profiles])


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=20, max_length=100)


@router.post('/services/{sid}/delete')
async def delete_model(sid: str, body: Confirm, request: Request):
    async with lock(sid):
        value = _previews.get(body.token)
        if not value or value['owner'] != owner(request) or value['sid'] != sid or value['expires'] < time.monotonic():
            raise HTTPException(409, '删除确认已失效，请重新操作')
        _previews.pop(body.token)
        item = service(sid, value['revision']); ollama(item); idle(sid)
        row = next((m for m in await inventory(item) if m['id'] == value['model']), None)
        if not row or row['digest'] != value['digest']:
            raise HTTPException(409, '模型已变化，请重新确认')
        check_usage(item, value['model'])
        await request_json(item, 'DELETE', '/api/delete', {'model': value['model']})
        return {'deleted': True}


def job_public(job):
    return {k: v for k, v in job.items() if k not in ('layers', 'revision')}


@router.get('/downloads')
def downloads(limit: int = 30, before: str = ''):
    if not 1 <= limit <= 100 or len(before) > 100: raise HTTPException(422, '分页参数有误')
    with catalog.read_connection() as db:
        cursor = db.execute("SELECT updated_at FROM capability_documents WHERE kind='local-model-download' AND id=?", (before,)).fetchone() if before else None
        if before and not cursor: raise HTTPException(422, '下载分页位置已失效，请刷新')
        where = " AND (updated_at<? OR (updated_at=? AND id<?))" if cursor else ''
        args = (cursor[0], cursor[0], before, limit + 1) if cursor else (limit + 1,)
        entries = db.execute("SELECT id,document FROM capability_documents WHERE kind='local-model-download'" + where + " ORDER BY updated_at DESC,id DESC LIMIT ?", args).fetchall()
    rows = {i['id']: {**json.loads(i['document']), 'id': i['id']} for i in entries[:limit]}
    for key, value in rows.items():
        if key not in _jobs and value['status'] in ('queued', 'downloading'):
            value.update(status='interrupted', phase='下载已中断，可重新下载继续')
    for key in rows:
        if key in _jobs: rows[key] = _jobs[key]
    active = [job_public(j) for j in _jobs.values() if j['status'] in ('queued', 'downloading')]
    items = list({j['id']: j for j in active + [job_public(i) for i in rows.values()]}.values())
    return {'items': items, 'next_cursor': entries[limit - 1]['id'] if len(entries) > limit else ''}


def persist(job):
    saved = catalog.put('local-model-download', job['id'], job_public(job), job.get('revision', 0))
    job['revision'] = saved['revision']


async def pull(item, job):
    job.update(status='downloading', phase='连接服务')
    try:
        async with asyncio.timeout(24 * 3600):
            async with client(item, httpx2.Timeout(120, connect=10)) as c:
                async with c.stream('POST', address(item['base_url']).rstrip('/') + '/api/pull', json={'model': job['model'], 'stream': True}) as r:
                    if r.status_code != 200:
                        raise RuntimeError('下载服务拒绝请求，请检查模型名称和服务')
                    buffer = b''; success = False
                    async for chunk in r.aiter_bytes():
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if len(line) > 65536:
                                raise RuntimeError('下载进度格式异常')
                            if line.strip():
                                success = progress(job, json.loads(line)) or success
                        if len(buffer) > 65536:
                            raise RuntimeError('下载进度格式异常')
                    if buffer.strip():
                        success = progress(job, json.loads(buffer)) or success
                    if not success:
                        raise RuntimeError('下载连接提前结束，可重新下载继续')
        job.update(status='complete', phase='下载完成')
    except asyncio.CancelledError:
        job.update(status='cancelled', phase='已取消下载请求')
    except Exception as error:
        job.update(status='failed', phase='下载失败', error=str(error) if type(error) is RuntimeError else '下载连接中断，请检查本地服务后重试')
    finally:
        persist(job)
        _tasks.pop(job['id'], None)


def progress(job, value):
    if not isinstance(value, dict) or value.get('error'):
        raise RuntimeError('下载未完成，请检查模型名称和服务')
    digest = value.get('digest')
    if digest and isinstance(value.get('total'), int) and isinstance(value.get('completed', 0), int):
        if digest not in job['layers'] and len(job['layers']) >= 256:
            raise RuntimeError('下载分层数量异常')
        total = max(0, value['total']); completed = min(total, max(0, value.get('completed', 0)))
        job['layers'][digest] = (total, completed)
        job.update(total_bytes=sum(i[0] for i in job['layers'].values()), completed_bytes=sum(i[1] for i in job['layers'].values()))
    state = str(value.get('status', ''))
    job['phase'] = '下载完成' if state == 'success' else '正在下载' if state.startswith('pulling') else '正在校验与保存'
    return state == 'success'


@router.post('/services/{sid}/download')
async def start_download(sid: str, body: ModelBody):
    async with lock(sid):
        item = service(sid, body.expected_revision); ollama(item)
        name = model_name(body.model)
        prior = next((j for j in active_jobs(sid) if j['model'] == name), None)
        if prior:
            return job_public(prior)
        if len(_tasks) >= 2:
            raise HTTPException(409, '已有两个下载，请完成或取消后再试')
        # Bound in-memory history; persisted recent history remains available after restart.
        for key in list(_jobs):
            if len(_jobs) < 100:
                break
            if key not in _tasks:
                _jobs.pop(key)
        job = {'id': uuid4().hex, 'service_id': sid, 'model': name, 'status': 'queued', 'phase': '等待下载',
               'completed_bytes': 0, 'total_bytes': 0, 'error': '', 'layers': {}}
        persist(job); _jobs[job['id']] = job
        _tasks[job['id']] = asyncio.create_task(pull(item, job))
        def finished(task):
            if task.cancelled() and job['status'] in ('queued', 'downloading'):
                job.update(status='cancelled', phase='已取消下载请求')
                persist(job)
            _tasks.pop(job['id'], None)
        _tasks[job['id']].add_done_callback(finished)
        return job_public(job)


@router.post('/downloads/{jid}/cancel')
async def cancel_download(jid: str):
    task = _tasks.get(jid)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {'cancelled': task is not None}


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _previews.clear(); _locks.clear(); _jobs.clear()
