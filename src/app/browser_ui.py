"""Explicit controls for the app's existing dedicated browser, separate from personal profiles."""
import os
from pathlib import Path
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from playwright.async_api import Error as BrowserError
from app import browser, catalog


@catalog.router.get('/browser/status')
def status():
    candidates = [Path(os.environ.get(name, 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe'
                  for name in ('PROGRAMFILES(X86)', 'PROGRAMFILES')]
    return {'installed': any(path.is_file() for path in candidates), 'running': browser._context is not None,
            'target': 'dedicated', 'note': '仅使用零汐独立浏览器资料，不连接个人浏览器；个人浏览器请配置 MCP。'}


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['open', 'read', 'play', 'pause', 'close']
    url: str = Field(default='', max_length=4000)


@catalog.router.post('/browser/operate')
async def operate(body: Operation):
    try:
        return await browser.execute(body.action, body.url)
    except (ValueError, RuntimeError, TimeoutError, BrowserError) as error:
        raise HTTPException(409, str(error)) from error
