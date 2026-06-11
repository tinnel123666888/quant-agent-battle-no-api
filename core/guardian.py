"""止损守护 Agent — 5 分钟规则扫描，无 LLM。

规则（金融专家建议）：
1. 硬止损：单票浮亏 >= HARD_STOP_PCT(默认 8%) → 立即清仓
2. 跟踪止损：浮盈达过 +TRAIL_TRIGGER(默认 10%) 后回撤 TRAIL_DRAWDOWN(默认 5%) → 清仓锁利
3. 单日组合最大回撤：总资产 vs 当日开盘下跌 >= PORTFOLIO_DD(默认 5%) → 全部清仓避险
"""
from __future__ import annotations
import time
from datetime import datetime
import os

from . import db, market, broker, memory


HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", "0.08"))
TRAIL_TRIGGER = float(os.getenv("TRAIL_TRIGGER", "0.10"))
TRAIL_DRAWDOWN = float(os.getenv("TRAIL_DRAWDOWN", "0.05"))
PORTFOLIO_DD = float(os.getenv("PORTFOLIO_DD", "0.05"))


def _today_open_total() -> float | None:
    """返回今日开盘时刻最早一条 equity 总额（用于算当日回撤）"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_start_ts = int(datetime.strptime(today + " 09:00:00", "%Y-%m-%d %H:%M:%S").timestamp())
    with db.conn() as c:
        row = c.execute(
            "SELECT total FROM equity WHERE ts>=? ORDER BY ts ASC LIMIT 1",
            (today_start_ts,),
        ).fetchone()
    return row["total"] if row else None


def guardian_scan() -> dict:
    """规则扫描，触发即下卖单。返回 {triggered: [...], checked: int, ts}"""
    pos = db.get_positions()
    if not pos:
        return {"triggered": [], "checked": 0, "ts": int(time.time())}
    quotes = {q["code"]: q for q in market.fetch_quotes(list(pos.keys()))}
    metas = db.list_position_meta()
    triggered = []

    # ① 单票规则
    for code, p in pos.items():
        avg = p["avg_price"]
        qty = p["qty"]
        avail = p["available"]
        px = quotes.get(code, {}).get("price") or avg
        # 持续更新 high_water
        meta = metas.get(code)
        hw = max(meta["high_water_price"] if meta else 0, px, avg)
        db.upsert_position_meta(code, hw)

        pnl_pct = (px / avg - 1)

        # 硬止损
        if pnl_pct <= -HARD_STOP_PCT:
            if avail > 0:
                r = broker.execute(code, "SELL", avail, px,
                                   reason=f"硬止损 浮亏 {pnl_pct*100:.2f}%")
                triggered.append({"code": code, "rule": "hard_stop",
                                   "pct": round(pnl_pct * 100, 2),
                                   "qty": avail, "ok": r.get("ok"),
                                   "msg": r.get("msg", "")})
                db.log_stop_event(code, "hard_stop", pnl_pct * 100,
                                  f"qty={avail} px={px}")
                memory.remember(
                    kind="trade",
                    title=f"⚠️ 硬止损 SELL {code} qty={avail} @{px}",
                    content=f"浮亏 {pnl_pct*100:.2f}%（>= {HARD_STOP_PCT*100:.0f}%），自动清仓避险",
                    importance=4, code=code, ttl_sec=30 * 86400,
                )
            else:
                # T+1 不可卖（available=0）→ 仅警告
                triggered.append({"code": code, "rule": "hard_stop_pending_t1",
                                   "pct": round(pnl_pct * 100, 2),
                                   "qty": 0, "ok": False,
                                   "msg": "T+1 not yet available"})
                db.log_stop_event(code, "hard_stop_pending_t1", pnl_pct * 100, "T+1")
            continue

        # 跟踪止损（浮盈过 +10% 后回撤 5%）
        if hw > avg * (1 + TRAIL_TRIGGER):
            drawdown_from_hw = (px / hw - 1)
            if drawdown_from_hw <= -TRAIL_DRAWDOWN:
                if avail > 0:
                    r = broker.execute(code, "SELL", avail, px,
                                       reason=f"跟踪止损 自高位 {drawdown_from_hw*100:.2f}%")
                    triggered.append({"code": code, "rule": "trail_stop",
                                       "pct": round(drawdown_from_hw * 100, 2),
                                       "qty": avail, "ok": r.get("ok"),
                                       "msg": r.get("msg", "")})
                    db.log_stop_event(code, "trail_stop", drawdown_from_hw * 100,
                                      f"hw={hw} now={px}")
                    memory.remember(
                        kind="trade",
                        title=f"📉 跟踪止损 SELL {code} qty={avail} @{px}",
                        content=f"自高水位 {hw:.2f} 回撤 {drawdown_from_hw*100:.2f}%，锁利出局",
                        importance=4, code=code, ttl_sec=30 * 86400,
                    )

    # ② 组合级最大回撤
    open_total = _today_open_total()
    if open_total:
        cash = db.get_cash()
        cur_pos = db.get_positions()  # 卖完后重新查
        cur_quotes = {q["code"]: q for q in market.fetch_quotes(list(cur_pos.keys()))}
        mv = sum((cur_quotes.get(c, {}).get("price", p["avg_price"]) * p["qty"])
                 for c, p in cur_pos.items())
        cur_total = cash + mv
        dd = (cur_total / open_total - 1)
        if dd <= -PORTFOLIO_DD and cur_pos:
            # 全部清仓（仅 available>0 的部分）
            for code, p in list(cur_pos.items()):
                if p["available"] > 0:
                    px = cur_quotes.get(code, {}).get("price") or p["avg_price"]
                    r = broker.execute(code, "SELL", p["available"], px,
                                       reason=f"组合回撤 {dd*100:.2f}%，整体避险")
                    triggered.append({"code": code, "rule": "portfolio_dd",
                                       "pct": round(dd * 100, 2),
                                       "qty": p["available"], "ok": r.get("ok")})
                    db.log_stop_event(code, "portfolio_dd", dd * 100,
                                      f"open={open_total} cur={cur_total}")
            memory.remember(
                kind="observation",
                title=f"⚠️ 组合最大回撤触发 {dd*100:.2f}%",
                content=f"总资产从开盘 {open_total:.0f} 跌至 {cur_total:.0f}，已触发整体止损",
                importance=5, ttl_sec=30 * 86400,
            )

    # 写当前 equity 快照
    cash = db.get_cash()
    pos2 = db.get_positions()
    if pos2:
        q2 = {q["code"]: q for q in market.fetch_quotes(list(pos2.keys()))}
        mv = sum((q2.get(c, {}).get("price", p["avg_price"]) * p["qty"])
                 for c, p in pos2.items())
    else:
        mv = 0.0
    db.insert_equity(cash, mv)

    return {"triggered": triggered, "checked": len(pos), "ts": int(time.time()),
            "thresholds": {"hard_stop": HARD_STOP_PCT,
                            "trail_trigger": TRAIL_TRIGGER,
                            "trail_drawdown": TRAIL_DRAWDOWN,
                            "portfolio_dd": PORTFOLIO_DD}}
