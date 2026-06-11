"""所有 Agent 全部走 LLM API。每个 Agent 输入「角色提示+实时材料」，输出 JSON。

Agent 列表：
  - scout       新闻侦察
  - quant       量化技术
  - macro       宏观/政策/地理文化
  - fundamental 基本面
  - sentiment   情绪
  - cio         首席投资官（综合决策）
  - risk        风控审查
  - trader      交易执行（把决策落到 broker，不再调 LLM）
"""
from __future__ import annotations
import json
from typing import Any
from ..core import llm, db


def _ask(tick_id: str, agent: str, system: str, user: str, max_tokens: int = 600) -> dict:
    db.log_agent(tick_id, agent, "prompt", user[:2000])
    resp = llm.chat(system=system, user=user, json_mode=True, max_tokens=max_tokens)
    if not resp["ok"]:
        db.log_agent(tick_id, agent, "error", resp.get("error", "unknown"), raw=resp)
        return {"_error": resp.get("error"), "_endpoint": None}
    text = resp["text"]
    db.log_agent(tick_id, agent, "raw", text[:4000], raw={"endpoint": resp["endpoint"]})
    parsed = llm.parse_json(text) or {"_raw_text": text}
    db.log_agent(tick_id, agent, "parsed", json.dumps(parsed, ensure_ascii=False)[:4000])
    return parsed


# ---------- Information Agents ----------

def run_scout(tick_id: str, news: list[dict]) -> dict:
    system = (
        "你是新闻侦察 Agent。基于最新财经新闻，提炼对 A 股影响。"
        "返回 JSON：{summary, themes:[…3-5个热点主题], bullish:[code|theme], "
        "bearish:[code|theme], confidence:0-1}"
    )
    user = "今日财经新闻：\n" + "\n".join(f"- {n['title']}" for n in news[:10])
    return _ask(tick_id, "scout", system, user)


def run_quant(tick_id: str, indicators: dict[str, dict]) -> dict:
    system = (
        "你是量化技术分析 Agent。根据 MA/动量/波动率，给每只股票 1 个评级 "
        "(strong_buy/buy/hold/sell/strong_sell) 和 0-1 的 score。"
        "返回 JSON：{stocks:{code:{rating, score, reason}}, top_pick}"
    )
    user = "技术指标：\n" + json.dumps(indicators, ensure_ascii=False, indent=2)
    return _ask(tick_id, "quant", system, user, max_tokens=800)


def run_macro(tick_id: str, news: list[dict]) -> dict:
    system = (
        "你是宏观研究 Agent，关注政策/利率/地缘/产业政策/中国地理文化因素（"
        "比如节假日消费、区域产业链、政策窗口期等）。返回 JSON："
        "{regime:bull|bear|neutral, drivers:[…], sectors_favor:[…], "
        "sectors_avoid:[…], risk_level:0-1}"
    )
    user = "近期新闻摘要：\n" + "\n".join(f"- {n['title']}" for n in news[:10])
    return _ask(tick_id, "macro", system, user)


def run_fundamental(tick_id: str, watchlist: list[str], quotes: dict[str, dict]) -> dict:
    system = (
        "你是基本面分析 Agent。基于你对 A 股龙头的常识（PE/PB/ROE/行业地位），"
        "对观察池打分。返回 JSON：{stocks:{code:{quality:0-1, value:0-1, comment}}, "
        "top_value:[…3只], top_quality:[…3只]}"
    )
    user = (
        "观察池：" + ", ".join(watchlist) + "\n现价：" +
        json.dumps({c: quotes.get(c, {}).get("price") for c in watchlist}, ensure_ascii=False)
    )
    return _ask(tick_id, "fundamental", system, user, max_tokens=800)


def run_sentiment(tick_id: str, quotes: dict[str, dict], news: list[dict]) -> dict:
    system = (
        "你是市场情绪 Agent。综合涨跌幅、新闻情绪、热度，判断短期情绪。"
        "返回 JSON：{mood:fear|neutral|greed, score:-1~1, hot:[code…], cold:[code…]}"
    )
    user = "今日涨跌：\n" + json.dumps(
        {c: q.get("pct") for c, q in quotes.items()}, ensure_ascii=False
    ) + "\n新闻：\n" + "\n".join(f"- {n['title']}" for n in news[:6])
    return _ask(tick_id, "sentiment", system, user)


# ---------- Decision Agents ----------

def run_cio(
    tick_id: str,
    portfolio: dict,
    quotes: dict[str, dict],
    reports: dict[str, dict],
) -> dict:
    system = (
        "你是 CIO（首席投资官），管理一个 10 万人民币 A 股账户（T+1）。"
        "汇总 5 个研究 Agent 报告 + 当前持仓 + 现价，输出今日操作建议。"
        "**必须返回 JSON**："
        '{thesis:"…一句话观点", '
        'orders:[{code, side:BUY|SELL|HOLD, target_weight:0-1, '
        'reason}], cash_target_weight:0-1, risk_budget:0-1}'
        "约束：单票 target_weight 不超过 0.25；总持仓 ≤ 1-cash_target_weight。"
    )
    payload = {
        "portfolio": portfolio,
        "quotes": {c: q.get("price") for c, q in quotes.items()},
        "reports": reports,
    }
    user = "决策素材：\n" + json.dumps(payload, ensure_ascii=False)[:6000]
    return _ask(tick_id, "cio", system, user, max_tokens=1200)


def run_risk(tick_id: str, cio_decision: dict, portfolio: dict) -> dict:
    system = (
        "你是风控官。检查 CIO 的指令是否违反规则："
        "①单票仓位 ≤25%；②单次新增买入金额 ≤ 总资产 30%；③现金不可为负。"
        "返回 JSON：{approved:[…orders], rejected:[{order, reason}], "
        "warnings:[…], adjusted_cash_target:0-1}"
    )
    user = json.dumps({"cio": cio_decision, "portfolio": portfolio}, ensure_ascii=False)[:6000]
    return _ask(tick_id, "risk", system, user, max_tokens=900)
