"""记忆仓库：把「关于你的事」写进数据库，再取出来。

这一层只管读写数据库，不关心这些记忆接下来怎么用。
「怎么拼进 System Prompt」是下一步 memory/context.py 的事。
跟账本 app/usage.py 是同一种角色：一个文件管一类数据。
"""

import sqlite3
from datetime import datetime

from app.config import DB_PATH
from app.memory.keys import normalize_key

# ------------------------------------------------------------------ 建表

# 四张表，一条 SQL 一段，init_table() 挨个跑。
# IF NOT EXISTS = 表已经在了就跳过 —— 所以这段代码跑一百遍也不会坏，
# 和 usage.init_table() 一个套路。
_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS facts (
        key        TEXT NOT NULL,
        value      TEXT,
        mode       TEXT NOT NULL DEFAULT 'shared',
        updated_at TEXT,
        PRIMARY KEY (mode, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT,
        mode    TEXT DEFAULT 'shared',
        at      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS todo (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        text   TEXT,
        status TEXT DEFAULT 'open',
        due    TEXT,
        at     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mood (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT,
        note  TEXT,
        at    TEXT
    )
    """,
]


def _connect():
    """连上数据库。

    row_factory = sqlite3.Row 的作用：查询结果可以用列名取值（row["key"]），
    不用去数「这是第几列」。数位置的写法一旦列顺序变了，不报错、只是数字串位。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now():
    """统一的时间格式。存 ISO 字符串，不存中文时间 —— 中文时间没法排序、没法比大小。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_table():
    """建四张表。跑多少遍都安全。"""
    conn = _connect()
    for sql in _SCHEMA:
        conn.execute(sql)
    conn.commit()   # 不 commit，改动只活在这次连接里，连接一关就没了
    conn.close()


# ------------------------------------------------------------------ 事实


def set_fact(key, value, mode="shared"):
    """记一条事实。同一个 key 再写一次 = 覆盖，不是新增。

    ON CONFLICT(mode, key) DO UPDATE：只更新相同模式下的同名事实。
    这就是「说三次我喜欢咖啡，只存一行」的实现方式。

    excluded.value 是 SQLite 给的名字，指「本来打算插进去的那个值」。
    """
    if mode not in ("shared", "work", "companion"):
        raise ValueError("无效的记忆范围")
    if not isinstance(key, str) or not isinstance(value, str):
        raise ValueError("记忆名称和内容必须是文字")
    key = normalize_key(key)
    if not key or len(key) > 80 or not value.strip() or len(value) > 2000:
        raise ValueError("记忆名称限 1–80 字，内容限 1–2000 字")
    conn = _connect()
    conn.execute(
        """
        INSERT INTO facts (key, value, mode, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(mode, key) DO UPDATE SET
            value      = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, mode, _now()),
    )
    conn.commit()
    conn.close()


def get_facts(modes=("shared",), limit=10):
    """取最多 limit 条事实，最近更新的排前面。

    modes: 只看哪些归属的记忆；传 None 表示全都要。
    limit: 硬上限 —— 计划里定的红线是「事实 10 条 + 事件 10 条」。
           不设上限，就是把整本记事本塞给模型：每轮都花钱，还会把它带偏。
    """
    sql = "SELECT key, value, mode, updated_at FROM facts"
    params = []
    if modes is not None:
        sql += f" WHERE mode IN ({','.join(['?'] * len(modes))})"
        params += list(modes)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    conn = _connect()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_fact(key, mode):
    """明确指定范围，只删除该模式的同名事实。"""
    if mode not in ("shared", "work", "companion"):
        raise ValueError("无效的记忆范围")
    conn = _connect()
    conn.execute("DELETE FROM facts WHERE mode = ? AND key = ?", (mode, normalize_key(key)))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ 事件


def add_event(content, mode="shared"):
    """记一件「发生过的事」。流水账，只增不改。"""
    conn = _connect()
    conn.execute(
        "INSERT INTO events (content, mode, at) VALUES (?, ?, ?)",
        (content, mode, _now()),
    )
    conn.commit()
    conn.close()


def recent_events(limit=10, modes=("shared",)):
    """取最近 limit 条事件，按时间【从早到晚】返回。

    注意这里有个「取完再倒过来」的动作：
      - 要的是「最新的 N 条」，所以查询是 ORDER BY id DESC（倒序）
      - 但给模型看应该按正常时间顺序（先早后晚）
    顺序反了，模型会把因果搞反（「先吃饭、后觉得饿」）。
    """
    sql = "SELECT id, content, mode, at FROM events"
    params = []
    if modes is not None:
        sql += f" WHERE mode IN ({','.join(['?'] * len(modes))})"
        params += list(modes)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    conn = _connect()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]
