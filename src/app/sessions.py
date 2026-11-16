"""会话固定模式；一轮消息与请求结果在同一事务中保存。"""

import json
from datetime import datetime

from app.db import connect


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def init_table():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
            mode TEXT NOT NULL DEFAULT 'shared', created_at TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT, at TEXT)""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "speaker_snapshot" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN speaker_snapshot TEXT")
        if "archived" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        session_columns = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "timeline_version" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN timeline_version INTEGER NOT NULL DEFAULT 0")
        if "archived" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS completed_requests (
            request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)""")


def get_session(session_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(limit=50, mode=None, offset=0, include_archived=False):
    sql = "SELECT * FROM sessions"
    clauses = []
    args = []

    if mode is not None:
        clauses.append("mode = ?")
        args.append(mode)
    if not include_archived:
        clauses.append("archived = 0")

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    with connect() as conn:
        rows = conn.execute(
            sql + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()

    return [dict(row) for row in rows]


def get_messages(session_id, limit=200, before=None):
    sql = "SELECT id, role, content, at, speaker_snapshot FROM messages WHERE session_id = ? AND archived=0"
    args = [session_id]
    if before is not None:
        sql += " AND id < ?"
        args.append(before)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
    messages = []
    for row in reversed(rows):
        item = dict(row)
        raw = item.pop("speaker_snapshot")
        item["speaker"] = json.loads(raw) if raw else None
        messages.append(item)
    return messages


def completed(request_id, fingerprint):
    with connect() as conn:
        row = conn.execute("SELECT * FROM completed_requests WHERE request_id = ?",
                           (request_id,)).fetchone()
    if row and row["fingerprint"] != fingerprint:
        raise ValueError("请求编号已用于其他内容，请重新发送")
    result = json.loads(row["result"]) if row else None
    if result:
        with connect() as conn:
            message = conn.execute("SELECT archived FROM messages WHERE id=?", (result.get('last_message_id'),)).fetchone()
        if message and message['archived']:
            raise ValueError("此请求的回复已撤回；不得恢复或自动重做，请重新发送新消息")
    return result


def save_turn(session_id, mode, text, result, request_id, fingerprint, expected_version=None):
    """失败整轮回滚；断线后用相同 request_id 可取回已经保存的结果。"""
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if expected_version is not None and session_id is not None:
            current = conn.execute('SELECT timeline_version FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not current or current[0] != expected_version:
                raise ValueError('会话已回退；旧回复不得保存')
        if session_id is None:
            cur = conn.execute(
                "INSERT INTO sessions(title, mode, created_at, updated_at) VALUES(?,?,?,?)",
                (text[:20], mode, _now(), _now()))
            session_id = cur.lastrowid
        else:
            row = conn.execute("SELECT mode FROM sessions WHERE id = ?",
                               (session_id,)).fetchone()
            if not row or row["mode"] != mode:
                raise ValueError("会话不存在或模式不匹配")
        for role, content in (("user", text), ("assistant", result["reply"])):
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, at, speaker_snapshot) VALUES(?,?,?,?,?)",
                (session_id, role, content, _now(),
                 json.dumps(result.get("speaker"), ensure_ascii=False)
                 if role == "assistant" and result.get("speaker") else None))
            from app import timeline
            timeline.checkpoint(conn, session_id, cur.lastrowid)
        result = {**result, "session_id": session_id, "last_message_id": cur.lastrowid}
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))
        conn.execute("INSERT INTO completed_requests VALUES(?,?,?)",
                     (request_id, fingerprint, json.dumps(result, ensure_ascii=False)))
    return result

