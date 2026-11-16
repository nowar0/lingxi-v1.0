"""Model-facing contracts for the same workbench operations."""
from app.tools.capabilities import spec


BROWSER = {'spec': spec('work_browser',
    '操作当前工作会话的专用 Edge。snapshot 返回元素编号、页面和指纹；后续动作提交同一指纹和目标。接管、脚本和附件传输由用户界面处理。',
    {'action': {'type': 'string', 'enum': ['snapshot', 'open', 'new_tab', 'switch_tab', 'close_tab',
          'click', 'fill', 'select', 'press', 'scroll', 'wait']},
     'url': {'type': 'string'}, 'snapshot': {'type': 'string'}, 'tab_id': {'type': 'string'},
     'ref': {'type': 'integer'}, 'value': {'type': 'string'}, 'delta': {'type': 'integer'}}, ['action']),
    'write': True, 'timeout': 38, 'run': None}

OFFICE = {'spec': spec('office_artifact',
    '在当前工作会话生成 Office 产物或处理会话附件。编辑生成新产物，render 输出 PDF，recognize 离线识别图片或扫描 PDF。',
    {'kind': {'type': 'string', 'enum': ['word', 'excel', 'powerpoint', 'ocr']},
     'action': {'type': 'string', 'enum': ['create', 'edit', 'render', 'recognize']},
     'attachment_id': {'type': 'string'},
     'fields': {'type': 'object', 'description': 'Word: title, paragraphs, find, replace；Excel: sheet, cells[{address,value,formula}]；PowerPoint: slides[{title,body}] 或 slide, title'},
     'language': {'type': 'string'}, 'pages': {'type': 'array', 'items': {'type': 'integer'}}}, ['kind', 'action']),
    'write': True, 'timeout': 155, 'run': None}
