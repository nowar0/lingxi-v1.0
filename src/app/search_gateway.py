"""Search providers, scoped cache and compact audit records."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import time
from typing import Literal
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import catalog, config, sessions
from app.db import connect
from app.tools import web_tools

_NAMES = {'bing': 'Bing RSS', 'searxng': 'SearXNG', 'tavily': 'Tavily'}
_KEY_ID = 'lingxi-search-tavily'


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    primary: Literal['bing', 'searxng', 'tavily'] = 'bing'
    fallback: Literal['none', 'bing', 'searxng', 'tavily'] = 'none'
    searxng_url: str = Field(default='', max_length=500)
    cache_seconds: int = Field(default=300, ge=0, le=3600)
    expected_revision: int = Field(default=0, ge=0)


class Search(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=500)
    site: str = Field(default='', max_length=250)
    days: int = Field(default=0, ge=0, le=365)


class SearchInput(Search):
    session_id: int = Field(gt=0)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    url: str = Field(min_length=1, max_length=3000)


class Secret(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: str = Field(min_length=1, max_length=500)


def init_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS web_search_cache(
            cache_key TEXT PRIMARY KEY, expires REAL NOT NULL, result TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS web_search_history(
            id INTEGER PRIMARY KEY, session_id INTEGER, provider TEXT, status TEXT,
            results INTEGER, latency INTEGER, cached INTEGER, at TEXT DEFAULT CURRENT_TIMESTAMP)''')


def configuration():
    try:
        return catalog.get('search-settings', 'settings')
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return {**Settings().model_dump(exclude={'expected_revision'}), 'revision': 0}


@catalog.router.get('/search/settings')
def settings():
    return {**configuration(), 'tavily_key_saved': bool(config.load_key(_KEY_ID))}


@catalog.router.put('/search/settings')
def save(body: Settings):
    if body.primary == body.fallback:
        raise HTTPException(422, '主搜索源与备用源必须不同')
    if 'searxng' in (body.primary, body.fallback) or body.searxng_url:
        parsed = urlsplit(body.searxng_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            raise HTTPException(422, '请输入可公开访问的 SearXNG HTTPS 根地址')
    return catalog.put('search-settings', 'settings', body.model_dump(exclude={'expected_revision'}), body.expected_revision)


@catalog.router.put('/search/tavily-key')
def set_key(body: Secret):
    if not re.fullmatch(r'[!-~]+', body.api_key):
        raise HTTPException(422, '密钥含有不可用字符')
    config.save_key(_KEY_ID, body.api_key)
    with connect() as db:
        db.execute('DELETE FROM web_search_cache')
    return {'saved': True}


@catalog.router.delete('/search/tavily-key')
def delete_key():
    config.delete_key(_KEY_ID)
    with connect() as db:
        db.execute('DELETE FROM web_search_cache')
    return {'saved': False}


def timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, OverflowError):
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat()


def item(title, url, snippet, date):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
        return None
    parser = web_tools.Text()
    parser.feed(str(snippet))
    return {'title': str(title)[:200], 'url': url[:2000], 'snippet': ''.join(parser.parts)[:800],
            'published_at': timestamp(date)}


def provider(kind, body, site, cfg, seconds):
    query = body.query.strip()
    if kind == 'bing':
        rss = urlencode({'format': 'rss', 'q': ('site:'+site+' ' if site else '') + query})
        raw, _, _ = web_tools.public_get('https://www.bing.com/search?' + rss, 512 * 1024, timeout=seconds)
        root = ET.fromstring(raw)
        return [item(row.findtext('title', ''), row.findtext('link', ''), row.findtext('description', ''), row.findtext('pubDate'))
                for row in root.findall('.//item')[:20]]
    if kind == 'searxng':
        query = {'q': ('site:'+site+' ' if site else '') + query, 'format': 'json'}
        if body.days:
            query['time_range'] = 'day' if body.days == 1 else 'week' if body.days <= 7 else 'month' if body.days <= 30 else 'year'
        raw, _, _ = web_tools.public_get(cfg['searxng_url'].rstrip('/') + '/search?' + urlencode(query),
                                        512 * 1024, timeout=seconds)
    else:
        key = config.load_key(_KEY_ID)
        if not key:
            raise ValueError('Tavily 尚未保存密钥')
        request = {'query': query, 'search_depth': 'basic', 'max_results': 10, 'include_published_date': True}
        if site:
            request['include_domains'] = [site]
        if body.days:
            request['start_date'] = (datetime.now(timezone.utc) - timedelta(days=body.days)).date().isoformat()
            request['filter_by_published_date'] = True
        deadline = time.monotonic() + seconds
        host = 'api.tavily.com'
        address = web_tools._resolve(host, 443, deadline)[0]
        status, _, _, raw = web_tools._pinned_get(host, address, 443, True, '/search', 'application/json',
            512 * 1024, deadline, request_body=json.dumps(request).encode(), authorization='Bearer '+key)
        if status != 200:
            raise ValueError(f'Tavily 返回 HTTP {status}')
    payload = json.loads(raw)
    return [item(row.get('title', ''), row.get('url', ''), row.get('content', ''),
                 row.get('published_date') or row.get('publishedDate')) for row in payload['results'][:20]]


def audit(session_id, value, started, cached):
    with connect() as db:
        db.execute('INSERT INTO web_search_history(session_id,provider,status,results,latency,cached) VALUES(?,?,?,?,?,?)',
                   (session_id, value.get('provider'), value.get('status'), len(value['results']),
                    round((time.monotonic()-started)*1000), int(cached)))
        db.execute('DELETE FROM web_search_history WHERE id NOT IN (SELECT id FROM web_search_history ORDER BY id DESC LIMIT 500)')


def search_request(body, session_id=None, *, check=False):
    query = body.query.strip()
    site = body.site.strip().lower().rstrip('.').encode('idna').decode('ascii')
    if not query or site and not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}', site):
        raise ValueError('请填写搜索词，并使用域名形式的站点筛选')
    cfg = configuration()
    key = hashlib.sha256(json.dumps([session_id, cfg, body.model_dump()], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    started = time.monotonic()
    if cfg['cache_seconds'] and not check:
        with connect() as db:
            row = db.execute('SELECT result FROM web_search_cache WHERE cache_key=? AND expires>?', (key, time.time())).fetchone()
        if row:
            result = {**json.loads(row['result']), 'cached': True}
            audit(session_id, result, started, True)
            return result
    result = {'query': query, 'site': site, 'days': body.days, 'results': [], 'provider': None,
              'attempts': [], 'cached': False, 'status': 'unavailable', 'untrusted_content': True}
    earliest = (datetime.now(timezone.utc)-timedelta(days=body.days)).isoformat()
    routes = [kind for kind in (cfg['primary'], cfg['fallback']) if kind != 'none']
    for kind in routes:
        name = _NAMES[kind]
        try:
            found = provider(kind, body, site, cfg, 9 if len(routes)>1 else 17)
            valid = []
            seen = set()
            for row in (row for row in found if row):
                host = urlsplit(row['url']).hostname.lower()
                if site and host != site and not host.endswith('.'+site):
                    continue
                if body.days and (not row['published_at'] or row['published_at'] < earliest):
                    continue
                if row['url'] not in seen:
                    seen.add(row['url'])
                    valid.append(row)
            result['attempts'].append({'provider': name, 'status': 'ok' if valid else 'empty'})
            result.update(provider=name, results=valid[:5], status='ok' if valid else 'empty')
            if valid:
                break
        except (ValueError, OSError, KeyError, TypeError, ET.ParseError):
            result['attempts'].append({'provider': name, 'status': 'unavailable'})
    audit(session_id, result, started, False)
    if cfg['cache_seconds'] and result['status'] == 'ok' and not check:
        with connect() as db:
            db.execute('INSERT OR REPLACE INTO web_search_cache VALUES(?,?,?)',
                       (key, time.time()+cfg['cache_seconds'], json.dumps(result, ensure_ascii=False)))
            db.execute('DELETE FROM web_search_cache WHERE expires<=? OR cache_key NOT IN '
                       '(SELECT cache_key FROM web_search_cache ORDER BY expires DESC LIMIT 200)', (time.time(),))
    return result


@catalog.router.post('/search/query')
async def query(body: SearchInput, check: bool = False):
    session = sessions.get_session(body.session_id)
    if not session or session.get('archived'):
        raise HTTPException(404, '请选择一个可用会话')
    try:
        return await asyncio.to_thread(search_request, Search.model_validate(body.model_dump(exclude={'session_id'})),
                                       body.session_id, check=check)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@catalog.router.get('/search/history')
def history(session_id: int = Query(gt=0)):
    if not sessions.get_session(session_id):
        raise HTTPException(404, '会话不存在')
    with connect() as db:
        return {'items': [dict(row) for row in db.execute(
            'SELECT provider,status,results,latency,cached,at FROM web_search_history WHERE session_id=? ORDER BY id DESC LIMIT 30',
            (session_id,))]}


@catalog.router.post('/search/read')
async def read_page(body: ReadInput):
    session = sessions.get_session(body.session_id)
    if not session or session.get('archived'):
        raise HTTPException(404, '请选择一个可用会话')
    try:
        return json.loads(await asyncio.to_thread(web_tools.fetch, {}, body.url))
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)[:400]) from exc
