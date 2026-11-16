"""A bounded tool program whose effects go through the existing task gateway."""
import json
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import advanced_approval, catalog


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    path: list[str | int] = Field(default_factory=list, max_length=8)


class Condition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Source
    op: Literal['equals', 'not_equals', 'contains', 'truthy']
    value: str | int | float | bool | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool: str = Field(pattern=r'^[a-zA-Z0-9_]{1,80}$')
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=20)
    when: Condition | None = None


class TaskStep(Step):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    title: str = Field(min_length=1, max_length=150)
    depends_on: list[str] = Field(default_factory=list, max_length=20)


class Workflow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=150)
    steps: list[TaskStep] = Field(min_length=1, max_length=20)
    expected_revision: int = Field(default=0, ge=0)


def validate(step, dependencies):
    from app.tools import BY_NAME
    from app.tools.registry import worker_tool_names
    if step.tool not in worker_tool_names() or step.tool not in BY_NAME:
        raise HTTPException(422, '流程步骤只能使用任务工作成员可调用的工具')
    schema = BY_NAME[step.tool]['spec']['function']['parameters']
    if not set(schema.get('required', [])) <= step.arguments.keys() or not step.arguments.keys() <= set(schema['properties']):
        raise HTTPException(422, f'{step.tool} 参数与工具契约不一致')
    if len(json.dumps(step.arguments, ensure_ascii=False)) > 16000:
        raise HTTPException(422, '步骤参数超过 16000 字符')
    if step.when and step.when.source.node not in dependencies:
        raise HTTPException(422, '条件只能引用直接依赖的步骤')
    for arg in step.arguments.values():
        if isinstance(arg, dict) and '$from' in arg:
            if set(arg) != {'$from'} or Source.model_validate(arg['$from']).node not in dependencies:
                raise HTTPException(422, '动态参数只能引用直接依赖的步骤')


def lookup(source, dependencies):
    raw = dependencies[source.node]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = raw
    for key in source.path:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            raise ValueError(f'步骤 {source.node} 的输出缺少引用字段')
    return value


def matches(condition, dependencies):
    if condition is None:
        return True
    value = lookup(condition.source, dependencies)
    if condition.op == 'truthy':
        return bool(value)
    if condition.op == 'equals':
        return value == condition.value
    if condition.op == 'not_equals':
        return value != condition.value
    return isinstance(value, str) and isinstance(condition.value, str) and condition.value in value


async def run(jid, nid, step, dependencies):
    from app import jobs
    if not matches(step.when, dependencies):
        return json.dumps({'skipped': True, 'reason': '流程条件不满足'}, ensure_ascii=False)
    args = {key: lookup(Source.model_validate(value['$from']), dependencies)
            if isinstance(value, dict) and '$from' in value else value for key, value in step.arguments.items()}
    if len(json.dumps(args, ensure_ascii=False)) > 16000:
        raise ValueError('引用结果超过工具参数上限')
    return await jobs.execute_tool(jid, nid, {'id': 'workflow-' + nid,
        'function': {'name': step.tool, 'arguments': json.dumps(args, ensure_ascii=False)}})


def task(body):
    from app.jobs import Job, Node, validate_job
    job = Job(session_id=body.session_id, goal=body.title, acceptance='检查流程步骤的工具结果和分支记录',
        nodes=[Node(id=s.id, title=s.title, task='执行已保存的工具步骤', depends_on=s.depends_on,
                    tools=[s.tool], program=Step.model_validate(s.model_dump(exclude={'id', 'title', 'depends_on'})))
               for s in body.steps])
    validate_job(job)
    return job


@catalog.router.get('/workflows')
def list_workflows(session_id: int):
    advanced_approval.require_work(session_id)
    return {'items': [row for row in catalog.listing('workflow') if row['session_id'] == session_id]}


@catalog.router.get('/workflows/tools')
def workflow_tools(session_id: int):
    from app.tools import BY_NAME
    from app.tools.registry import worker_tool_names
    advanced_approval.require_work(session_id)
    return {'items': [{**BY_NAME[name]['spec']['function'], 'write': BY_NAME[name]['write']}
                      for name in worker_tool_names()]}


@catalog.router.post('/workflows')
def create_workflow(body: Workflow):
    advanced_approval.require_work(body.session_id)
    task(body)
    return catalog.put('workflow', uuid4().hex, body.model_dump(exclude={'expected_revision'}), 0)


@catalog.router.put('/workflows/{identifier}')
def update_workflow(identifier: str, body: Workflow):
    advanced_approval.require_work(body.session_id)
    if catalog.get('workflow', identifier)['session_id'] != body.session_id:
        raise HTTPException(404, '当前会话没有这个流程')
    task(body)
    return catalog.put('workflow', identifier, body.model_dump(exclude={'expected_revision'}), body.expected_revision)


class Instantiate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    expected_revision: int = Field(ge=1)


@catalog.router.post('/workflows/{identifier}/tasks')
def instantiate(identifier: str, body: Instantiate):
    from app import jobs
    advanced_approval.require_work(body.session_id)
    saved = catalog.get('workflow', identifier)
    if saved['session_id'] != body.session_id or saved['revision'] != body.expected_revision:
        raise HTTPException(409, '流程版本已改变或不属于当前工作会话')
    return jobs.create(task(Workflow.model_validate({k: v for k, v in saved.items() if k not in ('id', 'revision')})))
