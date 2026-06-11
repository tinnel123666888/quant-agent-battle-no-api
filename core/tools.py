"""工具集：CIO 大脑通过 OpenAI tool-call 协议主动调用这些函数。"""
from __future__ import annotations
import json
import time
from . import market, db, memory, web_search
from .config import WATCHLIST


# ---------- 工具实现 ----------

def tool_web_search(query: str, limit: int = 8,
                    backend: str = "auto") -> dict:
    """互联网搜索（中文财经优先）。"""
    return web_search.web_search(query=query, limit=int(limit or 8),
                                  backend=str(backend or "auto"))


def tool_web_fetch(url: str) -> dict:
    """抓取一个 URL 的正文（HTML 剥壳）。"""
    return web_search.web_fetch(url=url)


def tool_fetch_quotes(codes: list[str] | None = None) -> dict:
    codes = codes or WATCHLIST
    quotes = market.fetch_quotes(codes)
    return {
        "ts": int(time.time()),
        "items": [{"code": q["code"], "price": q["price"],
                   "pct": q.get("pct"), "volume": q.get("volume"),
                   "synthetic": q.get("synthetic", False)} for q in quotes],
    }


def tool_get_history_indicators(code: str, days: int = 500) -> dict:
    hist = market.fetch_history(code, days=days)
    ind = market.compute_indicators(hist)
    sample = hist[-5:] if hist else []
    return {"code": code, "rows": len(hist), "tail": sample, "indicators": ind}


def tool_recent_news(limit: int = 30, since_min: int = 1440) -> dict:
    """读 news_cache（已经被 news_worker 异步抓取过的新闻）。
    since_min: 多少分钟内的新闻；默认 24 小时。
    """
    since_ts = int(time.time()) - since_min * 60
    rows = db.recent_news(limit=limit, since_ts=since_ts)
    return {"count": len(rows), "items": rows}


def tool_recall_memory(kinds: list[str] | None = None,
                       limit: int = 20,
                       min_importance: int = 1) -> dict:
    rows = memory.recall(kinds=kinds, limit=limit, min_importance=min_importance)
    return {"count": len(rows), "items": rows}


def tool_recent_trades(limit: int = 30, since_min: int | None = None) -> dict:
    """查询自己的历史买卖成交（CIO 决策观测的一环）。
    返回最近的成交明细 + 买/卖笔数/净买入/手续费统计。
    since_min: 只看最近多少分钟内的成交；不传则看全部。
    """
    since_ts = (int(time.time()) - since_min * 60) if since_min else None
    rows = db.recent_orders(limit=limit, since_ts=since_ts)
    stats = db.order_stats(since_ts=since_ts)
    # 精简字段给 LLM，附人类可读时间
    items = []
    for r in rows:
        items.append({
            "ts": r["ts"],
            "time": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
            "code": r["code"], "side": r["side"],
            "qty": r["qty"], "price": r["price"],
            "fee": r["fee"], "reason": (r.get("reason") or "")[:120],
        })
    return {"count": len(items), "stats": stats, "items": items}


def tool_remember(kind: str, title: str, content: str,
                  importance: int = 2, code: str | None = None) -> dict:
    memory.remember(kind=kind, title=title, content=content,
                    importance=importance, code=code)
    return {"ok": True}


def tool_get_portfolio() -> dict:
    quotes = {q["code"]: q for q in market.fetch_quotes(WATCHLIST)}
    cash = db.get_cash()
    pos = db.get_positions()
    holdings = {}
    mv = 0.0
    code_names = market._load_code_names() if hasattr(market, "_load_code_names") else {}
    # 拉取持仓中观察池外的票（确保有 quote）
    extra_codes = [c for c in pos.keys() if c not in quotes]
    if extra_codes:
        for q in market.fetch_quotes(extra_codes):
            quotes[q["code"]] = q
    for code, p in pos.items():
        px = quotes.get(code, {}).get("price") or p["avg_price"]
        v = px * p["qty"]
        mv += v
        holdings[code] = {
            "name": code_names.get(code, code),
            "qty": p["qty"], "available": p["available"],
            "avg_price": p["avg_price"], "price": px,
            "mv": round(v, 2),
            "pnl_pct": round((px / p["avg_price"] - 1) * 100, 2)
        }
    total = cash + mv
    return {"cash": round(cash, 2), "market_value": round(mv, 2),
            "total": round(total, 2),
            "holdings": holdings}


def tool_get_market_overview() -> dict:
    """大盘指数实时点位 + 全市场涨跌家数"""
    out: dict = {"indices": {}, "breadth": {}}
    # 主要指数（用新浪 hq.sinajs.cn 直连）
    import urllib.request
    idx_codes = {
        "上证指数": "sh000001", "深证成指": "sz399001",
        "沪深300": "sh000300", "创业板指": "sz399006",
        "科创50": "sh000688",
    }
    url = "http://hq.sinajs.cn/list=" + ",".join(idx_codes.values())
    req = urllib.request.Request(url, headers={
        "Referer": "http://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
        # 每行：var hq_str_sh000001="上证指数,3300.00,3290.00,...";
        for sym, line in zip(idx_codes.values(), raw.strip().split(";")):
            if "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().strip('"')
            fields = body.split(",")
            if len(fields) < 6:
                continue
            try:
                name = fields[0]
                price = float(fields[1] or 0)        # 当前点位
                prev = float(fields[2] or 0)         # 昨收
                # sina 指数行情字段：名称,当前,昨收,涨跌,涨跌幅(%或数),成交量,成交额
                pct = float(fields[4] or 0) if len(fields) > 4 else 0.0
                if abs(pct) > 50 and prev:           # 看似是涨跌额而非百分比
                    pct = (price - prev) / prev * 100
            except Exception:
                continue
            name_zh = next((k for k, v in idx_codes.items() if v == sym), name)
            out["indices"][name_zh] = {
                "code": sym, "price": round(price, 2),
                "pct": round(pct, 2),
            }
    except Exception:
        pass
    # 涨跌家数
    try:
        rows = market.fetch_market_snapshot()
        if rows:
            up = sum(1 for r in rows if r["pct"] > 0)
            down = sum(1 for r in rows if r["pct"] < 0)
            flat = sum(1 for r in rows if r["pct"] == 0)
            limit_up = sum(1 for r in rows if r["pct"] >= 9.8)
            limit_down = sum(1 for r in rows if r["pct"] <= -9.8)
            out["breadth"] = {
                "up": up, "down": down, "flat": flat,
                "limit_up": limit_up, "limit_down": limit_down,
                "total": len(rows),
                "up_ratio": round(up / max(1, len(rows)), 3),
            }
    except Exception:
        pass
    return out


def tool_market_top(sort: str = "amount",
                    desc: bool = True,
                    limit: int = 20,
                    contains: str | None = None) -> dict:
    """全市场榜单（拓宽视野）：sort = pct|amount|volume|price"""
    rows = market.fetch_market_snapshot()
    if not rows:
        return {"ok": False, "msg": "snapshot unavailable", "rows": []}
    if contains:
        rows = [r for r in rows if contains in r["code"] or contains in r.get("name", "")]
    key = sort if sort in ("amount", "pct", "volume", "price") else "amount"
    rows.sort(key=lambda r: r.get(key) or 0, reverse=desc)
    return {"ok": True, "total": len(rows),
            "rows": [{"code": r["code"], "name": r["name"], "price": r["price"],
                      "pct": r["pct"], "amount": r["amount"]}
                     for r in rows[:limit]]}


def tool_consult_financial_expert(
    draft_orders: list[dict] | None = None,
    cio_thesis: str = "",
    market_view: str = "",
) -> dict:
    """金融专家复审 — 独立 LLM 调用，扮演资深券商首席策略师角色。"""
    from . import llm as _llm
    if not isinstance(draft_orders, list):
        draft_orders = []

    # ★ 修复 1：必须传非空 draft_orders；空草案直接返回错误，强制 CIO 重做
    if len(draft_orders) < 5:
        return {
            "verdict": "ERROR",
            "overall_comment": (
                f"调用失败：draft_orders 必须至少 5 项，你只传了 {len(draft_orders)} 项。"
                "请先调 market_top + run_analysts + get_history_indicators 后，"
                "自己决定 8-15 只具体的票（含 code/side/target_weight/reason），"
                "再用非空 draft_orders 重调本工具。"
                "**不要等专家替你做决定！**"
            ),
            "per_order": [],
            "missing_considerations": ["draft_orders 太少或为空"],
            "confidence": 0.0,
        }

    portfolio = tool_get_portfolio()
    overview = tool_get_market_overview()
    top_pct = tool_market_top(sort="pct", desc=True, limit=10)
    top_amount = tool_market_top(sort="amount", desc=True, limit=10)

    # 仅 review 分支（非空 draft_orders）
    sys = (
        "你是中国资深金融专家，有 20 年 A 股投研和组合管理经验，**熟悉成长股和趋势投资**。"
        "现在另一个 AI（CIO）发来初步交易草案，请你以**保险/复审**身份独立评估。"
        "你的任务："
        "1) 判断整体方向是否合理（结合 A 股 T+1、当前市场环境、风格切换）；"
        "2) 逐条审视订单：仓位是否过大/过小？逻辑是否成立？基本面/技术面有无明显瑕疵？"
        "3) 找出 CIO 可能忽视的风险（黑天鹅、政策、行业景气拐点、估值陷阱、流动性等）；"
        "4) 给出可执行的修改建议（权重微调/剔除某只/补入某只）。"
        "**当前为 2026 年 AI 算力 / 半导体国产替代主线行情，对趋势龙头不应过度抑制；"
        "顺应主线远比追求'估值便宜'更重要**。"
        "**严格按 JSON 返回**：{"
        '"verdict": "APPROVE|MODIFY|REJECT", '
        '"overall_comment": "整体一句话结论", '
        '"per_order": [{"code":"...","judgment":"keep|trim|drop","suggested_weight":0-1,"reason":"..."}], '
        '"recommended_additions":[{"code","target_weight","reason"}], '
        '"missing_considerations": ["...风险/盲点..."], '
        '"confidence": 0-1}'
        "保持谨慎、专业、有数据支撑；不要客套，直接给意见。"
    )
    user = (
        f"# 当前组合\n{json.dumps(portfolio, ensure_ascii=False)}\n\n"
        f"# 大盘\n{json.dumps(overview, ensure_ascii=False)}\n\n"
        f"# 涨幅榜 top10\n{json.dumps(top_pct.get('rows', []), ensure_ascii=False)}\n\n"
        f"# 成交额榜 top10\n{json.dumps(top_amount.get('rows', []), ensure_ascii=False)}\n\n"
        f"# CIO 当前市场判断\n{market_view or '(未提供)'}\n\n"
        f"# CIO 思路\n{cio_thesis or '(未提供)'}\n\n"
        f"# 拟下订单（{len(draft_orders)} 笔）\n"
        f"{json.dumps(draft_orders, ensure_ascii=False, indent=2)}"
    )

    # 重试 2 次（专家模型支持按子 agent 配置）
    from .config import get_model
    parsed = None
    last_err = None
    for attempt in range(3):
        resp = _llm.chat(system=sys, user=user, json_mode=True,
                 max_tokens=2500, model=get_model("financial_expert"))
        if not resp.get("ok"):
            last_err = resp.get("error")
            time.sleep(1 + attempt)
            continue
        parsed = _llm.parse_json(resp.get("text", "")) or {}
        if parsed:
            break
        last_err = "JSON parse failed"
    if not parsed:
        return {"verdict": "ERROR",
                "overall_comment": f"专家 LLM 调用失败：{last_err}（已重试 3 次）。CIO 可基于自己判断继续，但请保守。",
                "per_order": [], "recommended_orders": [],
                "missing_considerations": ["专家网络异常"],
                "confidence": 0.0}
    db.log_agent("expert", "financial_expert", "review",
                 json.dumps(parsed, ensure_ascii=False)[:3000])
    from . import memory as _memory
    _memory.remember(
        kind="observation",
        title=f"金融专家复审：{parsed.get('verdict', '?')}",
        content=str(parsed.get("overall_comment", ""))[:400]
                + " | 风险点：" + ", ".join(parsed.get("missing_considerations", [])[:5]),
        importance=4,
        ttl_sec=7 * 86400,
    )
    return parsed
    sys = (
        "你是中国资深金融专家，有 20 年 A 股投研和组合管理经验，曾在头部券商任首席策略师。"
        "现在另一个 AI（CIO）发来初步交易草案，请你以**保险/复审**身份独立评估。"
        "你的任务："
        "1) 判断整体方向是否合理（结合 A 股 T+1、当前市场环境、风格切换）；"
        "2) 逐条审视订单：仓位是否过大/过小？逻辑是否成立？基本面/技术面有无明显瑕疵？"
        "3) 找出 CIO 可能忽视的风险（黑天鹅、政策、行业景气拐点、估值陷阱、流动性等）；"
        "4) 给出可执行的修改建议（权重微调/剔除某只/补入某只）。"
        "**严格按 JSON 返回**：{"
        '"verdict": "APPROVE|MODIFY|REJECT", '
        '"overall_comment": "整体一句话结论", '
        '"per_order": [{"code":"...","judgment":"keep|trim|drop","suggested_weight":0-1,"reason":"..."}], '
        '"missing_considerations": ["...风险/盲点..."], '
        '"confidence": 0-1}'
        "保持谨慎、专业、有数据支撑；不要客套，直接给意见。"
    )
    user = (
        f"# CIO 当前市场判断\n{market_view or '(未提供)'}\n\n"
        f"# CIO 思路\n{cio_thesis or '(未提供)'}\n\n"
        f"# 拟下订单（{len(draft_orders or [])} 笔）\n"
        f"{json.dumps(draft_orders or [], ensure_ascii=False, indent=2)}"
    )
    resp = _llm.chat(system=sys, user=user, json_mode=True, max_tokens=1800)
    if not resp.get("ok"):
        return {"verdict": "ERROR", "overall_comment": resp.get("error"),
                "per_order": [], "missing_considerations": [], "confidence": 0.0}
    parsed = _llm.parse_json(resp.get("text", "")) or {}
    if not parsed:
        parsed = {"verdict": "MODIFY", "overall_comment": resp.get("text", "")[:500],
                  "per_order": [], "missing_considerations": [], "confidence": 0.5}
    db.log_agent("expert", "financial_expert", "review",
                 json.dumps(parsed, ensure_ascii=False)[:3000])
    # 写入长期记忆，下次决策可读到
    from . import memory as _memory
    _memory.remember(
        kind="observation",
        title=f"金融专家复审：{parsed.get('verdict', '?')}",
        content=str(parsed.get("overall_comment", ""))[:400]
                + " | 风险点：" + ", ".join(parsed.get("missing_considerations", [])[:5]),
        importance=4,
        ttl_sec=7 * 86400,
    )
    return parsed


def tool_search_stocks(keyword: str, limit: int = 20) -> dict:
    """按公司名称模糊搜索全 A 股代码池（5500+ 只）。
    例：'比亚迪' / '银行' / '宁德' / '光伏' 都能命中。
    返回 [{code, name, price, pct, amount}]。
    """
    rows = market.fetch_market_snapshot()
    if not rows:
        return {"ok": False, "msg": "snapshot unavailable", "rows": []}
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "msg": "empty keyword", "rows": []}
    hit = [r for r in rows
           if kw in r.get("name", "") or kw in r["code"]]
    hit.sort(key=lambda r: r.get("amount") or 0, reverse=True)
    return {"ok": True, "total": len(hit), "rows": [
        {"code": r["code"], "name": r["name"], "price": r["price"],
         "pct": r["pct"], "amount": r["amount"]}
        for r in hit[:limit]
    ]}


def tool_market_overview() -> dict:
    return tool_get_market_overview()


def tool_get_strategy_state() -> dict:
    """读当前策略状态（上一轮决策遗留）+ 最近 5 轮历史。"""
    cur = db.get_latest_strategy()
    history = db.recent_strategies(limit=5)
    return {"current": cur, "history": history}


def tool_update_strategy_state(view: str = "neutral",
                                themes: list[str] | None = None,
                                target_position: float = 0.6,
                                confidence: float = 0.6,
                                notes: str = "") -> dict:
    """每轮 CIO 必须调用一次，写入本轮的 view/themes/target_position。
    view: bullish | neutral | bearish
    themes: 看好的 3-5 个主线（如 ["AI算力","创新药","出口链"]）
    """
    if view not in ("bullish", "neutral", "bearish"):
        view = "neutral"
    target_position = max(0.0, min(1.0, float(target_position or 0)))
    confidence = max(0.0, min(1.0, float(confidence or 0)))
    db.save_strategy_state(view=view, themes=themes or [],
                            target_position=target_position,
                            confidence=confidence, notes=notes)
    return {"ok": True, "view": view, "themes": themes or [],
            "target_position": target_position, "confidence": confidence}


def tool_run_analysts(candidate_codes: list[str] | None = None) -> dict:
    """★并行跑 4 个分析师（基本面 / 情绪 / 新闻 / 技术），CIO 综合参考。
    自动采集股吧 / 雪球 / 百度热度等社交数据 + 行业资金流。
    单次约 30-60s。"""
    from . import analysts
    if not candidate_codes:
        candidate_codes = list(WATCHLIST)
    return analysts.run_analysts(candidate_codes)


def tool_collect_social(codes: list[str] | None = None) -> dict:
    """单独采集股吧/雪球/百度热搜数据（不调 LLM）。"""
    from . import analysts
    return analysts.collect_social_data(codes or list(WATCHLIST))


def tool_industry_pulse() -> dict:
    """行业资金流向 / 板块涨跌（拓宽视野）。"""
    from . import analysts
    return analysts.collect_industry_pulse()


def _is_valid_a_share_code(code: str) -> bool:
    """A 股代码：6 位数字，沪/深/创业板/科创板"""
    return (len(code) == 6 and code.isdigit()
            and code[:2] in ("60", "00", "30", "68"))


def tool_place_orders(orders: list[dict]) -> dict:
    """orders: [{code, side:BUY|SELL, target_weight:0-1, reason}]
    支持任意 A 股代码（不再受 WATCHLIST 限制）。
    """
    from . import broker
    # 收集 needed codes：观察池 + 当前持仓 + 新订单
    needed_codes = set(WATCHLIST)
    pos = db.get_positions()
    needed_codes.update(pos.keys())
    for o in orders or []:
        if isinstance(o, dict):
            c = str(o.get("code", "")).strip()
            if c:
                needed_codes.add(c)
    quotes = {q["code"]: q for q in market.fetch_quotes(list(needed_codes))}

    cash = db.get_cash()
    holdings = {}
    mv = 0.0
    for code, p in pos.items():
        px = quotes.get(code, {}).get("price") or p["avg_price"]
        v = px * p["qty"]
        mv += v
        holdings[code] = {"qty": p["qty"], "available": p["available"],
                          "avg_price": p["avg_price"], "price": px,
                          "mv": round(v, 2)}
    total = cash + mv
    if total <= 0:
        total = 100_000.0

    results = []
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        code = str(o.get("code", "")).strip()
        side = str(o.get("side", "")).upper()
        if not _is_valid_a_share_code(code) or side not in ("BUY", "SELL"):
            results.append({"code": code, "ok": False, "msg": "invalid_code_or_side"})
            continue
        price = quotes.get(code, {}).get("price")
        is_synth = quotes.get(code, {}).get("synthetic", False)
        if not price:
            results.append({"code": code, "ok": False, "msg": "no_quote"})
            continue
        if is_synth:
            # ★ 合成价禁止成交（防止假价建仓污染均价，这是 44.8万 bug 的根因）
            results.append({"code": code, "ok": False,
                            "msg": "synthetic_price_rejected"})
            continue
        target_w = float(o.get("target_weight", 0) or 0)
        target_value = total * target_w
        cur = holdings.get(code, {})
        cur_value = cur.get("mv", 0.0)
        delta_value = target_value - cur_value
        reason = str(o.get("reason", ""))[:300]

        if side == "BUY" and delta_value > 0:
            qty = int(delta_value / price)
            r = broker.execute(code, "BUY", qty, price, reason=reason)
            results.append({"code": code, "side": "BUY", **r})
            if r.get("ok"):
                memory.remember(
                    "trade",
                    title=f"BUY {code} qty={qty} @{price}",
                    content=f"target_w={target_w}; reason={reason}",
                    importance=3, code=code,
                )
        elif side == "SELL":
            if target_w == 0:
                qty = cur.get("available", 0)
            else:
                qty = max(0, int(-delta_value / price))
                qty = min(qty, cur.get("available", 0))
            if qty > 0:
                r = broker.execute(code, "SELL", qty, price, reason=reason)
                results.append({"code": code, "side": "SELL", **r})
                if r.get("ok"):
                    memory.remember(
                        "trade",
                        title=f"SELL {code} qty={qty} @{price}",
                        content=f"target_w={target_w}; reason={reason}",
                        importance=3, code=code,
                    )
            else:
                results.append({"code": code, "side": "SELL", "ok": False,
                                "msg": "qty=0 (no available)"})
    # 更新 equity
    pf2 = tool_get_portfolio()
    db.insert_equity(pf2["cash"], pf2["market_value"])
    return {"results": results, "portfolio_after": pf2}


# ---------- 工具 schema（OpenAI tool-call） ----------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "★互联网搜索（中文财经优先，自动聚合东方财富+百度）。"
                "用于：宏观政策、行业最新动态、个股最新公告/异动、机构观点、地缘政治。"
                "**比 recent_news 更实时**，CIO 在判断主线时建议先搜 2-3 个关键词。"
                "返回 [{title,url,snippet,source,date}]。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "关键词，如 '美联储 议息' / '宁德时代 储能 订单' / '今日 涨停潮'"},
                    "limit": {"type": "integer", "default": 8},
                    "backend": {"type": "string",
                                "enum": ["auto", "baidu", "eastmoney"],
                                "default": "auto"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓取一个 URL 的正文。仅在 web_search 看到关键标题想读全文时使用，"
                "**整轮最多调 2 次**避免浪费时间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_quotes",
            "description": "获取观察池股票实时行情（带 30s 缓存）",
            "parameters": {
                "type": "object",
                "properties": {
                    "codes": {"type": "array", "items": {"type": "string"},
                              "description": "可选；不传则用默认观察池"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history_indicators",
            "description": "拿单只股票近 days 个交易日日线 + 计算后的技术指标(MA/动量/52w/波动)",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "days": {"type": "integer", "default": 500}
                },
                "required": ["code"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_news",
            "description": "读取最近 since_min 分钟内的新闻（由 news_worker 异步采集）",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 30},
                    "since_min": {"type": "integer", "default": 1440}
                }
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_trades",
            "description": ("查询你自己的历史买卖成交记录（决策观测的一环）。"
                            "返回成交明细 + 买/卖笔数、净买入额、累计手续费统计。"
                            "用于：复盘最近交易、避免追高刚买的票、控制换手率/交易成本。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 30},
                    "since_min": {"type": "integer",
                                  "description": "只看最近 N 分钟内成交；不传看全部"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "查询长期记忆（已发生的决策、成交、重要观察、事实等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "kinds": {"type": "array", "items": {"type": "string"},
                              "description": "news/decision/trade/observation/fact"},
                    "limit": {"type": "integer", "default": 20},
                    "min_importance": {"type": "integer", "default": 1}
                }
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "把当前的关键观察/事实写入长期记忆，下次决策可读到",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["news", "decision", "trade",
                                      "observation", "fact"]},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "importance": {"type": "integer", "default": 2,
                                   "description": "1-5，3 以上才会被默认 context 注入"},
                    "code": {"type": "string"}
                },
                "required": ["kind", "title", "content"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "查询当前账户现金、持仓、市值、浮盈",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_orders",
            "description": ("下单（模拟撮合，T+1）。可一次提交多笔。"
                            "side=BUY/SELL；target_weight 是该票占总资产的目标比例 0-1；"
                            "若 SELL 且 target_weight=0 则全部清仓。"
                            "约束：单票不超过 0.25。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "side": {"type": "string",
                                         "enum": ["BUY", "SELL"]},
                                "target_weight": {"type": "number"},
                                "reason": {"type": "string"}
                            },
                            "required": ["code", "side", "target_weight"]
                        }
                    }
                },
                "required": ["orders"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_overview",
            "description": "大盘指数实时点位（上证/深证/沪深300/创业板/科创50）+ 全市场涨跌家数",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_top",
            "description": "全市场榜单（拓宽视野，发现观察池外机会）",
            "parameters": {
                "type": "object",
                "properties": {
                    "sort": {"type": "string",
                             "enum": ["amount", "pct", "volume", "price"],
                             "default": "amount"},
                    "desc": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "default": 20},
                    "contains": {"type": "string",
                                 "description": "代码或名称模糊搜索"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_stocks",
            "description": ("按名称或代码片段搜索全 A 股 5500+ 只股票池。"
                            "例：'比亚迪'、'银行'、'光伏'、'隆基'、'宁德'。"
                            "用于发现观察池外的标的。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_strategy_state",
            "description": "读当前策略状态（上一轮的 view/themes/target_position）+ 最近 5 轮历史。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_strategy_state",
            "description": ("★每轮 CIO 必须调用一次，写入本轮的市场判断。"
                            "themes 是当前看好的 3-5 个主线（如 ['AI算力','创新药','出口链']）。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "view": {"type": "string",
                             "enum": ["bullish", "neutral", "bearish"]},
                    "themes": {"type": "array",
                               "items": {"type": "string"},
                               "description": "3-5 个主线名称"},
                    "target_position": {"type": "number",
                                          "description": "目标总仓位 0.0-1.0"},
                    "confidence": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": ["view", "themes", "target_position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_analysts",
            "description": ("★并行跑 4 个分析师（基本面/情绪/新闻/技术），"
                            "自动采集股吧/雪球/百度热度社交数据 + 行业资金流。"
                            "返回每位分析师的结构化观点，强烈建议 CIO 在 草拟订单前 调用一次。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "候选股票代码列表（不传则用观察池）"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "consult_financial_expert",
            "description": ("【保险/复审机制】把你拟下的订单草案 + 市场观点 提交给资深金融专家"
                            "（独立 LLM）评审，返回 verdict (APPROVE/MODIFY/REJECT)、"
                            "逐条意见、被你忽视的风险。"
                            "**place_orders 之前必须调用一次此工具**。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_orders": {
                        "type": "array",
                        "description": "你拟下的订单草案",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "side": {"type": "string",
                                         "enum": ["BUY", "SELL"]},
                                "target_weight": {"type": "number"},
                                "reason": {"type": "string"}
                            },
                            "required": ["code", "side", "target_weight"]
                        }
                    },
                    "cio_thesis": {"type": "string",
                                   "description": "你对当前的整体投资观点"},
                    "market_view": {"type": "string",
                                    "description": "你对大盘/风格/资金的判断"}
                },
                "required": ["draft_orders"]
            },
        },
    },
]

TOOL_DISPATCH = {
    "web_search": tool_web_search,
    "web_fetch": tool_web_fetch,
    "fetch_quotes": tool_fetch_quotes,
    "get_history_indicators": tool_get_history_indicators,
    "recent_news": tool_recent_news,
    "recent_trades": tool_recent_trades,
    "recall_memory": tool_recall_memory,
    "remember": tool_remember,
    "get_portfolio": tool_get_portfolio,
    "place_orders": tool_place_orders,
    "get_market_overview": tool_get_market_overview,
    "market_top": tool_market_top,
    "search_stocks": tool_search_stocks,
    "consult_financial_expert": tool_consult_financial_expert,
    "run_analysts": tool_run_analysts,
    "collect_social": tool_collect_social,
    "industry_pulse": tool_industry_pulse,
    "get_strategy_state": tool_get_strategy_state,
    "update_strategy_state": tool_update_strategy_state,
}


def dispatch(name: str, args: dict) -> dict:
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad args: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
