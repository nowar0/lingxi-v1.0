from app.tools.capabilities import spec
from app.tools.web_tools import host_only

SUGGEST={'spec':spec('suggest_work_task','给当前聊天提出工作任务建议卡片。此工具不会创建或启动任务；由用户确认拆分费用与目录',
    {key:{'type':'string'} for key in ('goal','acceptance','why')},['goal','acceptance','why']),
    'run':host_only,'write':False,'timeout':10}
PROJECT_SEARCH={'spec':spec('search_project_docs','检索当前工作会话已开启的独立项目资料库，返回向量/关键词命中和来源行号；不访问陪伴记忆',
    {'query':{'type':'string'},'top_k':{'type':'string'}},['query']),
    'run':host_only,'write':False,'timeout':30}
CONTEXT_SOURCE={'spec':spec('load_context_source','分页读取本工作节点被压缩的资料原文；不是执行授权',
    {key:{'type':'string'} for key in ('source_id','offset','limit')},['source_id']),
    'run':host_only,'write':False,'timeout':10}
