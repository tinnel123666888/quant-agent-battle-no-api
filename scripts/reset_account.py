"""一次性账户重置脚本：清空交易状态，cash 回到初始资金。

清空：positions / orders / equity / position_meta / market_snapshot / checkpoints
保留：memory / news_cache / agent_logs / strategy_state（历史决策与记忆不动）

用法：python -m quant_agent_battle.scripts.reset_account
"""
from __future__ import annotations
import time

try:
    from quant_agent_battle.core import db
    from quant_agent_battle.core.config import INITIAL_CAPITAL
except ImportError:
    from core import db
    from core.config import INITIAL_CAPITAL


def reset_account(keep_memory: bool = True) -> dict:
    cleared = {}
    with db.conn() as c:
        for tbl in ("positions", "orders", "equity",
                    "position_meta", "market_snapshot", "checkpoints"):
            try:
                n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                c.execute(f"DELETE FROM {tbl}")
                cleared[tbl] = n
            except Exception as e:
                cleared[tbl] = f"skip ({e})"
        if not keep_memory:
            for tbl in ("memory", "news_cache", "strategy_state"):
                try:
                    c.execute(f"DELETE FROM {tbl}")
                    cleared[tbl] = "cleared"
                except Exception:
                    pass
        # cash 回到初始
        c.execute("UPDATE account SET cash=?, updated_at=? WHERE id=1",
                  (INITIAL_CAPITAL, int(time.time())))
        # 重新埋一条初始 equity 点
        c.execute("INSERT INTO equity(ts,cash,market_value) VALUES (?,?,?)",
                  (int(time.time()), INITIAL_CAPITAL, 0.0))
    return {"cleared": cleared, "cash": INITIAL_CAPITAL}


if __name__ == "__main__":
    import json
    # 同时清掉合成价状态文件，避免旧假价残留
    try:
        try:
            from quant_agent_battle.core import market
        except ImportError:
            from core import market
        p = market._synth_state_path()
        if p.exists():
            p.unlink()
            print(f"[synth] removed {p.name}")
    except Exception as e:
        print(f"[synth] skip: {e}")
    result = reset_account(keep_memory=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
