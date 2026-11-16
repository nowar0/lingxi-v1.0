"""Versioned conversation controls; no model calls or wider tool authorization."""
from io import BytesIO
import json
from typing import Literal
from uuid import UUID
from zipfile import ZipFile, ZIP_DEFLATED

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, sessions


class Controls(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal: str = Field(default='', max_length=2000)
    plan_mode: bool = False
    permission: Literal['ask', 'readonly', 'workspace', 'full'] = 'ask'
    auto_compact: bool = True
    proactive_compact: bool = True
    auto_ratio: float = Field(default=.85, ge=.6, le=.95)
    proactive_ratio: float = Field(default=.65, ge=.3, le=.85)
    keep_recent_turns: int = Field(default=2, ge=1, le=10)
    summary_model: str = Field(default='', max_length=500)
    expected_revision: int = Field(default=0, ge=0)


class SaveControls(Controls):
    full_access_confirmed: bool = Field(default=False, exclude=True)


def session(sid):
    item = sessions.get_session(sid)
    if not item or item.get('archived') or item['mode'] not in ('companion', 'work'):
        raise HTTPException(404, '会话不存在或已归档')
    return item


@catalog.router.get('/sessions/{sid}/composer')
def get_state(sid: int):
    session(sid)
    defaults = Controls().model_dump(exclude={'expected_revision'})
    from app.customer_management import conversation_defaults
    defaults.update({k:v for k,v in conversation_defaults(sessions.get_session(sid)['mode']).items() if k in defaults})
    if sessions.get_session(sid)['mode']=='work': defaults['permission']='workspace'
    try:
        value=catalog.get('composer-controls', str(sid))
        result={**{key:value.get(key,default) for key,default in defaults.items()},'revision':value['revision']}
        if sessions.get_session(sid)['mode']=='work' and result['permission']=='ask':result['permission']='workspace'
        return result
    except HTTPException as error:
        if error.status_code != 404: raise
    return {**defaults, 'revision': 0}


@catalog.router.put('/sessions/{sid}/composer')
def save_state(sid: int, body: SaveControls):
    from app.extensions import busy
    from app.git_transition import assert_unclaimed
    with catalog.transaction() as db:
        row=session(sid)
        if body.permission in ('workspace','full') and row['mode']!='work':raise HTTPException(422,'扩展执行权限仅用于工作会话')
        if body.permission=='full' and get_state(sid)['permission']!='full' and not getattr(body,'full_access_confirmed',False):
            raise HTTPException(422,'启用完全权限前请勾选已了解风险并确认')
        if busy(db, sid):
            raise HTTPException(409, '请先停止当前会话与任务，再修改目标、计划或权限')
        assert_unclaimed(db, sid)
        if body.proactive_ratio >= body.auto_ratio:
            raise HTTPException(422, '提前压缩阈值必须低于自动压缩阈值')
        if body.summary_model:
            from app.llm import router
            from app.session_options import options_catalog
            if body.summary_model not in options_catalog(sid)['models']:
                raise HTTPException(422, '摘要模型不在可用清单中')
            router.resolve(body.summary_model)
        catalog.put('composer-controls', str(sid), body.model_dump(exclude={'expected_revision'}), body.expected_revision)
        return get_state(sid)


def patch_state(sid, **changes):
    old = get_state(sid)
    return save_state(sid, Controls(**{key: changes.get(key, old.get(key,False)) for key in Controls.model_fields if key != 'expected_revision'}, expected_revision=old['revision']))


def system_notes(sid):
    if not sid: return ''
    value = get_state(sid)
    notes = []
    if value['goal']:
        notes.append('用户设置的当前会话长期目标（不构成额外操作授权）：\n' + value['goal'])
    if value['plan_mode']:
        notes.append('当前为计划模式：先分析、提问和制定可审阅的计划；禁止执行写入、命令、插件副作用或启动任务。退出计划模式后仍须原有逐项审批。')
    elif value['permission'] == 'readonly':
        notes.append('当前会话为只读权限：只能使用已允许的只读工具；不得执行写入、命令、插件副作用或启动任务。')

    elif value['permission']=='workspace':
        notes.append('工作区内修改权限：可以自动修改本会话绑定目录内的文件；目录外文件不允许通过文件工具访问，外部命令和其他副作用仍需逐项确认。')
    elif value['permission']=='full':
        notes.append('用户已确认当前工作会话的完全权限：已登记命令和写入工具可减少逐项确认。仍需遵守任务目标、工具权限、项目范围和插件专用授权；不得自动重放失败的副作用。')

    return '\n' + '\n'.join(notes) if notes else ''


class Feedback(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=36, max_length=36)


@catalog.router.post('/sessions/{sid}/composer/feedback')
def feedback(sid: int, body: Feedback):
    session(sid)
    try: UUID(body.request_id)
    except ValueError: raise HTTPException(422, '反馈请求编号无效') from None
    text = body.text.strip()
    if not text: raise HTTPException(422, '请填写反馈内容')
    with catalog.transaction():
        try:
            prior = catalog.get('composer-feedback', body.request_id)
            if prior['session_id'] != sid or prior['text'] != text:
                raise HTTPException(409, '反馈编号已用于其他内容')
            return prior
        except HTTPException as error:
            if error.status_code != 404: raise
        return catalog.put('composer-feedback', body.request_id, {'session_id': sid, 'text': text}, 0)


@catalog.router.get('/sessions/{sid}/composer/export.zip')
def export_zip(sid: int):
    from app.evolution_api import export_conversation
    from app.session_options import effective
    current = session(sid)
    markdown = export_conversation(sid).body
    options = effective(sid)
    metadata = {'session_id': sid, 'title': current['title'], 'mode': current['mode'],
                'controls': get_state(sid), 'model': options['effective']['model'],
                'effort': options['effective']['effort'],
                'note': '只含可见会话与配置摘要，不含凭据、隐藏推理或原始工具参数。'}
    reports = [item for item in catalog.listing('composer-feedback') if item['session_id'] == sid]
    raw = BytesIO()
    with ZipFile(raw, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('conversation.md', markdown)
        archive.writestr('session.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        archive.writestr('feedback.json', json.dumps(reports, ensure_ascii=False, indent=2))
    return Response(raw.getvalue(), media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="lingxi-session-{sid}.zip"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
