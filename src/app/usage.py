"""用量账本：每次调用模型花了多少 token，记一行。

这是"只增不改"的流水账，将来算钱、看使用习惯都靠它。
"""

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from contextvars import ContextVar
from contextlib import contextmanager, closing

from app.config import DB_PATH

_owner = ContextVar('lingxi_usage_owner', default={})

@contextmanager
def scope(**owner):
    token = _owner.set({**_owner.get(), **owner})
    try:
        yield
    finally:
        _owner.reset(token)


def init_table():
    """建账本 + 补新列。跑多少遍都安全：表在就跳过，列在就跳过。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            序号 INTEGER PRIMARY KEY AUTOINCREMENT,
            角色 TEXT,
            输入 INTEGER,
            输出 INTEGER,
            时间 TEXT
        )
    """)

    # 老账本没有这三列。先问数据库"你现在有哪些列"，缺哪列补哪列。
    # 名字 + 类型成对放，循环里一次处理一个。
    new_columns = [
        ("模型", "TEXT"),     # 当时真正调用的模型全名，如 deepseek/deepseek-flash
        ("缓存", "INTEGER"),  # 输入里命中缓存的部分（注意：它是"输入"的一部分，不是额外的）
        ("思考", "INTEGER"),  # 输出里花在"想"上的部分（DeepSeek 的 reasoning_tokens）
        ('session_id', 'INTEGER'),
        ('job_id', 'TEXT'),
        ('node_id', 'TEXT'),
        ('agent_id', 'TEXT'),
        ('kind', 'TEXT'),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
    for name, kind in new_columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE usage ADD COLUMN {name} {kind}")

    conn.execute('CREATE INDEX IF NOT EXISTS usage_owner ON usage(session_id,job_id,node_id,agent_id)')
    conn.commit()
    conn.close()



def _pick_int(obj, *paths):
    """按顺序试几个字段名；缺失返回 None，真实的零保留为 0。

    paths 里用点号表示"往下钻一层"，
    例如 "prompt_tokens_details.cached_tokens" 表示 obj.prompt_tokens_details.cached_tokens
    """
    for path in paths:
        value = obj
        for name in path.split("."):
            value = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
            if value is None:
                break
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def extract(usage_obj):
    """把厂商的用量对象，翻译成我们要记的四个数。

    不同厂商把数字放在不同字段里，所以每条都写"首选 + 兜底"。
    缺失记 NULL；它与厂商明确返回的 0 不同。
    """
    if usage_obj is None:
        return {"输入": None, "缓存": None, "输出": None, "思考": None}

    return {
        "输入": _pick_int(usage_obj, "prompt_tokens", "input_tokens"),
        "输出": _pick_int(usage_obj, "completion_tokens", "output_tokens"),
        # 探针实测：DeepSeek 两条路都通。官方 SDK 的正规位（cached_tokens）优先，
        # 拿不到再退到它模板外的 prompt_cache_hit_tokens。
        "缓存": _pick_int(
            usage_obj,
            "prompt_tokens_details.cached_tokens",
            "prompt_cache_hit_tokens",
        ),
        # 思考：不是每家都给这个数，取不到记 NULL。
        "思考": _pick_int(usage_obj, "completion_tokens_details.reasoning_tokens"),
    }


def record(role, model_name, usage_obj, *, db=None, **owner):
    """Append real or explicitly unknown usage; optionally join a caller transaction."""
    numbers = extract(usage_obj)
    owner = {**_owner.get(), **owner}
    values = (role, model_name, numbers["输入"], numbers["缓存"], numbers["输出"], numbers["思考"],
              datetime.now().strftime("%Y-%m-%d %H:%M:%S"), owner.get('session_id'),
              owner.get('job_id') or '', owner.get('node_id') or '', owner.get('agent_id') or '',
              owner.get('kind') or 'chat')
    sql = ("INSERT INTO usage (角色, 模型, 输入, 缓存, 输出, 思考, 时间,session_id,job_id,node_id,agent_id,kind)"
           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    if db is not None:
        db.execute(sql, values)
    else:
        with closing(sqlite3.connect(DB_PATH)) as connection, connection:
            connection.execute(sql, values)


# 一次查询，就把每个角色要的 9 个数全算出来。顺序必须和 _row_to_dict 一一对应。
_SUMMARY_SQL = """
    SELECT
        角色,
        COUNT(*),                                                       -- 这个角色记了几笔
        COALESCE(SUM(输入), 0),                                         -- 输入：每笔都有值
        COALESCE(SUM(输出), 0),                                         -- 输出：每笔都有值
        COALESCE(SUM(缓存), 0),  -- 缓存：老记录是 NULL，当 0 加
        COALESCE(SUM(思考), 0),                                         -- 思考：同上
        COUNT(思考),                                                    -- 有几笔真的记了思考
        COALESCE(SUM(CASE WHEN 思考 IS NOT NULL THEN 输出 ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN 缓存 IS NOT NULL THEN 输入 ELSE 0 END), 0)
    FROM usage
    GROUP BY 角色
"""


def _row_to_dict(r):
    """把查询出来的一行 9 个数，装成带名字的字典。

    这里的顺序必须和 _SUMMARY_SQL 里 SELECT 的顺序一模一样，错位了不报错、只是数字会串。
    """
    return {
        "角色": r[0],
        "笔数": r[1],
        "输入": r[2],
        "输出": r[3],
        "缓存": r[4],
        "思考": r[5],
        "有思考笔数": r[6],
        "_思考分母": r[7],
        "_缓存分母": r[8],
    }


def _ratio(part, whole):
    """算百分比。分母是 0 就返回 None。

    SQL 里除零不报错、只给 NULL；但这两个数是在 Python 里算的，
    0 当除数会抛 ZeroDivisionError 直接把接口打崩，所以必须在这挡住。
    """
    if not whole:
        return None
    return round(part / whole * 100, 1)


def summary(session_id=None, job_id=None, node_id=None):
    """算总账：一共几笔；再按角色拆开看。

    关于 NULL（加这三列之前记的老数据没有 缓存/思考）：
      - 求"总额"时当 0 算 —— 得到的是"至少这么多"，是诚实的下界；
      - 求"占比"时不算 —— 只拿真的有数据的记录当分母。把"不知道"当成"0"
        塞进分母，会把比例稀释成一个看起来很正常、其实是假的数字。
      - 分母是 0 时占比返回 None，而不是 0 —— "没有数据"不等于"0%"。
    """
    conn = sqlite3.connect(DB_PATH)
    filters, args = [], []
    for field, value in (('session_id',session_id),('job_id',job_id),('node_id',node_id)):
        if value is not None:
            filters.append(field+'=?');args.append(value)
    sql = _SUMMARY_SQL.replace('FROM usage','FROM usage WHERE '+' AND '.join(filters)) if filters else _SUMMARY_SQL
    rows = conn.execute(sql,args).fetchall()
    where = ' WHERE '+' AND '.join(filters) if filters else ''
    known = conn.execute('SELECT COALESCE(SUM(CASE WHEN 缓存 IS NOT NULL AND 输入 IS NOT NULL THEN MAX(0,输入-缓存) ELSE 0 END),0),COUNT(输入),COUNT(输出) FROM usage'+where,args).fetchone()
    conn.close()

    by_role = [_row_to_dict(r) for r in rows]

    total = {
        key: sum(r[key] for r in by_role)
        for key in ("笔数", "输入", "输出", "缓存", "思考", "有思考笔数")
    }

    # 两个分母先各自加总，再算总占比。
    # 不能拿各角色的百分比去平均 —— 那会让"1 笔"和"100 笔"的角色权重一样。
    think_base = sum(r["_思考分母"] for r in by_role)
    cache_base = sum(r["_缓存分母"] for r in by_role)

    def clean(item):
        """藏掉内部用的分母字段，再补上这个角色自己的两个占比"""
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        out["思考占输出比"] = _ratio(out["思考"], item["_思考分母"])
        out["缓存占输入比"] = _ratio(out["缓存"], item["_缓存分母"])
        return out

    return {
        "总笔数": total["笔数"],
        "总输入": total["输入"],
        "总输出": total["输出"],
        "总缓存": total["缓存"],
        "总思考": total["思考"],
        '总未缓存输入': known[0],
        '输入已知笔数': known[1],
        '输出已知笔数': known[2],
        '用量完整': known[1]==total['笔数'] and known[2]==total['笔数'],
        "有思考记录的笔数": total["有思考笔数"],
        "思考占输出比": _ratio(total["思考"], think_base),
        "缓存占输入比": _ratio(total["缓存"], cache_base),
        "按角色": [clean(r) for r in by_role],
    }


# Scoped endpoints never infer ownership for legacy rows lacking an owner.
from fastapi import APIRouter, HTTPException
router = APIRouter(prefix='/v6')

@router.get('/sessions/{sid}/usage')
def session_usage(sid: int):
    from app import sessions
    if not sessions.get_session(sid):
        raise HTTPException(404,'会话不存在')
    return {**summary(session_id=sid),'session_id':sid,'子智能体':agent_summaries(sid)}

def agent_summaries(sid, job_id=None, node_id=None):
    args=[sid];where="session_id=? AND kind='agent'"
    for field,value in (('job_id',job_id),('node_id',node_id)):
        if value is not None:
            where+=' AND '+field+'=?';args.append(value)
    with closing(sqlite3.connect(DB_PATH)) as db:
        owners=db.execute('SELECT DISTINCT job_id,node_id FROM usage WHERE '+where,args).fetchall()
    return [{'job_id':jid,'node_id':nid,**summary(session_id=sid,job_id=jid,node_id=nid)} for jid,nid in owners]

@router.get('/jobs/{jid}/members/{nid}/usage')
def member_usage(jid: str, nid: str):
    from app import jobs
    job = jobs.snapshot(jid)
    if nid != '@supervisor' and not any(node['id']==nid for node in job['nodes']):
        raise HTTPException(404,'成员不存在')
    return {**summary(session_id=job['session_id'],job_id=jid,node_id='' if nid=='@supervisor' else nid),
            'session_id':job['session_id'],'job_id':jid,'node_id':nid}
