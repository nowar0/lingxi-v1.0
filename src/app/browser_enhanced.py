"""Work-scoped Edge controls with state-bound action previews and handoff."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from playwright.async_api import Error as BrowserError
from pydantic import BaseModel, ConfigDict, Field

from app import advanced_approval, artifacts, browser, catalog, config, media

_active = {}
_ELEMENTS = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[contenteditable=true]'
_CONTROL_KEYS = {'Enter', 'Escape', 'Tab', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
                 'Home', 'End', 'PageUp', 'PageDown', 'Space'}


class Session(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)


class Action(Session):
    action: Literal['open', 'new_tab', 'switch_tab', 'close_tab', 'close', 'click', 'fill', 'select',
                    'press', 'scroll', 'wait', 'upload', 'download', 'script', 'take_over', 'return']
    url: str = Field(default='', max_length=4000)
    tab_id: str = Field(default='', max_length=32)
    ref: int = Field(default=0, ge=0, le=120)
    value: str = Field(default='', max_length=8000)
    attachment_id: str = Field(default='', max_length=100)
    delta: int = Field(default=600, ge=-2500, le=2500)


def navigation_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or not 1 <= len(value) <= 4000 or any(ord(c) < 32 for c in value)):
        raise HTTPException(422, '浏览器只打开不含凭据的 HTTP(S) 地址')
    return value


def web_destination(url):
    if url == 'about:blank':
        return
    navigation_url(url)


class WorkBrowser:
    def __init__(self, playwright, context):
        self.playwright, self.context = playwright, context
        self.id = uuid4().hex
        self.selected = None
        self.takeover = False
        self.epoch = 0
        self.tabs = {}
        self.armed = None
        for page in context.pages:
            self.attach(page)
        context.on('page', self.attach)

    def attach(self, page):
        self.tabs[uuid4().hex] = page
        if self.selected is None:
            self.selected = page
        self.epoch += 1
        page.on('download', lambda download: self.unapproved_download(page, download))
        page.on('framenavigated', lambda frame: self.navigation(page, frame))
        page.on('close', lambda: self.closed(page))

    def navigation(self, page, frame):
        if frame == page.main_frame:
            self.epoch += 1

    def closed(self, page):
        self.tabs = {key: value for key, value in self.tabs.items() if value != page}
        if self.selected == page:
            self.selected = next(iter(self.tabs.values()), None)
        self.epoch += 1

    async def unapproved_download(self, page, download):
        if self.armed != page:
            try:
                await download.cancel()
            except BrowserError:
                pass
        else:
            self.armed = None

    async def snapshot(self):
        page = self.selected
        if page is None or page.is_closed():
            return {'available': True, 'running': True, 'url': '', 'tabs': [], 'elements': [],
                    'takeover': self.takeover, 'fingerprint': self.id+':'+str(self.epoch)}
        web_destination(page.url)
        visible = await page.locator(_ELEMENTS).evaluate_all("""nodes => nodes.slice(0,120).map((el, i) => ({
            ref:i+1, tag:el.tagName.toLowerCase(), type:el.getAttribute('type')||'',
            text:(el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.innerText||el.name||'').trim().slice(0,180),
            href:(el.getAttribute('href')||'').slice(0,500),
            name:(el.getAttribute('name')||el.id||'').slice(0,120),
            form:el.form ? (el.form.getAttribute('action')||'').slice(0,500) : '',
            method:el.form?.method || '',
            visible:!!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            disabled:!!el.disabled
        }))""")
        tabs = [{'id': key, 'url': tab.url, 'selected': tab == page} for key, tab in self.tabs.items() if not tab.is_closed()]
        title = await page.title()
        text = (await page.locator('body').inner_text(timeout=4000))[:15000]
        fingerprint = hashlib.sha256(json.dumps([self.id, self.epoch, page.url, tabs, title, text, visible],
                                                 sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {'available': True, 'running': True, 'url': page.url, 'title': title, 'tabs': tabs,
                'elements': [item for item in visible if item['visible']], 'text': text,
                'takeover': self.takeover, 'fingerprint': fingerprint, 'untrusted_content': True}

    async def close(self):
        await self.context.close()
        await self.playwright.stop()


async def spawn(session_id):
    if len(_active) >= 3:
        raise HTTPException(409, '最多打开 3 个工作浏览器，请先关闭一个')
    from app.browser_runtime import configure_playwright_driver
    configure_playwright_driver()
    from playwright.async_api import async_playwright
    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(config.DATA_DIR/'browser-workspaces'/str(session_id)), channel='msedge',
            headless=os.environ.get('LINGXI_QA_BROWSER_HEADLESS') == '1',
            accept_downloads=True, args=['--no-first-run'])
    except BaseException:
        await playwright.stop()
        raise
    value = WorkBrowser(playwright, context)
    _active[session_id] = value
    return value


async def state(session_id):
    value = _active.get(session_id)
    return await value.snapshot() if value else {'available': True, 'running': False, 'url': '', 'tabs': [],
        'elements': [], 'takeover': False, 'fingerprint': 'closed'}


def matching(current, body):
    row = next((element for element in current['elements'] if element['ref'] == body.ref), None)
    if body.action in ('click', 'fill', 'select', 'upload', 'download') or body.action == 'wait' and body.ref:
        if row is None or row['disabled']:
            raise HTTPException(409, '请先从当前页面快照选择可用元素')
    return row


def check(current, body):
    if body.action in ('open', 'new_tab'):
        if body.url or body.action == 'open':
            navigation_url(body.url)
    elif not current['running']:
        raise HTTPException(409, '请先打开网页')
    if current['takeover'] and body.action not in ('return', 'close'):
        raise HTTPException(409, '浏览器由你接管，请先归还控制')
    if body.action == 'press' and body.value not in _CONTROL_KEYS:
        raise HTTPException(422, '按键只支持列表中的单键')
    if body.action == 'script' and not body.value.strip():
        raise HTTPException(422, '请输入要运行的页面脚本')
    if body.action in ('switch_tab', 'close_tab') and body.tab_id not in {tab['id'] for tab in current['tabs']}:
        raise HTTPException(409, '标签已关闭或变化，请刷新')
    if body.action == 'upload' and not body.attachment_id:
        raise HTTPException(422, '请从当前会话选择要上传的附件')
    row = matching(current, body)
    if body.action == 'press' and body.ref and not row:
        raise HTTPException(409, '按键目标已离开当前快照')
    if body.action == 'upload' and row.get('type') != 'file':
        raise HTTPException(422, '上传目标须是网页文件输入框')
    return row


async def apply(body):
    sid = body.session_id
    value = _active.get(sid)
    action = body.action
    if action in ('open', 'new_tab'):
        value = value or await spawn(sid)
        page = await value.context.new_page() if action == 'new_tab' or value.selected is None else value.selected
        value.selected = page
        if body.url:
            await page.goto(body.url, wait_until='domcontentloaded', timeout=15000)
            web_destination(page.url)
        await page.bring_to_front()
    elif action == 'close':
        _active.pop(sid)
        await value.close()
    elif action in ('switch_tab', 'close_tab'):
        page = value.tabs[body.tab_id]
        if action == 'switch_tab':
            value.selected = page
            await page.bring_to_front()
        else:
            await page.close()
    elif action in ('take_over', 'return'):
        value.takeover = action == 'take_over'
        if value.takeover and value.selected:
            await value.selected.bring_to_front()
    else:
        page = value.selected
        if page is None:
            raise HTTPException(409, '没有打开的标签')
        element = page.locator(_ELEMENTS).nth(body.ref-1) if body.ref else None
        if action == 'click':
            await element.click(timeout=6000)
        elif action == 'fill':
            await element.fill(body.value, timeout=6000)
        elif action == 'select':
            await element.select_option(label=body.value, timeout=6000)
        elif action == 'press':
            await (element.press(body.value, timeout=6000) if element is not None else page.keyboard.press(body.value))
        elif action == 'scroll':
            await page.mouse.wheel(0, body.delta)
        elif action == 'wait':
            if element is None:
                await page.wait_for_load_state('domcontentloaded', timeout=8000)
            else:
                await element.wait_for(state='visible', timeout=8000)
        elif action == 'upload':
            row, path = media.read(body.attachment_id, sid)
            await element.set_input_files({'name': Path(row['name']).name, 'mimeType': row['mime'],
                                           'buffer': path.read_bytes()}, timeout=8000)
        elif action == 'download':
            value.armed = page
            try:
                async with asyncio.timeout(22):
                    async with page.expect_download(timeout=8000) as result:
                        await element.click(timeout=6000)
                    download = await result.value
                    try:
                        path = Path(await download.path())
                        if path.stat().st_size > media.LIMIT:
                            raise HTTPException(413, '下载超过 15 MiB，已丢弃')
                        raw = path.read_bytes()
                        name = Path(download.suggested_filename).name or '浏览器下载'
                        item = artifacts.store(raw, 'application/octet-stream', sid, name,
                            {'kind': 'browser-download', 'page': page.url, 'url': download.url})
                        value.epoch += 1
                        return {'artifact': item, 'browser': await state(sid)}
                    finally:
                        await download.delete()
            finally:
                value.armed = None
        elif action == 'script':
            async with asyncio.timeout(8):
                result = await page.evaluate(body.value)
            value.epoch += 1
            return {'result': str(result)[:8000], 'browser': await state(sid)}
    if value:
        value.epoch += 1
    return {'browser': await state(sid)}


async def in_browser(fn):
    async def guarded():
        async with browser._async_lock:
            return await fn()
    future = asyncio.run_coroutine_threadsafe(guarded(), browser.loop())
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), 35)
    except (ValueError, OSError, TimeoutError, BrowserError) as exc:
        future.cancel()
        raise HTTPException(409, '浏览器操作未完成：' + str(exc)[:400]) from exc
    except BaseException:
        future.cancel()
        raise


@catalog.router.get('/browser/workspace')
async def workspace_status(session_id: int):
    advanced_approval.require_work(session_id)
    return await in_browser(lambda: state(session_id))


@catalog.router.post('/browser/workspace/preview')
async def preview(body: Action):
    advanced_approval.require_work(body.session_id)
    async def prepare():
        current = await state(body.session_id)
        target = check(current, body)
        attachment = None
        if body.action == 'upload':
            row, _ = media.read(body.attachment_id, body.session_id)
            attachment = {'name': row['name'], 'sha256': row['sha256']}
        return advanced_approval.prepare('browser', body.session_id, current['fingerprint'], (body, attachment),
            {'action': body.model_dump(), 'current_url': current['url'], 'element': target, 'attachment': attachment})
    return await in_browser(prepare)


@catalog.router.post('/browser/workspace/commit')
async def commit(body: advanced_approval.Commit):
    async def execute():
        current = await state(body.session_id)
        action, attachment = advanced_approval.take('browser', body, current['fingerprint'])
        check(current, action)
        if attachment:
            row, _ = media.read(action.attachment_id, action.session_id)
            if row['sha256'] != attachment['sha256']:
                raise HTTPException(409, '附件已变化，请重新预览')
        return await apply(action)
    return await in_browser(execute)


@catalog.router.post('/browser/workspace/screenshot')
async def screenshot(body: Session):
    advanced_approval.require_work(body.session_id)
    async def capture():
        value = _active.get(body.session_id)
        if not value or not value.selected:
            raise HTTPException(409, '请先打开网页')
        page = value.selected
        web_destination(page.url)
        raw = await page.screenshot(timeout=8000)
        artifact = artifacts.store(raw, 'image/png', body.session_id, '网页截图.png',
                                   {'kind': 'browser-screenshot', 'url': page.url})
        return {'artifact': artifact}
    return await in_browser(capture)


async def execute_tool(session_id, args):
    advanced_approval.require_work(session_id)
    supplied = dict(args)
    fingerprint = supplied.pop('snapshot', '')
    async def execute():
        current = await state(session_id)
        if supplied.get('action') == 'snapshot':
            return current
        body = Action(session_id=session_id, **supplied)
        if current['running'] and fingerprint != current['fingerprint']:
            raise ValueError('浏览器状态已变化，请先重新读取快照')
        check(current, body)
        return await apply(body)
    return await in_browser(execute)


async def shutdown():
    if not _active:
        return
    async def close_all():
        for session_id, value in list(_active.items()):
            _active.pop(session_id, None)
            try:
                await value.close()
            except BrowserError:
                pass
    await in_browser(close_all)
