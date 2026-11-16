"""Session-local execution decisions, evaluated by the host at each tool call."""
from app import catalog

def state(ctx):
    if not ctx.get('session_id'): return {'permission':'ask','plan_mode':False}
    from app.composer_controls import get_state
    return get_state(ctx['session_id'])

def check(name, tool, args, ctx):
    value=state(ctx)
    if tool.get('write',True) and (ctx.get('read_only') or value['plan_mode'] or value['permission']=='readonly'):
        raise ValueError('计划或仅可查看权限禁止修改、命令与其他写入操作')
    if name.startswith('workspace_'):
        selected=catalog.get('workspace',args['workspace_id'])
        if selected.get('deleted') or selected['session_id']!=ctx.get('session_id'):
            raise ValueError('工作区不属于当前会话')
        if ctx.get('workspace_revision')!=selected['revision']:
            raise ValueError('工作区绑定已变化，请重新读取')
        if name in ('workspace_write','workspace_edit'):
            from app.workspace import resolve
            resolve(args['workspace_id'],ctx['session_id'],args['path'])
    return value

def needs_approval(name, tool, args, ctx):
    value=check(name,tool,args,ctx)
    if not tool.get('write',True):return False
    if name in ('workspace_write','workspace_edit') and value['permission'] in ('workspace','full'):return False
    # Full permission authorizes host-mediated command/MCP/browser/document tools.
    # Application-management and package grants keep their distinct approval contracts.
    if value['permission']=='full' and tool.get('group') not in ('task','ui'):
        return False
    return True
