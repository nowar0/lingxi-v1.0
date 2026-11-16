"""Single source of exposure, role boundaries and approval metadata.

Context is supplied by the host; schemas never accept mode/session/agent_role.
Legacy dictionaries remain live for existing executor integrations.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Capability:
    name: str
    kind: str
    spec: dict
    run: Callable | None
    group: str
    write: bool
    cost: str
    modes: tuple[str, ...]
    roles: tuple[str, ...]
    timeout: float
    audit: bool = True
    source: str = 'core'
    schemas: tuple[int, ...] = (3,)
    external_material: bool = True
    order: int = 0


class CapabilityRegistry:
    def __init__(self):
        self.entries = {}
        self.by_name = {}
        self.tools = []

    def register(self, tool, *, group, modes=('companion', 'work'), roles=('chat',),
                 schemas=(3,), external_material=True, cost='none', source='core',
                 kind='tool', order=None):
        name = tool['spec']['function']['name'] if kind == 'tool' else tool['spec']['name']
        if name in self.entries:
            raise ValueError(f'重复能力：{name}')
        if kind not in ('tool', 'command') or cost not in ('none', 'token', 'media', 'money'):
            raise ValueError('无效能力类型或成本')
        if not modes or not set(modes) <= {'companion', 'work'}:
            raise ValueError('无效能力模式')
        if not roles or not set(roles) <= {'chat', 'worker', 'orchestrator'}:
            raise ValueError('无效能力角色')
        if 'worker' in roles and group in {'memory', 'task', 'ui', 'desktop'}:
            raise ValueError('工作成员不能访问记忆或应用控制能力')
        if kind == 'tool':
            params = tool['spec']['function']['parameters']
            if {'mode', 'session_id', 'agent_role'} & set(params.get('properties', {})):
                raise ValueError('作用域只能由宿主注入')
        item = Capability(name=name, kind=kind, spec=deepcopy(tool['spec']), run=tool.get('run'),
            group=group, write=bool(tool['write']), cost=cost, modes=tuple(modes), roles=tuple(roles),
            timeout=tool['timeout'], source=source, schemas=tuple(schemas),
            external_material=external_material, order=len(self.entries) if order is None else order)
        self.entries[name] = item
        if kind == 'tool':
            descriptor = {**tool, 'spec': item.spec, 'write': item.write, 'timeout': item.timeout,
                          'group': group, 'roles': item.roles, 'source': source}
            self.by_name[name] = descriptor
            self.tools.append(descriptor)
        return item

    def visible(self, ctx, *, kind='tool'):
        mode = ctx.get('mode', 'companion')
        if mode == 'shared':
            # /chat historically defaults to shared; current queue modes stay
            # immutable companion/work. Tool implementations retain host ctx.
            mode = 'companion'
        role = ctx.get('agent_role', 'chat')
        schema = 3 if ctx.get('schema_version') == 3 else 2
        return [item for item in sorted(self.entries.values(), key=lambda x: x.order)
                if item.kind == kind and mode in item.modes and role in item.roles
                and schema in item.schemas
                and (not ctx.get('read_only') or not item.write)
                and (ctx.get('tool_groups') is None or item.group in ctx['tool_groups'])
                and (item.group not in ('task','ui','desktop') or ctx.get('domain_tools') is True)
                and (item.group!='ui' or bool(ctx.get('ui_owner')))
                and (not ctx.get('scheduled') or (not item.write and item.group in ('web','skill','system')))
                and (item.name!='request_user_input' or (ctx.get('session_id') and not ctx.get('scheduled')))
                and (not ctx.get('external_material') or item.external_material)]

    def names(self, *, role, groups=None, mode='work'):
        return tuple(item.name for item in self.visible(
            {'mode': mode, 'agent_role': role, 'schema_version': 3})
            if groups is None or item.group in groups)


registry = CapabilityRegistry()


def _register_core():
    from app.tools.basic import TOOL as clock
    from app.tools.memory_tool import TOOL as memory
    from app.tools.memory7_tool import PROPOSE, SEARCH
    from app.tools.capabilities import CATALOG, SKILL, DISCOVER, CALL, PACKAGE_TOOLS, PACKAGE_REQUEST
    from app.tools.workspace_tool import TOOLS as workspace
    from app.tools.browser_tool import TOOLS as browser
    registry.register(clock, group='system', schemas=(2, 3), order=0)
    from app.tools.question_tool import TOOL as QUESTION
    registry.register(QUESTION,group='system',roles=('chat',),order=28)
    registry.register(memory, group='memory', schemas=(2,), order=1)
    registry.register(PROPOSE, group='memory', external_material=False, order=2)
    registry.register(SEARCH, group='memory', order=3)
    registry.register(CATALOG, group='skill', roles=('chat', 'worker', 'orchestrator'), order=4)
    registry.register(SKILL, group='skill', roles=('chat','worker'), order=5)
    for index, tool in enumerate((DISCOVER, CALL, PACKAGE_TOOLS, PACKAGE_REQUEST), 6):
        registry.register(tool, group='plugin', roles=('chat', 'worker'),
                          cost='money' if tool is CALL else 'none', order=index)
    for index, tool in enumerate(workspace, 11):
        registry.register(tool, group='workspace', modes=('work',), roles=('chat', 'worker'), order=index)
    for tool in browser:
        registry.register(tool, group='browser', roles=('chat', 'worker'), order=10)
    from app.tools.web_tools import FETCH, SEARCH as WEB_SEARCH, DESCRIBE
    from app.tools.evolution_tools import SUGGEST, PROJECT_SEARCH, CONTEXT_SOURCE
    from app.tools.advanced_workbench_tools import BROWSER, OFFICE
    from app.domain_tools import TOOLS as DOMAIN
    registry.register(WEB_SEARCH,group='web',order=20)
    registry.register(FETCH,group='web',order=21)
    registry.register(DESCRIBE,group='media',cost='media',order=22)
    registry.register(SUGGEST,group='handoff',order=23)
    registry.register(PROJECT_SEARCH,group='project',modes=('work',),roles=('chat','worker'),order=24)
    registry.register(CONTEXT_SOURCE,group='context',modes=('work',),roles=('worker',),order=25)
    registry.register(BROWSER, group='browser', modes=('work',), roles=('chat', 'worker'), order=26)
    registry.register(OFFICE, group='documents', modes=('work',), roles=('chat', 'worker'), order=27)
    for index,tool in enumerate(DOMAIN,30):
        group='ui' if tool['spec']['function']['name']=='open_ui' else 'task'
        registry.register(tool,group=group,roles=('chat',),order=index)
    # User input commands are parsed by the backend; never supplied as model tools.
    for name, argument, description in (
        ('/work', '<目标>', '转交工作任务'), ('/skill', '<名称>', '加载已启用技能'),
        ('/model', '<名称>', '选择当前会话模型'), ('/reset', '', '新建当前模式会话'),
        ('/stop', '', '停止当前生成'), ('/help', '', '查看指令说明'),
        ('/goal', '[目标|clear]', '查看或设置长期目标'), ('/plan', '[on|off]', '切换只读计划模式'),
        ('/compact', '', '生成摘要并压缩上下文，原历史保留'),
        ('/permission', '<workspace|readonly>', '选择工作区内修改或仅可查看权限'),
        ('/feedback', '<内容>', '保存本地会话反馈'), ('/export', '', '导出当前会话ZIP')):
        registry.register({'spec': {'name': name, 'argument': argument, 'description': description,
                                   'enabled': True}, 'write': name not in ('/help', '/skill'),
                           'timeout': 10}, group='command', kind='command')


_register_core()


def worker_tool_names():
    order = {'workspace': 0, 'browser': 1, 'plugin': 2, 'skill':3, 'project':4, 'context':5, 'documents':6}
    items = registry.visible({'mode': 'work', 'agent_role': 'worker', 'schema_version': 3})
    return tuple(item.name for item in sorted(
        (item for item in items if item.group in order), key=lambda item: (order[item.group], item.order)))
