"""SQLite 持久化层：账户、持仓、订单、Agent 决策、净值曲线。"""
from __future__ import annotations
import sqlite3
import json
import time
from contextlib import contextmanager
from .config import DB_PATH, INITIAL_CAPITAL

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY,
    cash REAL NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    code TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    available INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    status TEXT NOT NULL,
    fee REAL NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    tick_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER PRIMARY KEY,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS market_snapshot (
    ts INTEGER NOT NULL,
    code TEXT NOT NULL,
    price REAL NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    volume REAL,
    pct REAL,
    PRIMARY KEY (ts, code)
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    importance INTEGER DEFAULT 1,
    code TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_memory_ts ON memory(ts DESC);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind, ts DESC);
CREATE TABLE IF NOT EXISTS news_cache (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    source TEXT,
    title TEXT NOT NULL,
    content TEXT,
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_cache(ts DESC);
CREATE TABLE IF NOT EXISTS position_meta (
    code TEXT PRIMARY KEY,
    high_water_price REAL NOT NULL DEFAULT 0,
    entry_ts INTEGER NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS daily_review (
    date TEXT PRIMARY KEY,
    total_start REAL,
    total_end REAL,
    pnl REAL,
    pnl_pct REAL,
    benchmark_pct REAL,
    alpha REAL,
    holdings_json TEXT,
    reflection TEXT
);
CREATE TABLE IF NOT EXISTS stop_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    code TEXT NOT NULL,
    rule TEXT NOT NULL,           -- hard_stop|trail_stop|portfolio_dd
    trigger_value REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    tick_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS strategy_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    view TEXT,
    themes TEXT,
    target_position REAL,
    confidence REAL,
    notes TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    try:
        yield c
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        row = c.execute("SELECT cash FROM account WHERE id=1").fetchone()
        if not row:
            c.execute(
                "INSERT INTO account(id,cash,updated_at) VALUES (1,?,?)",
                (INITIAL_CAPITAL, int(time.time())),
            )


def log_agent(tick_id: str, agent: str, role: str, content: str, raw=None):
    with conn() as c:
        c.execute(
            "INSERT INTO agent_logs(ts,tick_id,agent,role,content,raw) VALUES (?,?,?,?,?,?)",
            (int(time.time()), tick_id, agent, role, content,
             json.dumps(raw, ensure_ascii=False) if raw is not None else None),
        )


def get_cash() -> float:
    with conn() as c:
        return c.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"]


def set_cash(v: float):
    with conn() as c:
        c.execute("UPDATE account SET cash=?, updated_at=? WHERE id=1",
                  (v, int(time.time())))


def get_positions() -> dict:
    with conn() as c:
        rows = c.execute("SELECT code,qty,avg_price,available FROM positions WHERE qty>0").fetchall()
        return {r["code"]: dict(r) for r in rows}


def upsert_position(code: str, qty: int, avg_price: float, available: int):
    with conn() as c:
        c.execute(
            """INSERT INTO positions(code,qty,avg_price,available,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET qty=excluded.qty,
                  avg_price=excluded.avg_price, available=excluded.available,
                  updated_at=excluded.updated_at""",
            (code, qty, avg_price, available, int(time.time())),
        )


def insert_order(code, side, qty, price, status, fee, reason):
    with conn() as c:
        c.execute(
            "INSERT INTO orders(ts,code,side,qty,price,status,fee,reason) VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time()), code, side, qty, price, status, fee, reason),
        )


def insert_equity(cash: float, mv: float):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO equity(ts,cash,market_value,total) VALUES (?,?,?,?)",
                  (int(time.time()), cash, mv, cash + mv))


def insert_snapshot(rows):
    """rows: list[dict(ts,code,price,open,high,low,volume,pct)]"""
    with conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO market_snapshot(ts,code,price,open,high,low,volume,pct) VALUES (?,?,?,?,?,?,?,?)",
            [(r["ts"], r["code"], r["price"], r.get("open"), r.get("high"),
              r.get("low"), r.get("volume"), r.get("pct")) for r in rows],
        )


def make_available_t1():
    """T+1 释放：把昨日新增的可用置为持仓总数（简化版：每日开盘时调用）。"""
    with conn() as c:
        c.execute("UPDATE positions SET available=qty WHERE qty>0")


# ============ Memory ============

def write_memory(kind: str, title: str, content: str,
                 importance: int = 1, code: str | None = None,
                 expires_at: int | None = None):
    with conn() as c:
        c.execute(
            "INSERT INTO memory(ts,kind,importance,code,title,content,expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), kind, importance, code, title[:300],
             content[:4000], expires_at),
        )


def query_memory(kinds: list[str] | None = None,
                 limit: int = 30,
                 min_importance: int = 1,
                 since_ts: int | None = None) -> list[dict]:
    sql = ("SELECT id,ts,kind,importance,code,title,content "
           "FROM memory WHERE importance>=? ")
    args: list = [min_importance]
    if kinds:
        sql += " AND kind IN ({}) ".format(",".join(["?"] * len(kinds)))
        args.extend(kinds)
    if since_ts:
        sql += " AND ts>=? "
        args.append(since_ts)
    # 过滤已过期
    sql += " AND (expires_at IS NULL OR expires_at>?) "
    args.append(int(time.time()))
    sql += " ORDER BY importance DESC, ts DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def cache_news(item_id: str, source: str, title: str, content: str, url: str):
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO news_cache(id,ts,source,title,content,url) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, int(time.time()), source, title[:300], content[:4000], url[:500]),
        )


def recent_orders(limit: int = 50, since_ts: int | None = None) -> list[dict]:
    """历史成交（最新在前）。供前端滚动展示 + CIO 大脑观测。"""
    sql = ("SELECT id,ts,code,side,qty,price,fee,status,reason FROM orders ")
    args: list = []
    if since_ts:
        sql += " WHERE ts>=? "
        args.append(since_ts)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def order_stats(since_ts: int | None = None) -> dict:
    """成交统计：买/卖笔数、总手续费、净买入额。"""
    where = "WHERE ts>=?" if since_ts else ""
    args = [since_ts] if since_ts else []
    with conn() as c:
        rows = c.execute(
            f"SELECT side,COUNT(*) n,SUM(qty*price) notional,SUM(fee) fee "
            f"FROM orders {where} GROUP BY side", args
        ).fetchall()
    out = {"buy_count": 0, "sell_count": 0, "buy_notional": 0.0,
           "sell_notional": 0.0, "total_fee": 0.0}
    for r in rows:
        if r["side"] == "BUY":
            out["buy_count"] = r["n"]
            out["buy_notional"] = round(r["notional"] or 0, 2)
        elif r["side"] == "SELL":
            out["sell_count"] = r["n"]
            out["sell_notional"] = round(r["notional"] or 0, 2)
        out["total_fee"] = round((out["total_fee"] + (r["fee"] or 0)), 2)
    return out


def recent_news(limit: int = 30, since_ts: int | None = None) -> list[dict]:
    sql = "SELECT id,ts,source,title,content,url FROM news_cache "
    args: list = []
    if since_ts:
        sql += " WHERE ts>=? "
        args.append(since_ts)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ============ Position Meta（用于止损跟踪） ============

def upsert_position_meta(code: str, high_water_price: float,
                        entry_ts: int | None = None, notes: str | None = None):
    with conn() as c:
        c.execute(
            "INSERT INTO position_meta(code,high_water_price,entry_ts,notes) "
            "VALUES (?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
            "high_water_price=MAX(position_meta.high_water_price, excluded.high_water_price), "
            "notes=COALESCE(excluded.notes, position_meta.notes)",
            (code, high_water_price, entry_ts or int(time.time()), notes),
        )


def get_position_meta(code: str) -> dict | None:
    with conn() as c:
        r = c.execute(
            "SELECT code,high_water_price,entry_ts,notes FROM position_meta WHERE code=?",
            (code,),
        ).fetchone()
    return dict(r) if r else None


def list_position_meta() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT code,high_water_price,entry_ts,notes FROM position_meta"
        ).fetchall()
    return {r["code"]: dict(r) for r in rows}


def delete_position_meta(code: str):
    with conn() as c:
        c.execute("DELETE FROM position_meta WHERE code=?", (code,))


def log_stop_event(code: str, rule: str, trigger_value: float, note: str = ""):
    with conn() as c:
        c.execute(
            "INSERT INTO stop_events(ts,code,rule,trigger_value,note) VALUES (?,?,?,?,?)",
            (int(time.time()), code, rule, trigger_value, note),
        )


def recent_stop_events(limit: int = 30) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,ts,code,rule,trigger_value,note FROM stop_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()]


# ============ Daily Review ============

def upsert_daily_review(date: str, **kwargs):
    cols = ["total_start", "total_end", "pnl", "pnl_pct",
            "benchmark_pct", "alpha", "holdings_json", "reflection"]
    sets = []
    args = []
    for c in cols:
        if c in kwargs:
            sets.append(f"{c}=?")
            args.append(kwargs[c])
    args.append(date)
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO daily_review(date) VALUES (?)", (date,))
        if sets:
            c.execute(f"UPDATE daily_review SET {', '.join(sets)} WHERE date=?", args)


def recent_daily_reviews(limit: int = 30) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM daily_review ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()]


# ============ Checkpoints ============

def save_checkpoint(tick_id: str, stage: str, payload: str = ""):
    with conn() as c:
        c.execute(
            "INSERT INTO checkpoints(ts,tick_id,stage,payload) VALUES (?,?,?,?)",
            (int(time.time()), tick_id, stage, payload[:8000]),
        )


def get_last_checkpoint(tick_id: str | None = None) -> dict | None:
    with conn() as c:
        if tick_id:
            r = c.execute(
                "SELECT * FROM checkpoints WHERE tick_id=? ORDER BY id DESC LIMIT 1",
                (tick_id,),
            ).fetchone()
        else:
            r = c.execute(
                "SELECT * FROM checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
    return dict(r) if r else None


def latest_checkpoint_summary(limit: int = 5) -> list[dict]:
    """每个 tick_id 的最新 stage（用于"未完成 tick 恢复"判断）"""
    with conn() as c:
        rows = c.execute(
            "SELECT tick_id, MAX(id) AS last_id, COUNT(*) AS steps "
            "FROM checkpoints GROUP BY tick_id ORDER BY last_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        last = c.execute("SELECT stage,ts,payload FROM checkpoints WHERE id=?",
                         (r["last_id"],)).fetchone() if False else None
        with conn() as c2:
            last = c2.execute(
                "SELECT stage,ts,payload FROM checkpoints WHERE id=?",
                (r["last_id"],),
            ).fetchone()
        out.append({"tick_id": r["tick_id"], "steps": r["steps"],
                    "last_stage": last["stage"] if last else None,
                    "last_ts": last["ts"] if last else None})
    return out


# ============ Strategy State ============

def save_strategy_state(view: str, themes: list[str], target_position: float,
                        confidence: float = 0.5, notes: str = ""):
    with conn() as c:
        c.execute(
            "INSERT INTO strategy_state(ts,view,themes,target_position,confidence,notes) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), view, json.dumps(themes, ensure_ascii=False),
             float(target_position), float(confidence), notes[:500]),
        )


def get_latest_strategy() -> dict | None:
    with conn() as c:
        r = c.execute(
            "SELECT * FROM strategy_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["themes"] = json.loads(d["themes"]) if d.get("themes") else []
    except Exception:
        d["themes"] = []
    return d


def recent_strategies(limit: int = 30) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM strategy_state ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["themes"] = json.loads(d["themes"]) if d.get("themes") else []
        except Exception:
            d["themes"] = []
        out.append(d)
    return out
