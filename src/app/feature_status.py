"""Runtime-derived feature status; source code existence is never acceptance."""
import re
from fastapi import APIRouter
from app import config
from app.db import connect

router = APIRouter(prefix='/v11/features', tags=['feature-status'])


def _configured_enabled(kind,field='enabled'):
    try:
        import json
        with connect() as db:
            rows=db.execute('SELECT document FROM capability_documents WHERE kind=?',(kind,)).fetchall()
        return any(json.loads(row['document']).get(field) is True for row in rows)
    except Exception:
        return False


def _strict_status(manager, mode):
    """Diagnostics fail closed on an unavailable or malformed runtime provider."""
    if manager.personal_use:
        return manager.strict_runtime_status(mode)
    from app.plugin_runtime_selection import StrictRuntimeSelection
    blocked = StrictRuntimeSelection().status(mode)
    try:
        value = manager.strict_runtime_status(mode)
        if (type(value) is not dict or set(value) != set(blocked)
                or type(value['available']) is not bool or value['mode'] != mode
                or value['strict_required'] is not True or value['fallback_allowed'] is not False
                or type(value['reason_code']) is not str or len(value['reason_code']) > 100
                or type(value['notice']) is not str or not 1 <= len(value['notice']) <= 1000):
            return blocked
        if value['available']:
            if (value['reason_code'] != '' or type(value['runtime_inventory_sha256']) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', value['runtime_inventory_sha256'])):
                return blocked
        elif not value['reason_code'] or value['runtime_inventory_sha256'] is not None:
            return blocked
        return dict(value)
    except Exception:
        return blocked


@router.get('')
def listing():
    from app.memory import v2
    from app import package_host, data_management
    lifecycle = data_management.lifecycle_status()
    independent = lifecycle['independent_of_program']
    active = False
    schema = False
    try:
        with connect() as db:
            schema = v2.ready(db)
            if schema:
                row = db.execute("SELECT value FROM memory_v2_meta WHERE key='authority'").fetchone()
                active = bool(row and row[0] == 'v2')
    except Exception:
        # Diagnostics must remain usable when the migration/schema is unavailable.
        pass
    manager = package_host.initialize()
    packages = manager.listing()['items']
    strict=_strict_status(manager, 'ordinary')
    mcp=_strict_status(manager, 'mcp')
    upgrades=[{'id':key,'name':name,'state':'installed_enabled' if enabled else 'installed_disabled',
        'implemented':True,'installed':True,'enabled':enabled,'note':note} for key,name,enabled,note in (
        ('chat-light-tools','聊天轻工具与指令',True,'网页搜索、URL读取、会话附件识图与本地斜杠指令；遵循模式和权限。'),
        ('work-handoff','聊天转工作与结果回传',True,'建议卡片经确认才拆分，幂等创建待启动任务；完成后空闲时回写。'),
        ('domain-tools','聊天中的应用控制',_configured_enabled('evolution-settings','domain_tools'),'当前聊天及关联工作范围；写操作逐项预览、确认并检查版本冲突。'),
        ('project-docs','独立项目资料库',_configured_enabled('project-docs'),'独立本地向量数据库、BGE默认与Qwen3可选；资料范围需由用户配置。'),
        ('speech-engines','多引擎离线语音',True,'SenseVoice随包；CosyVoice2、IndexTTS 2.5按需安装；GPT-SoVITS保留；是否就绪见运行诊断。'),
        ('plugin-config-schema','插件配置自动表单与增量更新',True,'仅数据表单；保存需预览确认。已知文件按哈希复用，不提供插件市场。'),
        ('companion-reminders','陪伴定时消息',_configured_enabled('companion-schedule'),'仅应用运行时触发；直接提醒不调用模型，生成播报需预算，退出不补跑。'),
        ('runtime-diagnostics','诊断与Markdown导出',True,'请求耗时、工具结果与预算；导出可见消息及工具摘要，不含隐藏推理。'))]
    return {'source': 'runtime', 'data_root': str(config.DATA_DIR.resolve()), 'items': [*upgrades,
        {'id': 'data-independent', 'name': '用户数据独立存储',
         'state': 'installed_enabled' if independent else 'configured_program_local',
         'implemented': True, 'installed': True, 'enabled': independent,
         'note': ('程序与用户数据分开，复制程序不迁移聊天、附件、记忆或设置；Key 留在系统凭据管理器。' if independent
                  else '当前自定义数据路径在程序目录内；不是默认独立数据布局，复制和升级前请核对导出范围。')}, 
        {'id': 'plugin-host', 'name': '本地插件包宿主', 'state': 'installed_enabled',
         'implemented': True, 'installed': True, 'enabled': True,
         'note': f'已登记 {len(packages)} 个包；个人自用，无需发布者信任，启用后可运行。'},
        {'id': 'plugin-strict-runtime', 'name': '个人插件运行时',
         'state': 'installed_enabled' if strict['available'] and mcp['available'] else 'installed_disabled',
         'implemented': True, 'installed': True, 'enabled': strict['available'] and mcp['available'],
         'note': '以当前用户权限直接运行，保留超时、停止、依赖和文件完整性检查；不需要沙箱配置。',
         'ordinary':strict,'mcp':mcp}, 
        {'id': 'memory-v2', 'name': '四层记忆 V2',
         'state': 'installed_enabled' if active else 'installed_disabled' if schema else 'unavailable',
         'implemented': schema, 'installed': schema, 'enabled': active,
         'note': 'V2 是当前读写主库。' if active else '需完成迁移异常处理并明确切换；当前聊天仍走旧记忆。'},
        {'id': 'memory-nebula', 'name': '真实记忆 3D 星云',
         'state': 'installed_enabled' if active else 'installed_disabled',
         'implemented': True, 'installed': True, 'enabled': active,
         'note': '只读真实 V2 投影，不因浏览强化；设备图形验收另行记录。'},
        {'id': 'qq', 'name': 'QQ 接入', 'state': 'deferred', 'implemented': False,
         'installed': False, 'enabled': False, 'note': '用户本轮延期，不会自动安装。'},
        {'id': 'office-ocr', 'name': 'Office / OCR', 'state': 'installed_enabled', 'implemented': True,
         'installed': True, 'enabled': True, 'note': '工作台提供 Word、Excel、PowerPoint 和 Windows OCR；按本机应用和语言包显示就绪状态。'},
        {'id': 'always-online', 'name': '常在线同步', 'state': 'planned', 'implemented': False,
         'installed': False, 'enabled': False, 'note': '后期规划，关闭应用不会继续在线。'},
        {'id': 'search-browser-enhanced', 'name': '搜索 / 浏览器增强', 'state': 'installed_enabled',
         'implemented': True, 'installed': True, 'enabled': True,
         'note': '工作台管理搜索源、筛选、缓存和调用记录；浏览器支持快照、确认动作、附件与接管。'},
        {'id': 'terminal-enhanced', 'name': '终端增强', 'state': 'installed_enabled',
         'implemented': True, 'installed': True, 'enabled': True,
         'note': 'Windows ConPTY 交互终端支持输入确认、尺寸调整和重新连接。'},
        {'id': 'workflow-ptc', 'name': 'Workflow / PTC', 'state': 'installed_enabled',
         'implemented': True, 'installed': True, 'enabled': True,
         'note': '工作台编排可保存的工具流程，支持串并行、条件和输出引用，沿用任务审批与取消。'},
    ], 'verification': {'source_tests': 'see-delivery-report', 'frozen_exe': 'see-delivery-report',
                        'formal_user_data': 'not_tested', 'external_services': 'not_tested'}}
