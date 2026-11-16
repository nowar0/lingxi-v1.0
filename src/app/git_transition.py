"""Fence project conversations while an explicitly requested branch switch runs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from fastapi import HTTPException
from app import catalog
from app.db import connect


def init_table():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS git_transitions(id TEXT PRIMARY KEY,root TEXT NOT NULL,session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,status TEXT NOT NULL,at REAL NOT NULL,detail TEXT NOT NULL)')
        db.execute("UPDATE git_transitions SET status='interrupted',detail='服务重启中断；请核对当前分支，不自动重放' WHERE status='running'")


def under(path,root):
    try:return os.path.normcase(str(Path(path).resolve()))==os.path.normcase(str(root)) or Path(path).resolve().is_relative_to(root)
    except (OSError,ValueError):return False


def _has_table(db,name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def paths_for(db,sid):
    if not _has_table(db,'capability_documents'):return []
    rows=db.execute("SELECT document FROM capability_documents WHERE (kind='work-settings' AND id=?) OR (kind='workspace' AND json_extract(document,'$.session_id')=?)",(str(sid),sid)).fetchall()
    return [value['path'] for row in rows if not (value:=json.loads(row['document'])).get('deleted') and value.get('path')]


def related_sessions(db,root,sid):
    result={sid}
    if not _has_table(db,'capability_documents'):return result
    for row in db.execute("SELECT kind,id,document FROM capability_documents WHERE kind IN ('workspace','work-settings') AND json_valid(document)"):
        value=json.loads(row['document'])
        if value.get('deleted') or not value.get('path') or not under(value['path'],root):continue
        owner=value.get('session_id') if row['kind']=='workspace' else int(row['id']) if row['id'].isdigit() else None
        if owner and db.execute('SELECT 1 FROM sessions WHERE id=? AND archived=0',(owner,)).fetchone():result.add(owner)
    return result


def is_running(sid,db):
    if not _has_table(db,'git_transitions'):return False
    paths=paths_for(db,sid)
    return any(row['session_id']==sid or any(under(path,Path(row['root'])) for path in paths)
               for row in db.execute("SELECT root,session_id FROM git_transitions WHERE status='running'"))


def assert_unclaimed(db,sid,target=None):
    if is_running(sid,db) or target and any(under(target,Path(row['root'])) for row in db.execute("SELECT root FROM git_transitions WHERE status='running'")):
        raise HTTPException(409,'此项目正在切换Git分支，请等待操作结束再发送、启动任务或切换项目目录')


def boundary_reason(db,root,sid):
    if not _has_table(db,'capability_documents'):return ''
    from app import chat,extensions,jobs
    for owner in related_sessions(db,root,sid):
        if extensions.busy(db,owner):return '此项目仍有对话、排队输入、审批、工具或任务在处理，请等回复与操作结束后切换'
        if any(db.execute('SELECT 1 FROM work_jobs WHERE id=? AND session_id=?',(jid,owner)).fetchone() for jid in list(jobs.running)):
            return '此项目的任务仍在停止或清理工具进程，请等操作结束后切换'
        if db.execute("SELECT 1 FROM capability_documents WHERE kind='ui-command' AND json_extract(document,'$.session_id')=? AND json_extract(document,'$.status')='running'",(owner,)).fetchone():
            return '此项目的旧终端命令结果尚未确认，请先核对执行记录'
        if _has_table(db,'stage5_attempts') and db.execute("SELECT 1 FROM stage5_attempts WHERE session_id=? AND status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)",(owner,)).fetchone():
            return '此项目有结果未确认的旧对话请求，请先恢复并核对'
        latest=db.execute("SELECT delivery_status FROM messages WHERE session_id=? AND archived=0 AND role='assistant' ORDER BY id DESC LIMIT 1",(owner,)).fetchone()
        # Pending user answer decides whether failed/stopped also count as ended.
        if latest and latest['delivery_status'] not in ('complete','stopped','failed','cancelled'):return '此项目最近一轮尚未完整回复，请先处理本轮'
    return ''


def switch_boundary(root,sid):
    with connect() as db:return boundary_reason(db,root,sid)


@contextmanager
def reserve(root,sid):
    root=Path(root).resolve(strict=True);token=uuid4().hex
    with catalog.transaction() as db:
        reason=boundary_reason(db,root,sid)
        if reason:raise HTTPException(409,reason)
        db.execute("INSERT INTO git_transitions VALUES(?,?,?,'running',?,'已确认分支操作；阻止同目录新执行')",(token,str(root),sid,time.time()))
    outcome='interrupted'
    try:
        yield token
        outcome='finished'
    finally:
        with connect() as db:db.execute('UPDATE git_transitions SET status=? WHERE id=?',(outcome,token))
