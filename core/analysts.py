"""4 个并行分析师 + 数据采集 Agent。

Researcher（数据收集）：
  - SocialResearcher  抓股吧/雪球/百度热搜/讨论/调研
  - WebResearcher     wraps web search api（保留接口；当前用 akshare 概念热度替代）

Analyst（4 个并行）：
  - FundamentalsAnalyst   基本面（PE/PB/ROE/财报）— LLM + web_search
  - SentimentAnalyst       情绪（社交热度/排名/讨论）— LLM + web_search
  - NewsAnalyst            新闻面（recent_news + 个股相关）— LLM + web_search
  - TechnicalAnalyst       技术面（MA/动量/52w/波动率）— LLM + 量化指标（不联网）

输出：每个分析师返回结构化 JSON，最终给 CIO 综合。

★ 2026-06 升级：分析师改用 tool-call 协议，可主动 web_search 上网搜增量信息，
   akshare 数据作为打底 context，互补不替代。
"""
from __future__ import annotations
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import market, db, llm, memory, web_search
from .config import get_model


# ============ 给分析师用的小工具集（独立 schema，不污染 CIO 工具池） ============

ANALYST_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "互联网搜索（中文财经优先，自动聚合东财+百度）。"
                "用于：宏观政策、行业新闻、个股最新公告、资金动向、机构观点等。"
                "返回 [{title,url,snippet,source}]。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "搜索关键词，例如 '宁德时代 2026 储能订单' / "
                                              "'美联储 12月 议息会议 鲍威尔'"},
                    "limit": {"type": "integer", "default": 8,
                              "description": "返回结果数 (1-10)"},
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
                "抓取一个 URL 的正文（HTML 剥壳）。"
                "用于：你在 web_search 看到一条很关键的标题，想读全文再判断时调用。"
                "**最多调 2 次**，避免浪费时间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
]


def _analyst_tool_dispatch(name: str, args: dict) -> dict:
    """分析师专用 tool 路由（独立于 CIO 的 tools.py 路由）。"""
    if name == "web_search":
        return web_search.web_search(
            query=args.get("query", ""),
            limit=int(args.get("limit", 8) or 8),
            backend=str(args.get("backend", "auto")),
        )
    if name == "web_fetch":
        return web_search.web_fetch(url=args.get("url", ""))
    return {"error": f"unknown tool: {name}"}


# ============ 数据采集器 ============

def collect_social_data(codes: list[str]) -> dict:
    """聚合股吧 / 雪球 / 百度热度 / 概念关键词。每只票限速。"""
    out: dict = {"hot_rank_em": [], "hot_search_baidu": [],
                 "xq_follow": [], "xq_tweet": [], "concept": {}}
    try:
        import akshare as ak
        # ① 东财热度榜（人气股）
        try:
            df = ak.stock_hot_rank_em()
            for _, r in df.head(30).iterrows():
                out["hot_rank_em"].append({
                    "rank": int(r.get("当前排名", 0) or 0),
                    "code": str(r.get("代码", "")),
                    "name": str(r.get("股票名称", "")),
                    "price": float(r.get("最新价", 0) or 0),
                })
        except Exception:
            pass

        # ② 百度热搜
        try:
            from datetime import datetime
            df = ak.stock_hot_search_baidu(
                symbol="A股", date=datetime.now().strftime("%Y%m%d"), time="今日"
            )
            for _, r in df.head(15).iterrows():
                out["hot_search_baidu"].append({
                    "name": str(r.get("名称/代码", "")),
                    "pct": str(r.get("涨跌幅", "")),
                    "heat": str(r.get("综合热度", "")),
                })
        except Exception:
            pass

        # ③ 概念热度（每只持仓 / hot 票）
        for code in codes[:8]:
            try:
                df = ak.stock_hot_keyword_em(symbol=f"SH{code}" if code.startswith("6") else f"SZ{code}")
                if len(df):
                    out["concept"][code] = [
                        {"concept": str(r.get("概念名称", "")),
                         "heat": str(r.get("热度", ""))}
                        for _, r in df.head(5).iterrows()
                    ]
            except Exception:
                continue
    except Exception:
        pass
    return out


def collect_industry_pulse() -> dict:
    """行业资金流 + 板块涨跌（拓宽视野）"""
    out: dict = {"sector_flow": [], "concept_top": []}
    try:
        import akshare as ak
        # 行业资金流向
        try:
            df = ak.stock_sector_fund_flow_rank(indicator="今日")
            for _, r in df.head(15).iterrows():
                out["sector_flow"].append({
                    "name": str(r.get("名称", "")),
                    "pct": float(r.get("今日涨跌幅", 0) or 0),
                    "flow_main": float(r.get("今日主力净流入-净额", 0) or 0),
                })
        except Exception:
            pass
    except Exception:
        pass
    return out


# ============ 4 个分析师 ============

ANALYST_SYS = {
    "fundamentals": (
        "你是基本面分析师。基于公司行情/估值/财务通识 + 你自己 web_search 查到的最新公告/财报/机构评级，"
        "对每只候选股给一个 score (0-1) 和 verdict (bullish|neutral|bearish)。"
        "**强烈建议**：对 3-5 只你不熟的票，用 web_search 查 '<股票名> 最新业绩 / 公告' 拿到事实依据。"
        "**最多调 web_search 6 次**，搜完直接出 JSON，不要无限搜。"
        "最终输出 JSON：{view:'整体一句话', stocks:{code:{score,verdict,reason}}}"
    ),
    "sentiment": (
        "你是市场情绪分析师。基于股吧/雪球/百度热搜数据 + 你自己 web_search 查到的最新讨论热点，"
        "判断短期情绪。"
        "**建议**：用 web_search 查 '今日 A股 龙头 / 涨停潮 / 主力资金' 或具体板块情绪。"
        "**最多调 web_search 5 次**。"
        "输出 JSON：{mood:'fear|neutral|greed', score:-1~1, "
        "hot:[code...], cold:[code...], note:'一句解读'}"
    ),
    "news": (
        "你是新闻分析师。基于近 24h 多源新闻（akshare 已注入）+ 你自己 web_search 查到的实时新闻，"
        "+ 行业资金流，判断哪些标的有正面/负面催化。"
        "**强烈建议**：用 web_search 实时查 '今日 A股 利好' / '美股 隔夜' / '<板块> 政策'，"
        "akshare 的新闻可能已经过时，互联网最新信息更重要。"
        "**最多调 web_search 8 次** + web_fetch 2 次（看具体长文）。"
        "输出 JSON：{macro_view:'宏观一句话', "
        "bullish_themes:[...], bearish_themes:[...], "
        "stocks:{code:{verdict,reason}}}"
    ),
    "technical": (
        "你是技术分析师。基于 MA5/20/60/120、动量(5d/20d/60d/240d)、"
        "52w高低点和波动率（指标已注入），对每只候选给评级。"
        "技术面以量化指标为主，**不需要 web_search**，直接基于注入的 indicators 出结论。"
        "输出 JSON：{regime:'trending_up|range|trending_down', "
        "stocks:{code:{rating:'strong_buy|buy|hold|sell|strong_sell',"
        "score:0-1,reason}}}"
    ),
}


# 哪些分析师可以用 web_search/web_fetch
ANALYST_USE_TOOLS = {"fundamentals", "sentiment", "news"}
MAX_TOOL_ROUNDS = 6  # 单个分析师最多 6 轮 tool-call


def _ask_analyst(name: str, payload: dict, model: str) -> dict:
    """分析师调用：news/sentiment/fundamentals 走 tool-call 多轮，technical 走单次 JSON。"""
    sys = ANALYST_SYS[name]
    user = json.dumps(payload, ensure_ascii=False, default=str)[:6000]

    # ----- technical：单次 JSON（量化数据足够，不需要联网） -----
    if name not in ANALYST_USE_TOOLS:
        resp = llm.chat(sys, user, json_mode=True, max_tokens=1500, model=model)
        if not resp.get("ok"):
            return {"_error": resp.get("error")}
        parsed = (llm.parse_json(resp.get("text", ""))
                  or {"_raw": resp.get("text", "")[:500]})
        db.log_agent("analysts", f"analyst_{name}", "raw",
                     resp.get("text", "")[:2000])
        db.log_agent("analysts", f"analyst_{name}", "parsed",
                     json.dumps(parsed, ensure_ascii=False)[:3000])
        return parsed

    # ----- news/sentiment/fundamentals：tool-call 多轮 -----
    messages: list[dict] = [
        {"role": "system", "content": sys},
        {"role": "user", "content":
            user + "\n\n请按需调用 web_search 补充实时信息，最后输出严格 JSON。"},
    ]
    rounds = 0
    final_text = ""
    tool_call_count = 0

    while rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        # 倒数第二轮强制收口出 JSON
        force_json = rounds >= MAX_TOOL_ROUNDS - 1
        resp = llm.chat(
            messages=messages,
            tools=ANALYST_TOOL_SCHEMAS,
            json_mode=False,
            max_tokens=1500,
            model=model,
            tool_choice="none" if force_json else "auto",
        )
        if not resp.get("ok"):
            db.log_agent("analysts", f"analyst_{name}", "llm_error",
                         resp.get("error", "unknown")[:300])
            return {"_error": resp.get("error")}

        text = resp.get("text", "") or ""
        tool_calls = resp.get("tool_calls", []) or []
        if text:
            final_text = text  # 始终保留最新一次的文字输出

        # assistant 回复入栈
        asst = {"role": "assistant", "content": text or None}
        if tool_calls:
            asst["tool_calls"] = [
                {"id": tc["id"] or f"call_{i}",
                 "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": tc["arguments"] or "{}"}}
                for i, tc in enumerate(tool_calls)
            ]
        messages.append(asst)

        if not tool_calls:
            break  # 没工具调用 = 收尾

        # 执行工具
        for i, tc in enumerate(tool_calls):
            tool_call_count += 1
            try:
                args = json.loads(tc["arguments"] or "{}")
            except Exception:
                args = {}
            db.log_agent("analysts", f"analyst_{name}", "tool_call",
                         f"{tc['name']}({json.dumps(args, ensure_ascii=False)[:200]})")
            result = _analyst_tool_dispatch(tc["name"], args)
            # 结果裁剪到 3000 字，避免上下文爆炸
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{i}",
                "name": tc["name"],
                "content": json.dumps(result, ensure_ascii=False)[:3000],
            })

    # 收尾解析
    parsed = llm.parse_json(final_text) or {"_raw": final_text[:500]}
    db.log_agent("analysts", f"analyst_{name}", "raw", final_text[:2000])
    db.log_agent("analysts", f"analyst_{name}", "parsed",
                 json.dumps(parsed, ensure_ascii=False)[:3000])
    db.log_agent("analysts", f"analyst_{name}", "summary",
                 f"rounds={rounds} tool_calls={tool_call_count}")
    return parsed



def run_analysts(candidate_codes: list[str]) -> dict:
    """并行运行 4 个分析师。"""
    if not candidate_codes:
        candidate_codes = []

    # 数据准备
    quotes = market.fetch_quotes(candidate_codes)
    quote_map = {q["code"]: q for q in quotes}
    code_names = market._load_code_names() if hasattr(market, "_load_code_names") else {}

    # 技术指标
    indicators: dict[str, dict] = {}
    for code in candidate_codes[:15]:
        try:
            hist = market.fetch_history(code, days=500)
            ind = market.compute_indicators(hist)
            if ind:
                indicators[code] = ind
        except Exception:
            continue

    # 基本面：行情 + 名称（可让 LLM 凭知识判断估值）
    fundamental_in = {
        "candidates": [{"code": c, "name": code_names.get(c, c),
                        "price": quote_map.get(c, {}).get("price"),
                        "pct": quote_map.get(c, {}).get("pct")}
                        for c in candidate_codes[:15]]
    }

    # 情绪：社交热度
    social = collect_social_data(candidate_codes[:8])

    # 新闻：最近 24h
    since = int(time.time()) - 86400
    news = db.recent_news(limit=50, since_ts=since)
    industry = collect_industry_pulse()

    # 技术
    tech_in = {"indicators": indicators,
               "candidates": [{"code": c, "name": code_names.get(c, c)}
                              for c in candidate_codes[:15]]}

    # 并行调用（tool-call 多轮 + web_search，timeout 给到 240s）
    results = {}
    fundamentals_model = get_model("fundamentals_analyst")
    sentiment_model = get_model("sentiment_analyst")
    news_model = get_model("news_analyst")
    technical_model = get_model("technical_analyst")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            "fundamentals": ex.submit(_ask_analyst, "fundamentals", fundamental_in, fundamentals_model),
            "sentiment": ex.submit(_ask_analyst, "sentiment",
                                    {"social": social, "candidates": candidate_codes[:15]},
                                    sentiment_model),
            "news": ex.submit(_ask_analyst, "news",
                              {"news": news[:30], "industry": industry,
                               "candidates": candidate_codes[:15]},
                              news_model),
            "technical": ex.submit(_ask_analyst, "technical", tech_in, technical_model),
        }
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=240)
            except Exception as e:
                results[name] = {"_error": f"{type(e).__name__}: {e}"}

    # 写入长期记忆（每个分析师的核心观点）
    for name, r in results.items():
        if r.get("_error"):
            continue
        memory.remember(
            kind="observation",
            title=f"分析师 {name} 综述",
            content=str(r)[:500],
            importance=3,
            ttl_sec=2 * 86400,
        )
    return {"results": results, "social_data": social,
            "industry": industry, "news_count": len(news)}
