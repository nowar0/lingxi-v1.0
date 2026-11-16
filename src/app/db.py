"""短事务连接；异常时回滚，无论成功失败都关闭连接。"""

import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()
