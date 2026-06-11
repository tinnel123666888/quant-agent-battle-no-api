"""每日复盘 / 反思 — 收盘后调用：
1. 计算 today_pnl, alpha vs CSI300
2. LLM 写反思（成功/失败原因），存入 daily_review.reflection
3. 反思自动写进长期记忆，下一轮 CIO 决策能读到
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timedelta

from . import db, market, llm, memory
from .config import INITIAL_CAPITAL, get_model


REFLECT_SYS = (
    "你是基金经理的复盘助手。基于今日组合表现 + 持仓 + 关键决策，"
    "用 4-6 句中文写出本日复盘反思（**只输出 JSON**）。"
    "JSON 字段："
    '{"summary":"一句话整体评价",'
    '"good":"做对了什么（具体到票或决策）",'
    '"bad":"哪里错了/被打脸",'
    '"lesson":"明天要改什么（最多 2 条具体行动）",'
    '"alpha_view":"超额/落后基准的核心原因"}'
)


def _csi300_today_change() -> float:
    """今日沪深300 涨跌幅（百分比）。失败返回 None。"""
    try:
        import urllib.request
        url = "http://hq.sinajs.cn/list=sh000300"
        req = urllib.request.Request(url, headers={
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        })
        raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
        body = raw.split("=", 1)[1].strip().strip(';"')
        fields = body.split(",")
        if len(fields) >= 4:
            try:
                price = float(fields[1])
                prev = float(fields[2])
                if prev:
                    return round((price - prev) / prev * 100, 2)
            except Exception:
                pass
    except Exception:
        pass
    return 0.0


def _today_open_close_total() -> tuple[float | None, float | None]:
    today = datetime.now().strftime("%Y-%m-%d")
    today_start_ts = int(datetime.strptime(today + " 09:00:00", "%Y-%m-%d %H:%M:%S").timestamp())
    today_end_ts = int(datetime.strptime(today + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp())
    with db.conn() as c:
        first = c.execute(
            "SELECT total FROM equity WHERE ts BETWEEN ? AND ? ORDER BY ts ASC LIMIT 1",
            (today_start_ts, today_end_ts),
        ).fetchone()
        last = c.execute(
            "SELECT total FROM equity WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
            (today_start_ts, today_end_ts),
        ).fetchone()
    return (first["total"] if first else None,
            last["total"] if last else None)


def run_daily_reflection() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    open_total, close_total = _today_open_close_total()
    if not open_total or not close_total:
        # 无当日数据 → 用上一次 equity 当作 close、INITIAL 当作 start
        with db.conn() as c:
            row = c.execute("SELECT total FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        close_total = row["total"] if row else INITIAL_CAPITAL
        open_total = open_total or INITIAL_CAPITAL

    pnl = close_total - open_total
    pnl_pct = (close_total / open_total - 1) * 100 if open_total else 0
    bench = _csi300_today_change()
    alpha = round(pnl_pct - bench, 2)

    # 拉今日成交、当前持仓
    with db.conn() as c:
        today_start_ts = int(datetime.strptime(today + " 00:00:00", "%Y-%m-%d %H:%M:%S").timestamp())
        orders = [dict(r) for r in c.execute(
            "SELECT code,side,qty,price,reason FROM orders WHERE ts>=? ORDER BY id ASC",
            (today_start_ts,),
        ).fetchall()]
    pos = db.get_positions()
    code_names = market._load_code_names() if hasattr(market, "_load_code_names") else {}
    holdings_brief = {
        code_names.get(c, c): {
            "qty": p["qty"],
            "avg": p["avg_price"],
            "now": market.fetch_quotes([c])[0]["price"] if c else 0,
        }
        for c, p in list(pos.items())[:15]
    }
    stops = db.recent_stop_events(limit=10)

    user = json.dumps({
        "date": today,
        "total_open": round(open_total, 2),
        "total_close": round(close_total, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "csi300_pct": bench,
        "alpha_vs_csi300": alpha,
        "trades_today": orders,
        "stop_events_recent": stops,
        "holdings_now": holdings_brief,
    }, ensure_ascii=False, default=str)

    parsed = {}
    for attempt in range(2):
        resp = llm.chat(REFLECT_SYS, user, json_mode=True,
                        max_tokens=1200, model=get_model("daily_reflection"))
        if resp.get("ok"):
            parsed = llm.parse_json(resp.get("text", "")) or {}
            if parsed:
                break
        time.sleep(1)

    reflection_text = (
        f"【{today}】PnL={pnl_pct:+.2f}% vs CSI300 {bench:+.2f}% "
        f"(alpha {alpha:+.2f}%) | "
        f"summary: {parsed.get('summary','')} | "
        f"good: {parsed.get('good','')} | "
        f"bad: {parsed.get('bad','')} | "
        f"lesson: {parsed.get('lesson','')}"
    )
    db.upsert_daily_review(
        date=today,
        total_start=round(open_total, 2),
        total_end=round(close_total, 2),
        pnl=round(pnl, 2),
        pnl_pct=round(pnl_pct, 2),
        benchmark_pct=bench,
        alpha=alpha,
        holdings_json=json.dumps(holdings_brief, ensure_ascii=False),
        reflection=reflection_text,
    )
    # 注入长期记忆（importance=5，永久）
    memory.remember(
        kind="fact",
        title=f"📓 {today} 收盘复盘 PnL {pnl_pct:+.2f}% (alpha {alpha:+.2f}%)",
        content=reflection_text,
        importance=5,
        ttl_sec=None,
    )
    return {"date": today, "pnl_pct": pnl_pct, "alpha": alpha,
            "reflection": parsed, "trades": len(orders)}
