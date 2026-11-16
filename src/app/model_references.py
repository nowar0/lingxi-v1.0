"""Find configured consumers without inspecting prompt text or running any model."""
import json
from fastapi import HTTPException
from app import catalog


def references(routes, profile_ids=()):
    routes, profile_ids = set(routes), set(profile_ids)
    def used(value, key=''):
        if isinstance(value, dict):
            return any(used(v, k) for k, v in value.items())
        if isinstance(value, list):
            return any(used(v, key) for v in value)
        return isinstance(value, str) and (key in ('model', 'summary_model', 'reviewer', 'fallbacks') and value in routes
                                          or key == 'profile_id' and value in profile_ids)
    found = []
    with catalog.read_connection() as db:
        for row in db.execute("SELECT kind,id,document FROM capability_documents WHERE kind NOT IN ('model-profile','module','local-model-service','local-model-download')"):
            doc = json.loads(row['document'])
            if not doc.get('deleted') and used(doc):
                found.append({'kind': row['kind'], 'id': row['id']})
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'work_jobs' in tables:
            for row in db.execute("SELECT id,document FROM work_jobs"):
                if used(json.loads(row['document'])):
                    found.append({'kind': 'task', 'id': row['id']})
    return found


def assert_unused(routes, profile_ids=()):
    found = references(routes, profile_ids)
    if found:
        names = {'session-options':'会话','composer-controls':'摘要设置','agent-binding':'会话预设','agent-preset':'预设','provider-failover':'备用模型','task':'任务'}
        labels = '、'.join(names.get(r['kind'], '配置') + ' ' + str(r['id'])[:60] for r in found[:3])
        raise HTTPException(409, '仍被' + labels + '引用，请先更换引用的模型')


def assert_idle():
    from app import chat, jobs
    if chat.active or jobs.running or jobs.member_chats:
        raise HTTPException(409, '有对话或任务正在运行，请先停止后再修改模型连接')
