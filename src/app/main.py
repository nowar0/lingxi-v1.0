"""HTTP 接口；导入模块不建表，启动时才初始化本地数据。"""

import json
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid4

from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import timeline, queue6, approvals, browser_migrations, card_packages, chat, migrations, persona, sessions, stage5, usage
from app.budget import MAX_USER_CHARS
from app.config import DATA_DIR
from app.llm import router
from app.memory import store as memory
from app.memory import stage7, summarizer, local_vectors
from app import catalog, mcp_access, mcp_client, modules, media, jobs, browser, data_api, work_settings  # noqa: F401 -- route registration imports

from app import work_projects  # noqa: F401 -- work folder groups and persistent pins
from app import local_models
from app import model_profiles  # noqa: F401 -- registers model library routes
from app import context_capacity  # noqa: F401 -- model context budgets
from app import model_keys  # noqa: F401 -- reusable keys and automatic model discovery
from app import local_tts  # noqa: F401 -- registers local speech routes
from app import local_stt  # noqa: F401 -- registers offline speech routes
from app.speech import registry as speech_registry  # noqa: F401 -- lazy contract routes
from app import provider_failover  # noqa: F401 -- explicit opt-in candidate chains
from app import composer_compaction, composer_controls, evolution_api, session_options, handoff, domain_tools, project_docs, work_quality, commands, reminders, model_packages
from app import message_actions, conversation_approvals, package_host, feature_status
from app.memory import v2 as memory_v2, v2_api, source_api, v2_sync
from app import workspace_ui, workspace_search, git_changes, git_commits, artifacts, document_reader, workspace_activity, git_transition
from app import terminal_ui, persistent_terminal
from app import browser_enhanced, enhanced_terminal, office_tools, search_gateway
from app import browser_ui  # noqa: F401 -- registers dedicated browser controls
from app.local_access import LocalAccessMiddleware
from app.package_ui_limits import PackageUiBodyLimitMiddleware
from app.version import VERSION
from app import extensions, extension_jobs, extension_tools, workbench_git, workbench_ssh, workbench_review
from app import ui_auth

app_usage_router = usage.router

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 旧接口暂时保留，兼容原请求。
Mode = Literal["shared", "work", "companion"]

# 新聊天只允许陪伴和工作。
ChatMode = Literal["companion", "work"]

# 记忆仍保留三个范围。
MemoryScope = Literal["shared", "companion", "work"]


@asynccontextmanager
async def lifespan(app):
    ui_auth.sessions.configure_launcher()
    from app.desktop_source_bridge import configure_launcher as configure_source_bridge
    configure_source_bridge()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fresh_personal_data = not (DATA_DIR / 'lingxi.db').exists()
    schema_checkpoint = migrations.prepare_upgrade(DATA_DIR)
    try:
        migrations.ensure_capabilities_backup(DATA_DIR)
        timeline.ensure_backup()
        migrations.ensure_schema_backup(DATA_DIR)
        usage.init_table()
        memory.init_table()
        sessions.init_table()
        stage5.init_table()
        card_packages.init_table()
        browser_migrations.init_table()
        queue6.init_table()
        stage7.init_table()
        timeline.init_table()
        local_vectors.init_table()
        catalog.init_table()
        local_models.reconcile_profiles()
        search_gateway.init_table()
        provider_failover.init_table()
        mcp_access.init_table()
        memory_v2.init_table()
        if fresh_personal_data:
            memory_v2.activate(user_confirmed=True)
        package_host.initialize()
        media.init_table()
        jobs.init_table()
        evolution_api.init_table()
        handoff.init_table()
        domain_tools.init_table()
        work_quality.init_table()
        commands.init_table()
        reminders.init_table()
        project_docs.init_table()
        git_transition.init_table()
        extensions.init_table()
        workbench_review.init_table()
        message_actions.init_table()
        work_projects.init_table()
    except BaseException:
        if schema_checkpoint['migration_required']:
            migrations.finish_upgrade(DATA_DIR / 'lingxi.db', error=True)
        package_host.shutdown()
        raise
    if schema_checkpoint['migration_required']:
        migrations.finish_upgrade(DATA_DIR / 'lingxi.db')
    # One-time journal configuration, after migration/backup completion.
    from app.db import connect
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
    await extension_jobs.start()
    try:
        yield
    finally:
        ui_auth.sessions.close()
        model_host_runtime.close()
        plugin_model_prepared.close()
        package_host.shutdown()
        await local_tts.service.close()
        await local_stt.service.close()
        from app.speech.sidecar import shutdown as shutdown_speech
        await shutdown_speech()
        await git_commits.shutdown()
        await persistent_terminal.shutdown()
        await enhanced_terminal.shutdown()
        await terminal_ui.shutdown()
        await extension_jobs.shutdown()
        project_docs.shutdown()
        await local_models.shutdown()
        await model_packages.shutdown()
        await workbench_ssh.shutdown()
        await extension_tools.shutdown()
        await queue6.shutdown()
        await summarizer.shutdown()
        await jobs.shutdown()
        await browser_enhanced.shutdown()
        await browser.shutdown()


app = FastAPI(title="零汐 App", lifespan=lifespan)
app.add_middleware(PackageUiBodyLimitMiddleware)
app.add_middleware(LocalAccessMiddleware)
from app.ui_auth_middleware import HostUiAuthMiddleware
app.add_middleware(HostUiAuthMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic errors include raw input by default, which can contain an API key.
    errors = [{k: error[k] for k in ('loc', 'msg', 'type') if k in error}
              for error in exc.errors()]
    return JSONResponse(status_code=422, content={'detail': errors})


app.include_router(usage.router)
app.include_router(ui_auth.router)
app.include_router(local_models.router)
from app.desktop_source_bridge import router as desktop_source_router
app.include_router(desktop_source_router)
from app.model_host import HostModelRuntime, create_router as create_model_host_router

def _model_session_authority(principal, session_id):
    if ui_auth.sessions.live(principal) != principal:
        return False
    from app.db import connect
    with connect() as db:
        return db.execute('SELECT 1 FROM sessions WHERE id=?',(session_id,)).fetchone() is not None

model_host_runtime=HostModelRuntime(live_principal=ui_auth.sessions.live,session_authority=_model_session_authority,
    allow_plain_http=True)  # Endpoint is only attempted after its separate per-send HTTP confirmation.
app.include_router(create_model_host_router(model_host_runtime,principal_provider=ui_auth.sessions.principal))
app.include_router(feature_status.router)
from app.plugin_broker_api import create_router as create_plugin_broker_router
app.include_router(create_plugin_broker_router(package_host.initialize,principal_provider=ui_auth.sessions.principal))
app.include_router(package_host.router)
app.include_router(package_host.metadata_router)
from app.connections import router as connections_router
app.include_router(connections_router)
app.include_router(v2_api.router)
app.include_router(source_api.router)
# No implicit directory authorization: this router stays inactive until a trusted
# portable-manager host explicitly supplies an approved program/data root.
from app.program_manager_api import create_router as create_program_manager_router
app.include_router(create_program_manager_router())
from app.model_document_api import create_router as create_model_document_router
app.include_router(create_model_document_router())
from app.plugin_model_host import (PreparedModelRegistry,make_model_host_request_exporter,
    build_service_provider,create_router as create_plugin_model_host_router)
plugin_model_prepared=PreparedModelRegistry(manager_provider=package_host.initialize,
    live_principal=ui_auth.sessions.live,
    model_host_request_provider=make_model_host_request_exporter(lambda:model_host_runtime))
package_host.configure_model_request_provider(plugin_model_prepared.model_request)
plugin_model_services=build_service_provider(package_host.initialize,routes_provider=lambda:model_host_runtime.routes)
app.include_router(create_plugin_model_host_router(service_provider=plugin_model_services,
    principal_provider=ui_auth.sessions.principal,prepared_registry=plugin_model_prepared))
from app.plugin_data_api import create_router as create_plugin_data_router
# Stage A and B share the exact manager-lifetime service and real execution owner.
def _plugin_stage_api():
    from app.plugin_stage_api import get_stage_api
    return get_stage_api(package_host.initialize())

app.include_router(create_plugin_data_router(package_host.initialize,
    stage_provider=lambda pid,stage_id:_plugin_stage_api().service.prepared(pid,stage_id),
    publication_completed=lambda pid,stage_id,receipt:_plugin_stage_api().service.publication_completed(pid,stage_id,receipt),
    principal_provider=ui_auth.sessions.principal))
from app.plugin_stage_api import create_router as create_plugin_stage_router
app.include_router(create_plugin_stage_router(package_host.initialize))
app.include_router(v2_sync.router)
app.include_router(stage5.router)
app.include_router(card_packages.router)
app.include_router(browser_migrations.router)
app.include_router(queue6.router)
app.include_router(stage7.router)
app.include_router(timeline.router)


@app.get("/v4/memory/rag/status")
def rag_status():
    return local_vectors.status()
from app import customer_management
app.include_router(customer_management.router)
app.include_router(catalog.router)
app.include_router(extensions.router)
app.include_router(workbench_git.router)
app.include_router(workbench_ssh.router)
app.include_router(workbench_review.router)
app.include_router(workspace_ui.router)
app.include_router(workspace_search.router)
app.include_router(git_changes.router)
app.include_router(git_commits.router)
app.include_router(artifacts.router)
from app import image_tools
app.include_router(image_tools.router)
app.include_router(document_reader.router)
app.include_router(workspace_activity.router)
app.include_router(terminal_ui.router)
app.include_router(persistent_terminal.router)
app.include_router(enhanced_terminal.router)
app.include_router(message_actions.router)
app.include_router(conversation_approvals.router)


app.mount(
    "/ui",
    StaticFiles(directory=STATIC_DIR / "ui", html=True),
    name="ui",
)


@app.get("/")
def index():
    return RedirectResponse(url="/ui/")


@app.post('/v11/desktop/shutdown')
async def desktop_shutdown(request: __import__('fastapi').Request):
    from app.desktop_source_bridge import require_launcher
    require_launcher(request)
    stop=getattr(app.state,'request_shutdown',None)
    if stop is None: raise HTTPException(503,'当前启动器不支持正常退出')
    queue6.shutting_down=True
    stop()
    return {'ok':True}


@app.get("/health")
def health():
    return {"ok": True, "name": "lingxi-app", "version": VERSION}


@app.get("/hello")
def hello():
    return {"msg": "你好，我是零汐"}


@app.get("/v1/models")
def list_models():
    from app.reasoning import describe
    roles, models = router.list_roles(), router.list_models()
    reasoning = {}
    for name in {*roles, *models}:
        try:
            provider, model = router.resolve(name)
            reasoning[name] = provider.get('reasoning') or describe(model, provider, provider.get('supports_effort', False))
        except ValueError:
            continue
    return {"roles": roles, "models": models, "reasoning": reasoning}


class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=MAX_USER_CHARS)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="chat", max_length=120)
    mode: Mode = "shared"
    session_id: int | None = Field(default=None, gt=0)
    request_id: UUID = Field(default_factory=uuid4)
    expected_last_id: int | None = Field(default=None, ge=0)
    # 历史从数据库读取，客户端只提交当前一句，不能注入别的会话历史。
    messages: list[UserMessage] = Field(min_length=1, max_length=1)


class PersonaRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    nickname: str = Field(default="", max_length=80)
    body: str = Field(min_length=1, max_length=5000)


class ApprovalRequest(BaseModel):
    allowed: bool = Field(strict=True)


@app.get("/v1/memory")
def get_memory(mode: Mode = "shared", session_id: int | None=None):
    current=stage5.get_memory(mode,session_id)
    scopes=[current] if mode=='shared' else [current,stage5.get_memory('shared')]
    return {'mode':mode,'facts':[f for s in scopes for f in s['facts']][:100],
            'events':[e for s in scopes for e in s['events']][:100]}


@app.get("/v1/persona")
def get_persona():
    return {**persona.load_persona(), "default_name": persona.DEFAULT_NAME,
            "default_body": persona.default_body()}


@app.post("/v1/persona/preview")
def preview_persona(req: PersonaRequest):
    return {"preview": persona.render(req.model_dump())}


@app.put("/v1/persona")
def put_persona(req: PersonaRequest):
    if not req.name.strip() or not req.body.strip():
        raise HTTPException(400, "伙伴名称和人设内容不能为空")
    # 昵称只有一个来源：人设设置，不再覆写用户的真实名字记忆。
    persona.save_persona(req.name.strip(), req.nickname.strip(), req.body)
    return {"ok": True, "note": "从下一次请求开始生效"}


@app.get("/v1/sessions")
def get_sessions(mode: Mode | None = None):
    return {"sessions": sessions.list_sessions(mode=mode)}


@app.get("/v1/sessions/{session_id}/messages")
def get_session_messages(session_id: int, before: int | None = Query(default=None, gt=0)):
    session = sessions.get_session(session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    messages = sessions.get_messages(session_id, limit=201, before=before)
    has_more = len(messages) > 200
    return {"session_id": session_id, "mode": session["mode"],
            "messages": messages[-200:], "has_more": has_more}


@app.post("/v1/approvals/{approval_id}")
async def approve(approval_id: UUID, req: ApprovalRequest):
    if not approvals.decide(str(approval_id), req.allowed):
        raise HTTPException(409, "确认已经失效或处理过")
    return {"ok": True}


def legacy_context(req):
    try:
        result = sessions.completed(str(req.request_id), chat.fingerprint(req))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is not None:
        return {'cached': result}
    if req.mode == 'shared':
        raise HTTPException(409, '旧 shared 仅可恢复成功结果；先归类，再使用/v2')
    chat.validate(req)
    sid = req.session_id
    if sid is None:
        sid = stage5.create_session(stage5.SessionInput(mode=req.mode,
                                    creation_id='legacy-' + str(req.request_id), title='旧请求恢复'))['id']
    expected = req.expected_last_id
    if expected is None:
        history = sessions.get_messages(sid, limit=1)
        expected = history[-1]['id'] if history else 0
    adapter = SimpleNamespace(session_id=sid, mode=req.mode, expected_last_id=expected,
                              character_id='lingxi' if req.mode == 'companion' else None,
                              character_revision=1 if req.mode == 'companion' else None,
                              request_id=req.request_id, legacy=True,
                              model_dump_json=req.model_dump_json)
    context = stage5.begin(adapter, chat.fingerprint(req))
    context['session_id'] = sid
    return context


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    context = legacy_context(req)
    if context.get('cached'):
        return context['cached']
    async with aclosing(chat.run(req, interactive=False, request_context=context)) as events:
        async for event in events:
            if event['type'] == 'complete':
                return {k: v for k, v in event.items() if k != 'type'}
    raise HTTPException(500, '没有收到完整回复，本轮未保存')



def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/v1/chat/stream")
async def chat_stream(req: ChatRequest):
    return stream_response(req, legacy_context(req))


class ChatRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="chat", max_length=120)
    mode: Literal["companion", "work"]
    session_id: int = Field(gt=0)
    request_id: UUID
    expected_last_id: int = Field(ge=0)
    character_id: str | None = Field(default=None, max_length=80)
    character_revision: int | None = Field(default=None, gt=0)
    messages: list[UserMessage] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def valid_character(self):
        if self.mode == "companion" and (not self.character_id or self.character_revision is None):
            raise ValueError("陪伴请求必须指定角色与版本")
        if self.mode == "work" and (self.character_id is not None or self.character_revision is not None):
            raise ValueError("工作不绑定陪伴人物卡")
        return self


@app.post("/v2/chat/stream")
async def chat_stream_v2(req: ChatRequestV2):
    digest = chat.fingerprint(req)
    try:
        cached = sessions.completed(str(req.request_id), digest)
        if cached:
            context = {"cached": cached}
        else:
            chat.validate(req)
            context = stage5.begin(req, digest)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    return stream_response(req, context)


def stream_response(req, context):
    async def generate():
        try:
            if context.get("cached"):
                yield sse({"type": "complete", **context["cached"]})
            else:
                async with aclosing(chat.run(req, request_context=context)) as events:
                    async for event in events:
                        yield sse(event)
        except Exception as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            yield sse({"type": "error", "message": message})
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/v1/usage")
def get_usage():
    return usage.summary()

from app import conversation_questions
app.include_router(conversation_questions.router)
