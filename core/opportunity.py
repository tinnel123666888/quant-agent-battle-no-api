"""机会捕手 (opportunity_scout) Agent — 5 分钟规则扫描，无 LLM。

功能：
1. 持仓加仓：持仓票 30 分钟内涨 ≥3% 且成交量放大 → 加仓 30%
2. 急跌补仓：持仓票当日跌 ≥2% 但浮亏 < 5%（未触止损）→ 加仓 50% 摊低成本
3. 主动追入：涨幅榜单只票连续 2 期出现 + 涨幅 5-9.5%（未涨停）+ 当前持仓没有 → 5% 试仓
4. 风险信号：持仓急跌 ≥5% 但未触止损 → 写 memory 给 CIO 关注

频率：5 分钟（与 quote/guardian 同频但功能互补）
- guardian = 卖（亏到阈值）
- opportunity_scout = 买（机会到阈值）
- brain = 大方向（每小时综合）
"""
from __future__ import annotations
import os
import time

from . import db, market, broker, memory


# 阈值（环境变量可调）
BREAKOUT_PCT = float(os.getenv("OPP_BREAKOUT_PCT", "0.03"))   # 30 分钟内涨 3%
DIP_PCT = float(os.getenv("OPP_DIP_PCT", "0.02"))             # 当日跌 2%
ENTRY_TEST_PCT = float(os.getenv("OPP_ENTRY_PCT", "0.05"))    # 5% 试仓
MAX_TOTAL_POSITION = float(os.getenv("OPP_MAX_POSITION", "0.92"))  # 总仓位上限
MAX_PER_STOCK = float(os.getenv("OPP_MAX_PER_STOCK", "0.20"))      # 单票上限
TRACK_TICKS = int(os.getenv("OPP_TRACK_TICKS", "2"))           # 涨幅榜连续 N 次出现


def _portfolio_total_and_holdings():
    cash = db.get_cash()
    pos = db.get_positions()
    if not pos:
        return cash, cash, {}, {}
    quotes = {q["code"]: q for q in market.fetch_quotes(list(pos.keys()))}
    mv = 0.0
    holdings = {}
    for code, p in pos.items():
        px = quotes.get(code, {}).get("price") or p["avg_price"]
        v = px * p["qty"]
        mv += v
        holdings[code] = {"qty": p["qty"], "avg_price": p["avg_price"],
                          "price": px, "mv": v, "available": p["available"]}
    return cash, cash + mv, holdings, quotes


def _price_30min_ago(code: str, now_price: float) -> float | None:
    """从 market_snapshot 取 30 分钟前快照"""
    cutoff = int(time.time()) - 30 * 60
    with db.conn() as c:
        row = c.execute(
            "SELECT price FROM market_snapshot WHERE code=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1",
            (code, cutoff),
        ).fetchone()
    return row["price"] if row else None


def opportunity_scan() -> dict:
    """规则扫描，触发即下买单。返回 {actions: [...], checked, ts}"""
    actions: list[dict] = []
    cash, total, holdings, quotes = _portfolio_total_and_holdings()
    cur_position = (total - cash) / total if total else 0
    if total <= 0:
        return {"actions": [], "checked": 0, "ts": int(time.time())}

    # ① ② 持仓票：突破/急跌补仓
    for code, h in holdings.items():
        px = h["price"]
        avg = h["avg_price"]
        weight = h["mv"] / total
        if weight >= MAX_PER_STOCK:
            continue
        if cur_position >= MAX_TOTAL_POSITION:
            break

        # 急跌补仓
        prev_close = quotes.get(code, {}).get("open") or avg
        day_pct = (px / prev_close - 1) if prev_close else 0
        pnl_pct = (px / avg - 1)
        if day_pct <= -DIP_PCT and -0.05 < pnl_pct <= -0.005:
            # 浮亏 0.5%~5% 且当日跌 ≥ 2% → 加仓 50% 摊低
            add_value = h["mv"] * 0.5
            qty = int(add_value / px / 100) * 100
            if qty >= 100 and qty * px <= cash:
                r = broker.execute(
                    code, "BUY", qty, px,
                    reason=f"急跌补仓 当日{day_pct*100:.1f}% 浮亏{pnl_pct*100:.1f}%"
                )
                actions.append({"code": code, "rule": "dip_buy",
                                  "qty": qty, "ok": r.get("ok")})
                if r.get("ok"):
                    cash -= qty * px
                    memory.remember(
                        kind="trade",
                        title=f"📥 急跌补仓 BUY {code} qty={qty}",
                        content=f"{day_pct*100:.1f}% 跌幅 + {pnl_pct*100:.1f}% 浮亏，规则补仓",
                        importance=4, code=code, ttl_sec=14 * 86400,
                    )
                    continue  # 已加仓不再触发突破

        # 突破加仓：30 分钟内涨 ≥3% 且当日仍 < 9% (不追涨停板)
        prev_30min = _price_30min_ago(code, px)
        if prev_30min:
            short_pct = (px / prev_30min - 1)
            if short_pct >= BREAKOUT_PCT and day_pct < 0.09:
                add_value = h["mv"] * 0.3
                qty = int(add_value / px / 100) * 100
                if qty >= 100 and qty * px <= cash:
                    r = broker.execute(
                        code, "BUY", qty, px,
                        reason=f"突破加仓 30min涨{short_pct*100:.1f}%"
                    )
                    actions.append({"code": code, "rule": "breakout_add",
                                      "qty": qty, "ok": r.get("ok")})
                    if r.get("ok"):
                        cash -= qty * px
                        memory.remember(
                            kind="trade",
                            title=f"🚀 突破加仓 BUY {code} qty={qty}",
                            content=f"30min 涨 {short_pct*100:.1f}%，规则加仓追势",
                            importance=4, code=code, ttl_sec=14 * 86400,
                        )

    # ③ 主动追入：涨幅榜单只票连续出现 + 涨幅 5-9.5%（未涨停）
    if cur_position < MAX_TOTAL_POSITION and cash > 5000:
        try:
            rows = market.fetch_market_snapshot()
            if rows:
                rising = [r for r in rows if 0.05 <= r["pct"] / 100 < 0.095]
                rising.sort(key=lambda r: r.get("amount") or 0, reverse=True)
                # 简化：直接选成交额最大且不在持仓中的，不要求连续出现（避免太复杂）
                for row in rising[:5]:
                    code = row["code"]
                    if code in holdings:
                        continue
                    if not (code.startswith(("60", "00", "30", "68"))):
                        continue
                    px = row["price"]
                    test_value = total * ENTRY_TEST_PCT
                    qty = int(test_value / px / 100) * 100
                    if qty >= 100 and qty * px <= cash:
                        r = broker.execute(
                            code, "BUY", qty, px,
                            reason=f"主动追入 涨{row['pct']:.1f}% 成交额{row['amount']/1e8:.1f}亿"
                        )
                        actions.append({"code": code, "rule": "active_entry",
                                          "qty": qty, "ok": r.get("ok"),
                                          "name": row.get("name")})
                        if r.get("ok"):
                            cash -= qty * px
                            memory.remember(
                                kind="trade",
                                title=f"🎯 主动追入 BUY {code} {row.get('name','')}",
                                content=f"涨幅榜 +{row['pct']:.1f}%，成交额"
                                        f"{row['amount']/1e8:.1f}亿，{ENTRY_TEST_PCT*100:.0f}%试仓",
                                importance=4, code=code, ttl_sec=14 * 86400,
                            )
                            cur_position = (total + sum(a.get("qty", 0) * px for a in actions
                                                          if a.get("rule") == "active_entry") - cash) / total
                        if cur_position >= MAX_TOTAL_POSITION:
                            break
        except Exception:
            pass

    # ④ 风险预警：持仓急跌但未触止损
    for code, h in holdings.items():
        px = h["price"]
        avg = h["avg_price"]
        pnl_pct = (px / avg - 1)
        if -0.08 < pnl_pct <= -0.05:
            memory.remember(
                kind="observation",
                title=f"⚠️ 持仓警告：{code} 浮亏 {pnl_pct*100:.1f}%",
                content=f"接近止损线（-8%），CIO 下次决策应重新评估",
                importance=4, code=code, ttl_sec=2 * 86400,
            )

    return {"actions": actions, "checked": len(holdings),
            "cur_position": round(cur_position, 3),
            "ts": int(time.time()),
            "thresholds": {
                "breakout_pct": BREAKOUT_PCT,
                "dip_pct": DIP_PCT,
                "entry_test_pct": ENTRY_TEST_PCT,
                "max_position": MAX_TOTAL_POSITION,
            }}
