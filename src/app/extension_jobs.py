"""Optional task presets, conservative token reservations, scheduled plans and reviews."""

import asyncio
import contextlib
import contextvars
import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException, Query
from pydantic import Field

from app import catalog, jobs, sessions, stage5
from app.db import connect
from app.extensions import TZ, Strict, busy, optional, router

current_job = contextvars.ContextVar("lingxi_extension_job", default=None)
_ticker = None
_reviewing = set()
from app.tools.registry import worker_tool_names

ALLOWED_TOOLS = worker_tool_names()


@contextlib.contextmanager
def task_scope(jid):
    token = current_job.set(jid)
    try:
        state = jobs.snapshot(jid)
        from app.session_options import generation_scope
        from app import usage
        with generation_scope(state['session_id']), usage.scope(session_id=state['session_id'],job_id=jid,node_id='',agent_id='@supervisor',kind='agent'):yield
    finally:
        current_job.reset(token)


class TaskSettings(Strict):
    daily_tokens: int = Field(default=0, ge=0, le=100000000)
    daily_media_calls: int = Field(default=0, ge=0, le=100000)
    reply_tokens: int = Field(default=4096, ge=128, le=32768)
    reviewer: str = Field(default="", max_length=300)
    auto_archive_days: int = Field(default=0, ge=0, le=3650)
    expected_revision: int = Field(default=0, ge=0)


def settings():
    return {
        **TaskSettings().model_dump(exclude={"expected_revision"}),
        **optional("extension-config", "tasks"),
    }


@router.get("/tasks/settings")
def get_settings():
    return settings()


@router.put("/tasks/settings")
def save_settings(body: TaskSettings):
    from app.llm import router as models

    if (
        body.reviewer
        and body.reviewer not in models.list_roles()
        and body.reviewer not in models.list_models()
    ):
        raise HTTPException(422, "请选择已配置的文字验收模型")
    return catalog.put(
        "extension-config",
        "tasks",
        body.model_dump(exclude={"expected_revision"}),
        body.expected_revision,
    )


def reserve(messages, tools):
    jid = current_job.get()
    if not jid:
        return None
    cfg = settings()
    cap = cfg["daily_tokens"]
    if not cap:
        return None
    # UTF-8 byte count plus framing is deliberately conservative; never count unknown usage as 0.
    maximum = (
        len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8"))
        + 1024
        + cfg["reply_tokens"]
    )
    key, day = uuid4().hex, datetime.now(TZ).date().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        spent = db.execute(
            "SELECT COALESCE(SUM(charged),0) FROM extension_budget WHERE day=?", (day,)
        ).fetchone()[0]
        if spent + maximum > cap:
            raise HTTPException(429, "任务日预算不足以保守预留本次输入和输出；已停止新的模型调用")
        db.execute(
            "INSERT INTO extension_budget VALUES(?,?,?,?,?,0)", (key, day, jid, maximum, maximum)
        )
    return {"id": key, "output": cfg["reply_tokens"]}


def reserve_media(module_id, model):
    jid = current_job.get()
    if not jid:
        return None
    cfg = settings()
    maximum = cfg['daily_media_calls']
    if cfg['daily_tokens'] and not maximum:
        raise HTTPException(409, '文字 Token 预算不能覆盖媒体费用；请先设置每日媒体调用次数上限')
    day, ident = datetime.now(TZ).date().isoformat(), uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        used = db.execute('SELECT COUNT(*) FROM extension_media_budget WHERE day=?', (day,)).fetchone()[0]
        if maximum and used >= maximum:
            raise HTTPException(429, '任务每日媒体调用次数已达上限，未提交新请求')
        db.execute('INSERT INTO extension_media_budget VALUES(?,?,?,?,?,?)', (ident, day, jid, module_id, model, 'submitted'))
    return ident


def finish_media_reservation(ident, status):
    if ident:
        with connect() as db:
            db.execute('UPDATE extension_media_budget SET status=? WHERE id=?', (status, ident))


def finish_reservation(reservation, actual):
    if not reservation:
        return
    from app import usage

    numbers = usage.extract(actual)
    known = numbers["输入"] is not None and numbers["输出"] is not None
    with connect() as db:
        if known:
            db.execute(
                "UPDATE extension_budget SET charged=?,known=1 WHERE id=?",
                (numbers["输入"] + numbers["输出"], reservation["id"]),
            )
        # Interrupted/unreported calls retain the entire reservation, including after restart.


@router.get("/tasks/budget")
def budget():
    day = datetime.now(TZ).date().isoformat()
    with connect() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(charged),0) AS charged,COUNT(*) AS calls,COALESCE(SUM(CASE WHEN known=0 THEN 1 ELSE 0 END),0) AS unknown FROM extension_budget WHERE day=?",
            (day,),
        ).fetchone()
        media = db.execute('SELECT COUNT(*) AS calls,SUM(CASE WHEN status IN ("unknown","submitted") THEN 1 ELSE 0 END) AS unknown FROM extension_media_budget WHERE day=?', (day,)).fetchone()
    return {
        "day": day,
        "media_calls": media['calls'], "media_unknown": media['unknown'] or 0,
        "media_limit": settings()['daily_media_calls'],
        **dict(row),
        "limit": settings()["daily_tokens"],
        "scope": "启用预算后经任务执行器发出的模型调用；不含日常聊天；未知用量按完整预留计入",
    }


class AgentPreset(Strict):
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(default="", max_length=300)
    effort: str = Field(default="auto", pattern=r"^(auto|none|enabled|minimal|low|medium|high|xhigh|max)$")
    temperature: float|None=Field(default=None,ge=0,le=2)
    skill_ids: list[str] = Field(default_factory=list, max_length=20)
    tools: list[str] = Field(default_factory=lambda: list(worker_tool_names()), max_length=20)
    brief: str = Field(default="", max_length=4000)
    expected_revision: int = Field(default=0, ge=0)


def validate_preset(body):
    from app.llm import router as models
    from app import reasoning
    selected=body.model or ('work' if 'work' in models.list_roles() else 'chat')
    try:
        provider,model=models.resolve(selected)
        capability=provider.get('reasoning') or reasoning.describe(model,provider,provider.get('supports_effort',False))
        if body.effort!='auto' and reasoning.coerce(body.effort,capability)!=body.effort:raise HTTPException(422,'当前模型不支持此思考强度')
        supported=capability.get('family') not in ('gemini','kimi') and (not (capability.get('family')=='openai' and reasoning.openai_model(model)) or body.effort=='none')
        if body.temperature is not None and not supported:raise HTTPException(422,'当前模型不支持温度覆盖，请清空温度')
    except ValueError:raise HTTPException(422,'预设模型不可用') from None

    if (
        body.model
        and body.model not in models.list_roles()
        and body.model not in models.list_models()
    ):
        raise HTTPException(422, "预设模型不存在或未启用")
    size = 0
    for key in body.skill_ids:
        size += len(catalog.assigned_skill(key)["body"])
    if size > 24000:
        raise HTTPException(422, "预设技能正文合计超过 24000 字，请减少分配的技能")
    if not set(body.tools) <= set(worker_tool_names()):
        raise HTTPException(422, "预设包含不支持的工具")


def preset_parameters():
    from app.llm import router as models
    from app import reasoning
    result={}
    for name in list(models.list_roles())+models.list_models():
        try:
            provider,model=models.resolve(name);cap=provider.get('reasoning') or reasoning.describe(model,provider,provider.get('supports_effort',False))
            result[name]={'levels':['auto',*cap.get('levels',[])],'temperature':cap.get('family') not in ('gemini','kimi') and not (cap.get('family')=='openai' and reasoning.openai_model(model))}
        except ValueError:continue
    return result

@router.get("/presets")
def presets():
    from app.llm import router as models

    return {
        "items": catalog.listing("agent-preset"),
        "models": {**models.list_roles(),**{name:name for name in models.list_models()}},
        "parameters": preset_parameters(),
        "skills": catalog.skill_metadata(),
        "tools": worker_tool_names(),
    }


@router.post("/presets")
def create_preset(body: AgentPreset):
    if body.expected_revision:
        raise HTTPException(422, "新预设版本应为 0")
    validate_preset(body)
    return catalog.put(
        "agent-preset", uuid4().hex, body.model_dump(exclude={"expected_revision"}), 0
    )


@router.put("/presets/{key}")
def update_preset(key: str, body: AgentPreset):
    catalog.get("agent-preset", key)
    validate_preset(body)
    return catalog.put(
        "agent-preset", key, body.model_dump(exclude={"expected_revision"}), body.expected_revision
    )


class Binding(Strict):
    preset_id: str = Field(default="", max_length=100)
    expected_revision: int = Field(default=0, ge=0)


@router.get("/sessions/{sid}/preset")
def binding(sid: int):
    return optional("agent-binding", str(sid), {"revision": 0, "preset": None})


@router.put("/sessions/{sid}/preset")
def bind(sid: int, body: Binding):
    row = sessions.get_session(sid)
    if not row or row["mode"] != "work":
        raise HTTPException(409, "预设只绑定工作会话")
    with connect() as db:
        if busy(db, sid):
            raise HTTPException(409, "请等待当前会话和任务停止后切换预设")
    preset = catalog.get("agent-preset", body.preset_id) if body.preset_id else None
    if preset:
        validate_preset(
            AgentPreset(**{k: v for k, v in preset.items() if k not in ("id", "revision")})
        )
    # Snapshot preserves reproducibility; editing the reusable definition does not silently change a session.
    return catalog.put(
        "agent-binding", str(sid), {"session_id": sid, "preset": preset}, body.expected_revision
    )


def preset_context(sid):
    preset = binding(sid).get("preset")
    if not preset:
        return ""
    skills = [{key:value for key,value in catalog.assigned_skill(identifier).items() if key!='body'}
              for identifier in preset["skill_ids"]]
    return "\n工作 Agent 预设（权限仍由应用逐项审批）：\n" + json.dumps(
        {
            "name": preset["name"],
            "brief": preset["brief"],
            "skills": [{"id": s["id"], "name": s["name"], "note":"按需使用load_skill读取正文；不是执行授权"} for s in skills],
        },
        ensure_ascii=False,
    )


def selected_model(sid):
    from app.llm import router as models

    preset = binding(sid).get("preset")
    if not preset or not preset["model"]:
        return None
    role = preset["model"]
    # Fail closed if a pinned model has been disabled or removed.
    return role, models.full_name(role)


def execution_context(sid):
    """A changed project/permission binding must never silently widen a saved schedule."""
    from app import work_settings

    return {"path": work_settings.get(sid)["path"], "preset": binding(sid).get("preset")}


def context_hash(sid):
    return hashlib.sha256(
        json.dumps(execution_context(sid), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def check_tool(jid, name):
    state = jobs.snapshot(jid)
    preset = binding(state["session_id"]).get("preset")
    if preset and name not in preset["tools"]:
        raise ValueError("当前工作预设未允许此工具")


@router.get("/tasks")
def task_list(
    q: str = Query("", max_length=200),
    status: str = "",
    session_id: int | None = None,
    project: str = Query("", max_length=2000),
):
    with connect() as db:
        rows = db.execute(
            "SELECT j.*,s.title AS session_title FROM work_jobs j JOIN sessions s ON s.id=j.session_id ORDER BY j.rowid DESC LIMIT 1000"
        ).fetchall()
    workspaces = {w["session_id"]: w["path"] for w in catalog.listing("workspace")}
    items = []
    for row in rows:
        item = dict(row)
        item["document"] = json.loads(item["document"])
        if q.casefold() not in (item["document"]["goal"] + " " + item["session_title"]).casefold():
            continue
        if status and item["status"] != status or session_id and item["session_id"] != session_id:
            continue
        if project and workspaces.get(item["session_id"]) != project:
            continue
        item["project_path"] = workspaces.get(item["session_id"], "")
        items.append(item)
    selected = items[:200]
    if selected:
        grouped = {item['id']: [] for item in selected}
        with connect() as db:
            placeholders = ','.join('?' for _ in grouped)
            nodes = db.execute(f'SELECT job_id,id,status,document FROM work_nodes WHERE job_id IN ({placeholders})', list(grouped)).fetchall()
        for node in nodes:
            document = json.loads(node['document'])
            grouped[node['job_id']].append({'id': node['id'], 'status': node['status'],
                                           'document': {'title': document.get('title', node['id'])}})
        for item in selected:
            item['nodes'] = grouped[item['id']]
    return {
        "items": selected,
        "limited": len(rows) == 1000 or len(items) > 200,
        "projects": sorted(set(workspaces.values())),
    }


def evidence(state):
    return {
        "goal": state["document"]["goal"],
        "acceptance": state["document"]["acceptance"],
        "nodes": [
            {"id": n["id"], "status": n["status"], "result": n["result"]} for n in state["nodes"]
        ],
        "tools": [e for e in state["events"] if e["event"] in ("tool_result", "tool_interrupted")],
    }


def evidence_hash(state):
    return hashlib.sha256(
        json.dumps(evidence(state), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@router.post("/tasks/{jid}/review")
async def independent_review(jid: str, body: dict):
    from app.workbench_review import run
    return await run(jid, body)


def _current_review(jid, review):
    from app.workbench_review import current_report
    return current_report(jid, review)


@router.get("/tasks/{jid}/review")
def review_state(jid: str):
    state = jobs.snapshot(jid)
    review = optional("independent-review", jid)
    return {
        **review,
        "required": bool(settings()["reviewer"]),
        "current": _current_review(jid, review),
    }


def check_accept(jid):
    if settings()["reviewer"]:
        review = review_state(jid)
        if (
            not review["current"]
            or not review.get("report", {}).get("passed")
            or review.get("reviewer") != settings()["reviewer"]
        ):
            raise HTTPException(409, "独立验收尚未通过或证据已变化，请先重新验收")


def cron_fields(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("需要五段 Cron：分 时 日 月 周")
    result = []
    for part, minimum, maximum in zip(parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        values = set()
        for segment in part.split(","):
            pieces = segment.split("/")
            if len(pieces) > 2:
                raise ValueError("Cron 步长无效")
            step = int(pieces[1]) if len(pieces) == 2 else 1
            if step < 1 or step > maximum + 1:
                raise ValueError("Cron 步长越界")
            bounds = pieces[0]
            if bounds == "*":
                lo, hi = minimum, maximum
            elif "-" in bounds:
                pair = bounds.split("-")
                if len(pair) != 2:
                    raise ValueError("Cron 范围无效")
                lo, hi = map(int, pair)
            else:
                lo = hi = int(bounds)
            if not minimum <= lo <= hi <= maximum:
                raise ValueError("Cron 数值越界")
            values.update(range(lo, hi + 1, step))
        result.append(values)
    result[4] = {x % 7 for x in result[4]}
    return parts, result


def next_due(expression, after=None):
    parts, fields = cron_fields(expression)
    start = (after or datetime.now(TZ)).astimezone(TZ).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # A leap day across a non-leap century can be eight years away. Iterate days,
    # not millions of minutes, and reject impossible calendars with a finite bound.
    times = [(hour, minute) for hour in sorted(fields[1]) for minute in sorted(fields[0])]
    day = start.replace(hour=0, minute=0)
    for _ in range(366 * 8 + 1):
        dom = day.day in fields[2]
        dow = (day.weekday() + 1) % 7 in fields[4]
        day_match = (dom or dow) if not parts[2].startswith('*') and not parts[4].startswith('*') else dom and dow
        if day.month in fields[3] and day_match:
            for hour, minute in times:
                candidate = day.replace(hour=hour, minute=minute)
                if candidate >= start:
                    return candidate.isoformat()
        day += timedelta(days=1)
    raise ValueError('未来八年没有触发时间，请检查月份与日期组合')


class Schedule(Strict):
    job_id: str = Field(min_length=1, max_length=100)
    cron: str = Field(default="0 9 * * *", max_length=100)
    enabled: bool = False
    confirm_start: bool = False
    expected_revision: int = Field(default=0, ge=0)


@router.get("/schedules")
def schedules():
    with connect() as db:
        history = [
            dict(r)
            for r in db.execute("SELECT * FROM extension_schedule_runs ORDER BY due DESC LIMIT 50")
        ]
    return {
        "items": catalog.listing("task-schedule"),
        "history": history,
        "timezone": "Asia/Shanghai",
        "note": "仅程序运行时触发；错过不补跑；写入、命令和 MCP 仍逐项确认；同一计划未结束时跳过。",
    }


@router.put("/schedules/{key}")
def save_schedule(key: str, body: Schedule):
    template = jobs.snapshot(body.job_id)
    if body.enabled and (not body.confirm_start or not settings()["daily_tokens"]):
        raise HTTPException(422, "启用前请设置任务日预算并勾选允许自动启动这份计划")
    try:
        due = next_due(body.cron)
    except ValueError, OverflowError:
        raise HTTPException(422, "Cron 无效，请填写五段规则：分 时 日 月 周") from None
    # Snapshot approved plan and inputs; future edits do not change scheduled execution silently.
    plan = {k: template["document"][k] for k in ("goal", "materials", "acceptance")}
    plan["session_id"] = template["session_id"]
    plan["nodes"] = [n["document"] for n in template["nodes"]]
    previous = optional("task-schedule", key)
    last_job = previous.get("last_job") if previous.get("job_id") == body.job_id else None
    return catalog.put(
        "task-schedule",
        key,
        {
            "job_id": body.job_id,
            "cron": body.cron,
            "enabled": body.enabled,
            "next_due": due,
            "plan": plan,
            "context_hash": context_hash(template["session_id"]),
            "last_job": last_job,
        },
        body.expected_revision,
    )


async def tick(now=None):
    current = now or datetime.now(TZ)
    for item in catalog.listing("task-schedule"):
        try:
            await tick_schedule(item, current)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('单个定时计划维护失败，其他计划继续')
    archive_days = settings()["auto_archive_days"]
    if archive_days:
        cutoff = (current - timedelta(days=archive_days)).isoformat()
        state = stage5.ui_state()
        with connect() as db:
            for row in db.execute(
                "SELECT id FROM sessions WHERE mode='work' AND archived=0 AND updated_at<?",
                (cutoff,),
            ).fetchall():
                if row["id"] != state.get("active_work_session_id") and not busy(db, row["id"]):
                    db.execute(
                        "UPDATE sessions SET archived=1,updated_at=? WHERE id=?",
                        (stage5.now(), row["id"]),
                    )



def advance_schedule(item, current, jid=None):
    updated = {k: v for k, v in item.items() if k not in ("id", "revision")}
    updated["next_due"] = next_due(item["cron"], current)
    updated["last_job"] = jid or item.get("last_job")
    try:
        catalog.put("task-schedule", item["id"], updated, item["revision"])
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        # A concurrent explicit user edit wins.


async def tick_schedule(item, current):
    if not item["enabled"] or datetime.fromisoformat(item["next_due"]) > current:
        return
    due = item["next_due"]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        live=db.execute("SELECT revision,document FROM capability_documents WHERE kind='task-schedule' AND id=?",(item['id'],)).fetchone()
        if not live or live['revision']!=item['revision'] or json.loads(live['document']).get('deleted'):return
        existing = db.execute("SELECT * FROM extension_schedule_runs WHERE schedule_id=? AND due=?",
                              (item["id"], due)).fetchone()
        if not existing:
            db.execute("INSERT INTO extension_schedule_runs VALUES(?,?,'launching',NULL,'')", (item["id"], due))
    if existing:
        advance_schedule(item, current, existing['job_id'])
        return
    status, detail, jid = "skipped", "错过触发时间，不补跑", None
    try:
        if (current - datetime.fromisoformat(due)).total_seconds() < 60:
            previous = jobs.snapshot(item["last_job"]) if item.get("last_job") else None
            session = sessions.get_session(item["plan"]["session_id"])
            if not settings()["daily_tokens"]:
                detail = "任务预算已关闭，未自动启动"
            elif not session or session["archived"]:
                detail = "工作会话不存在或已归档"
            elif item.get("context_hash") != context_hash(session["id"]):
                detail = "项目目录或工作预设已变化，请重新核对并保存定时计划"
            elif previous and previous["status"] not in ("complete", "cancelled"):
                detail = "上一轮尚未结束"
            else:
                with connect() as db:
                    active = busy(db, session["id"])
                if active:
                    detail = "工作会话正忙"
                else:
                    created = jobs.create(jobs.Job(**item["plan"]))
                    jid = created["id"]
                    with connect() as db:
                        db.execute("UPDATE extension_schedule_runs SET job_id=? WHERE schedule_id=? AND due=?", (jid, item["id"], due))
                    await jobs.action(jid, jobs.Action(action="start", expected_revision=created["revision"]))
                    status, detail = "started", "按已保存计划启动，外部写操作仍需人工确认"
    except Exception as exc:
        status, detail = "failed", type(exc).__name__ + "；未自动重试，请查看任务列表"
    finally:
        # Persist the occurrence before computing a future date or touching a versioned plan.
        with connect() as db:
            db.execute("UPDATE extension_schedule_runs SET status=?,job_id=?,detail=? WHERE schedule_id=? AND due=?",
                       (status, jid, detail, item["id"], due))
        advance_schedule(item, current, jid)


async def start():
    global _ticker

    async def loop():
        while True:
            await asyncio.sleep(20)
            try:
                await tick()
                from app import handoff,reminders,project_docs
                for callback in (handoff.deliver_returns,reminders.tick,project_docs.tick):
                    try: await callback()
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception('扩展后台维护失败，其他模块继续运行')
                from app.extension_tools import reap

                await reap()
                from app.workbench_ssh import reap as reap_workbench
                await reap_workbench()
            except Exception:
                # Never crash the app or start work after uncertain scheduler failure.
                import logging

                logging.getLogger(__name__).exception("扩展任务维护失败，未自动重放")

    _ticker = asyncio.create_task(loop())


async def shutdown():
    global _ticker
    if _ticker:
        _ticker.cancel()
        await asyncio.gather(_ticker, return_exceptions=True)
        _ticker = None
