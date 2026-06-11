"""T+1 撮合引擎：买入次日可卖；含佣金/印花税/滑点。"""
from __future__ import annotations
from .config import COMMISSION_RATE, STAMP_TAX_RATE, SLIPPAGE_BPS, MIN_TRADE_AMOUNT
from . import db


def _round_lot(qty: int) -> int:
    """A 股 100 股一手。"""
    return (qty // 100) * 100


def _is_real_price(code: str, price: float) -> bool:
    """成交前校验价格是否为"真实行情"。
    防止合成价（synthetic）污染真实建仓 —— 这是导致均价离谱的根因。
    校验：重新拉一次该票实时行情，要求 synthetic=False 且价格与传入值接近。
    """
    try:
        from . import market
        q = next((x for x in market.fetch_quotes([code])
                  if x.get("code") == code), None)
    except Exception:
        q = None
    if not q:
        return False
    if q.get("synthetic"):
        return False
    real = q.get("price")
    if not real or real <= 0:
        return False
    # 传入价与实时真实价偏差 > 20% 视为不可信（可能用了旧合成价）
    if abs(price - real) / real > 0.20:
        return False
    return True


def execute(code: str, side: str, qty: int, price: float, reason: str = "") -> dict:
    """side: BUY / SELL；qty 为目标股数（自动取整到 100 整数倍）。"""
    qty = _round_lot(qty)
    if qty <= 0:
        return {"ok": False, "msg": "qty<100, skipped"}

    # ★ 关键修复：拒绝用合成价/失真价格成交（尤其是 BUY 建仓）
    if not _is_real_price(code, price):
        return {"ok": False, "code": code,
                "msg": "no real-time price (synthetic/stale), order rejected"}

    slip = price * SLIPPAGE_BPS / 10_000
    deal_price = price + slip if side == "BUY" else price - slip
    deal_price = round(deal_price, 2)
    notional = deal_price * qty
    commission = max(5.0, notional * COMMISSION_RATE)
    stamp = notional * STAMP_TAX_RATE if side == "SELL" else 0
    fee = round(commission + stamp, 2)

    if notional < MIN_TRADE_AMOUNT:
        return {"ok": False, "msg": f"notional<{MIN_TRADE_AMOUNT}"}

    cash = db.get_cash()
    positions = db.get_positions()

    if side == "BUY":
        cost = notional + fee
        if cost > cash:
            # 自适应缩量
            adj_qty = _round_lot(int((cash - 50) / (deal_price * (1 + COMMISSION_RATE))))
            if adj_qty < 100:
                return {"ok": False, "msg": "cash short"}
            qty = adj_qty
            notional = deal_price * qty
            commission = max(5.0, notional * COMMISSION_RATE)
            fee = round(commission, 2)
            cost = notional + fee
        new_cash = cash - cost
        prev = positions.get(code)
        if prev:
            new_qty = prev["qty"] + qty
            new_avg = (prev["avg_price"] * prev["qty"] + notional) / new_qty
            db.upsert_position(code, new_qty, round(new_avg, 4), prev["available"])  # 新买入今日不可卖
        else:
            db.upsert_position(code, qty, deal_price, 0)
        db.set_cash(new_cash)
        db.insert_order(code, side, qty, deal_price, "FILLED", fee, reason)
        # 更新 position_meta（买入价作为初始 high_water）
        try:
            db.upsert_position_meta(code, deal_price)
        except Exception:
            pass
        return {"ok": True, "side": "BUY", "code": code, "qty": qty, "price": deal_price, "fee": fee}

    if side == "SELL":
        prev = positions.get(code)
        if not prev or prev["available"] < qty:
            return {"ok": False, "msg": "no available shares (T+1)"}
        new_cash = cash + notional - fee
        new_qty = prev["qty"] - qty
        new_avail = prev["available"] - qty
        db.upsert_position(code, new_qty, prev["avg_price"], new_avail)
        db.set_cash(new_cash)
        db.insert_order(code, side, qty, deal_price, "FILLED", fee, reason)
        if new_qty == 0:
            try:
                db.delete_position_meta(code)
            except Exception:
                pass
        return {"ok": True, "side": "SELL", "code": code, "qty": qty, "price": deal_price, "fee": fee}

    return {"ok": False, "msg": f"unknown side {side}"}
