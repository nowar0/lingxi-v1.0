"""Dedicated browser only. Personal browser access is supplied separately via MCP."""
import asyncio
import os
import threading
from concurrent.futures import Future
from urllib.parse import urlsplit

from app import config

_loop = None
_thread = None
_context = None
_playwright = None
_guard = threading.Lock()
_async_lock = None


def loop():
    global _loop, _thread
    with _guard:
        if _loop is None:
            ready = Future()

            def start():
                global _loop, _async_lock
                _loop = asyncio.ProactorEventLoop() if os.name == 'nt' else asyncio.new_event_loop()
                asyncio.set_event_loop(_loop)
                _async_lock = asyncio.Lock()
                ready.set_result(None)
                _loop.run_forever()

            _thread = threading.Thread(target=start, daemon=True, name='lingxi-dedicated-browser')
            _thread.start()
            ready.result(timeout=5)
        return _loop


async def operate(action, url='', headless=False):
    global _context, _playwright
    async with _async_lock:
        if action == 'close':
            if _context:
                await _context.close()
                _context = None
            if _playwright:
                await _playwright.stop()
                _playwright = None
            return {'closed': True, 'target': 'dedicated'}
        if action == 'open':
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('浏览器只接受不含凭据的 HTTP(S) 地址')
        if _context is None:
            from app.browser_runtime import configure_playwright_driver
            configure_playwright_driver()
            from playwright.async_api import async_playwright
            _playwright = await async_playwright().start()
            try:
                # This profile is isolated from the user's usual Edge profile.
                _context = await _playwright.chromium.launch_persistent_context(
                    str(config.DATA_DIR / 'browser-dedicated'), channel='msedge',
                    headless=headless, accept_downloads=False)
            except Exception:
                await _playwright.stop()
                _playwright = None
                raise RuntimeError('专用浏览器启动失败；请确认已安装 Microsoft Edge')
        page = _context.pages[-1] if _context.pages else await _context.new_page()
        if action == 'open':
            await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        elif action in ('play', 'pause'):
            count = await page.locator('video,audio').count()
            if not count:
                raise ValueError('当前页面未发现音视频元素')
            await page.locator('video,audio').first.evaluate(
                'el => el.play()' if action == 'play' else 'el => el.pause()')
        elif action != 'read':
            raise ValueError('未知浏览器动作')
        text = await page.locator('body').inner_text(timeout=5000)
        return {'target': 'dedicated', 'url': page.url, 'title': await page.title(),
                'text': text[:30000], 'truncated': len(text) > 30000, 'action': action}


async def execute(action, url='', headless=False):
    future = asyncio.run_coroutine_threadsafe(operate(action, url, headless), loop())
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=35)
    except BaseException:
        future.cancel()
        raise


async def shutdown():
    global _loop, _thread
    if _loop:
        await execute('close')
        _loop.call_soon_threadsafe(_loop.stop)
        await asyncio.to_thread(_thread.join, 5)
        _loop.close()
        _loop = None
        _thread = None
