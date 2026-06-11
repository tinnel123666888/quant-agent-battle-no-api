"""主编排器：驱动一次完整 tick：行情→Agent 群→CIO→Risk→Trader。"""
from __future__ import annotations
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from ..core import db, market, broker
from ..core.config import WATCHLIST, INITIAL_CAPITAL
from ..agents import personas


def snapshot_portfolio(quotes: dict[str, dict]) -> dict:
    cash = db.get_cash()
    pos = db.get_positions()
    mv = 0.0
    holdings = {}
    for code, p in pos.items():
        px = quotes.get(code, {}).get("price") or p["avg_price"]
        v = px * p["qty"]
        mv += v
        holdings[code] = {
            "qty": p["qty"],
            "available": p["available"],
            "avg_price": p["avg_price"],
            "price": px,
            "mv": round(v, 2),
            "pnl_pct": round((px / p["avg_price"] - 1) * 100, 2),
        }
    total = cash + mv
    return {
        "cash": round(cash, 2),
        "market_value": round(mv, 2),
        "total": round(total, 2),
        "return_pct": round((total / INITIAL_CAPITAL - 1) * 100, 2),
        "holdings": holdings,
    }


def execute_tick() -> dict:
    """运行一次完整决策周期。返回本轮摘要。"""
    tick_id = uuid.uuid4().hex[:8]
    t0 = time.time()

    # 1) 行情快照
    quotes_list = market.fetch_quotes(WATCHLIST)
    db.insert_snapshot(quotes_list)
    quotes = {q["code"]: q for q in quotes_list}

    # 2) 历史指标
    indicators = {}
    for code in WATCHLIST:
        hist = market.fetch_history(code, days=60)
        ind = market.compute_indicators(hist)
        if ind:
            indicators[code] = ind

    # 3) 新闻
    news = market.fetch_news(limit=10)

    # 4) 信息层 Agent 并发
    portfolio = snapshot_portfolio(quotes)
    reports = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {
            "scout": ex.submit(personas.run_scout, tick_id, news),
            "quant": ex.submit(personas.run_quant, tick_id, indicators),
            "macro": ex.submit(personas.run_macro, tick_id, news),
            "fundamental": ex.submit(personas.run_fundamental, tick_id, WATCHLIST, quotes),
            "sentiment": ex.submit(personas.run_sentiment, tick_id, quotes, news),
        }
        for name, fut in futs.items():
            try:
                reports[name] = fut.result(timeout=60)
            except Exception as e:
                reports[name] = {"_error": f"{type(e).__name__}: {e}"}

    # 5) CIO 决策
    cio = personas.run_cio(tick_id, portfolio, quotes, reports)

    # 6) Risk 审查
    risk = personas.run_risk(tick_id, cio, portfolio)

    # 7) Trader 执行
    trades = []
    approved_orders = risk.get("approved") if isinstance(risk, dict) else None
    if not approved_orders:
        approved_orders = cio.get("orders") if isinstance(cio, dict) else []
    if isinstance(approved_orders, dict):
        approved_orders = list(approved_orders.values())
    if not isinstance(approved_orders, list):
        approved_orders = []

    total_equity = portfolio["total"]
    for order in approved_orders:
        if not isinstance(order, dict):
            continue
        code = str(order.get("code", "")).strip()
        side = str(order.get("side", "")).upper()
        if code not in WATCHLIST or side not in ("BUY", "SELL"):
            continue
        price = quotes.get(code, {}).get("price")
        if not price:
            continue
        target_w = float(order.get("target_weight", 0) or 0)
        target_value = total_equity * target_w
        cur = portfolio["holdings"].get(code, {})
        cur_qty = cur.get("qty", 0)
        cur_value = cur.get("mv", 0)
        delta_value = target_value - cur_value

        if side == "BUY" and delta_value > 0:
            qty = int(delta_value / price)
            res = broker.execute(code, "BUY", qty, price,
                                 reason=str(order.get("reason", ""))[:200])
            trades.append(res)
        elif side == "SELL":
            if target_w == 0:
                qty = cur.get("available", 0)
            else:
                qty = max(0, int(-delta_value / price))
                qty = min(qty, cur.get("available", 0))
            if qty > 0:
                res = broker.execute(code, "SELL", qty, price,
                                     reason=str(order.get("reason", ""))[:200])
                trades.append(res)

    # 8) 净值入库
    final_portfolio = snapshot_portfolio(quotes)
    db.insert_equity(final_portfolio["cash"], final_portfolio["market_value"])

    summary = {
        "tick_id": tick_id,
        "ts": int(time.time()),
        "elapsed_sec": round(time.time() - t0, 2),
        "portfolio_before": portfolio,
        "portfolio_after": final_portfolio,
        "reports": reports,
        "cio": cio,
        "risk": risk,
        "trades": trades,
        "synthetic_quote": any(q.get("synthetic") for q in quotes_list),
    }
    db.log_agent(tick_id, "orchestrator", "summary", str(summary)[:1000], raw=summary)
    return summary


def daily_open_routine():
    """每日 9:30 前调用：T+1 释放可用股数。"""
    db.make_available_t1()
