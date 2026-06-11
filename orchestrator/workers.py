"""定时工作者：
- news_worker     每 15 分钟：抓新闻 → cache + 让 LLM 标重要的写入 memory
- brain_worker    每 60 分钟：CIO 大脑用 tool-call 自主获取信息并下单
- quote_worker    每 5 分钟：刷一次行情快照（只入库，不 LLM）
"""
from __future__ import annotations
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    from ..core import market, db, llm, memory
    from ..core.config import WATCHLIST, get_model
    from ..core.tools import TOOL_SCHEMAS, dispatch
except ImportError:
    from core import market, db, llm, memory
    from core.config import WATCHLIST, get_model
    from core.tools import TOOL_SCHEMAS, dispatch


# ------------------ Quote (5min) ------------------

def quote_worker():
    """轻量：只刷快照、写库、推 equity 曲线。"""
    quotes = market.fetch_quotes(WATCHLIST)
    db.insert_snapshot(quotes)
    pf_total = 0.0
    pos = db.get_positions()
    cash = db.get_cash()
    qmap = {q["code"]: q for q in quotes}
    mv = 0.0
    for code, p in pos.items():
        px = qmap.get(code, {}).get("price") or p["avg_price"]
        mv += px * p["qty"]
    db.insert_equity(cash, mv)
    return {"quotes": len(quotes), "synthetic": any(q.get("synthetic") for q in quotes)}


# ------------------ News (15min) ------------------

NEWS_RANK_SYS = (
    "你是新闻分级 Agent。给每条新闻打 importance 1-5（5=可能直接驱动盘面/政策；"
    "1=噪音）。覆盖范围：A股个股公告/财经/全球宏观/地缘政治/全球时政等。"
    "返回 JSON：{items:[{id,importance:int,reason:short,affects:[code|sector]}]}。"
    "只输出 JSON，无其他文字。"
)


def news_worker():
    """抓多源财经新闻 -> 入 cache -> LLM 打分 -> 重要的写入 memory。
    Hot codes 包含：观察池 + 当前持仓 + 涨跌幅榜 top16，确保拿到行业/个股新闻。
    """
    tick_id = uuid.uuid4().hex[:8]

    # 选定要拉个股新闻的 hot codes
    hot_codes = list(WATCHLIST)
    try:
        for code in db.get_positions().keys():
            if code not in hot_codes:
                hot_codes.append(code)
    except Exception:
        pass
    try:
        market_rows = market.fetch_market_snapshot()
        if market_rows:
            asc = sorted(market_rows, key=lambda r: r.get("pct") or 0)
            for r in (asc[:8] + asc[-8:]):
                if r["code"] not in hot_codes:
                    hot_codes.append(r["code"])
    except Exception:
        pass

    items = market.fetch_news(limit=150, hot_codes=hot_codes)
    if not items:
        return {"fetched": 0}
    # 入 cache
    for it in items:
        db.cache_news(it["id"], it.get("source", ""), it["title"],
                      it.get("content", ""), it.get("url", ""))

    # LLM 排序
    user = "新闻列表（id|源|标题）：\n" + "\n".join(
        f"{i['id']} | {i.get('source','')} | {i['title']}" for i in items
    )
    db.log_agent(tick_id, "news_worker", "prompt", user[:1500])
    resp = llm.chat(
        NEWS_RANK_SYS,
        user,
        json_mode=True,
        max_tokens=2500,
        model=get_model("news_worker"),
    )
    raw_text = resp.get("text", "") or ""
    parsed = llm.parse_json(raw_text) or {}
    # 容错：JSON 被截断时手动正则解出 items（注意不能覆盖外层 items）
    if not parsed.get("items"):
        import re
        rank_items: list[dict] = []
        for m in re.finditer(
            r'\{[^{}]*?"id"\s*:\s*"([^"]+)"[^{}]*?"importance"\s*:\s*(\d+)[^{}]*?\}',
            raw_text,
        ):
            rank_items.append({"id": m.group(1), "importance": int(m.group(2))})
        if rank_items:
            parsed = {"items": rank_items}
    db.log_agent(tick_id, "news_worker", "raw", raw_text[:5000])

    rank_map = {}
    for it in (parsed.get("items") or []):
        if isinstance(it, dict) and it.get("id"):
            rank_map[str(it["id"])] = it

    saved = 0
    for it in items:
        rk = rank_map.get(it["id"], {})
        imp = int(rk.get("importance", 1) or 1)
        if imp >= 3:
            memory.remember(
                kind="news",
                title=it["title"],
                content=(f"[{it.get('source','')}] {it.get('content','')[:300]} "
                         f"affects={rk.get('affects')} reason={rk.get('reason','')}"),
                importance=imp,
                ttl_sec=7 * 86400,
            )
            saved += 1
    db.log_agent(tick_id, "news_worker", "summary",
                 f"fetched={len(items)} saved_imp3+={saved} ranked={len(rank_map)}")
    return {"fetched": len(items), "ranked": len(rank_map),
            "saved_to_memory": saved}


# ------------------ Brain (60min) ------------------

BRAIN_SYS_TMPL = """你是 CIO 大脑（首席投资官 + Orchestrator），管理一个 ¥100,000 A 股账户（T+1）。
你的目标：**根据全球新闻 + 中国宏观 + 行业资金流 + 技术面，自主判断当前市场主线，再选股下单**。

# 你的可用工具
  - get_portfolio              查看现金/持仓/浮盈
  - get_market_overview        大盘指数实时点位 + 全市场涨跌家数 + 涨停跌停数
  - fetch_quotes(codes)        任意股票实时行情
  - market_top(sort,desc,limit) 全市场涨幅榜 / 成交额榜
  - search_stocks(keyword)     按名称搜全 A 股（"光伏"/"AI"/"创新药"等）
  - get_history_indicators(code, days=500) 任意 A 股 500 日 K + 技术指标
  - recent_news(since_min)     **多源新闻**：财新+东财个股+全球宏观日历+异动+财联社全球电报+CCTV
  - **web_search(query)**      ★互联网实时搜索（东财+百度），比 recent_news 更新
  - **web_fetch(url)**         抓取 URL 正文（最多 2 次/轮）
  - recall_memory              读历史决策、成交、重要观察、事实（含每日 PnL 反思）
  - recent_trades              ★查自己最近的买卖成交（笔数/净买入/手续费），复盘+控制换手
  - remember                   把关键判断写入长期记忆
  - get_strategy_state         读当前策略状态（view / themes / target_position）
  - update_strategy_state      更新策略状态（每轮决策都应更新）
  - run_analysts               ★4 分析师并行（基本面/情绪/新闻/技术，各自会自己上网搜）
  - collect_social             股吧/雪球/百度热搜
  - industry_pulse             行业资金流
  - consult_financial_expert   ★金融专家复审（draft_orders 必须 ≥5 项）
  - place_orders               最终下单（任意 A 股代码）

# 核心原则（重要！）
1. **不要预设主线**：当前应该买什么板块/行业，**完全由你根据新闻+市场判断决定**。
   - 美联储降息 / 海外流动性宽松 → 出口 / 消费 / 地产
   - AI 大厂 capex 提升 → 算力 / CPO / 芯片
   - 反内卷 / 产能调控 → 周期 / 钢铁 / 化工
   - 创新药 BD 落地 → 医药生物
   - 国常会关注 → 地产链 / 公用事业 / 稳增长
   - 跟着事实走，不要持有"AI 永远最强"或"银行永远稳"这种偏见。
2. **每轮策略可以变**：strategy_state 让你跨轮保持连续；但当出现重大新催化时果断转向。
3. **持仓集中度**：8-15 只票；单票 5%-25%（高确信度可重仓 25%）。
4. **目标仓位**：看多 75%-90%；中性 55%-75%；看空 30%-55%。
   ★ **目标总仓 ≥ 65%**，不要长期低仓徘徊。现金留 10-25% 应急即可。
5. **保险机制**：place_orders 之前必须 consult_financial_expert（draft_orders ≥ 5 项）。
   专家网络 ERROR 时直接执行你自己的草案，不要空仓。

# 工作流
  1) recall_memory(kinds=["decision","trade","observation","fact"]) — 重点看 fact（每日反思）
  2) get_strategy_state — 看上一轮的方向是否要延续
  3) get_portfolio
  4) get_market_overview
  5) recent_news(since_min=1440) — **认真读 30 条新闻**，识别"全球+国内"的潜在主线
  6) **思考**：基于新闻和市场，本轮的 3-5 个潜在投资主线是什么？
  7) market_top × 2（涨幅榜、成交额榜）+ search_stocks 围绕主线搜 3-5 个关键词
  8) fetch_quotes(候选 ≥ 25 只)
  9) run_analysts(candidate_codes) — 4 分析师并行
 10) get_history_indicators × 10-15 看 K 线
 11) remember 关键观察（importance>=4）
 12) update_strategy_state(view=..., themes=[...], target_position=0.xx) — 必调
 13) 草拟订单 8-15 只 → consult_financial_expert(draft_orders, cio_thesis, market_view)
 14) 按专家意见调整 → place_orders（最终 8-15 只）
 15) thesis：本轮总结，必须包含：主线判断 + 依据新闻、行业分布、各票理由、专家结论

# 重要约束
- 必须 8-15 只票（除非全空仓且明确说明）
- 单票 5%-25%；总仓 ≥ 65%（不要低仓徘徊）
- consult_financial_expert 必须传具体 draft_orders（不能空）
- update_strategy_state 必须调用一次

观察池（仅作起点，请用 search_stocks/market_top 大幅扩展）：{watchlist}
当前北京时间：{now}
"""


def brain_worker(resume_tick_id: str | None = None) -> dict:
    """CIO 大脑：tool-call 多轮直到 finish_reason=stop。
    支持 checkpoint：传 resume_tick_id 可基于历史 checkpoint 续跑。
    """
    if resume_tick_id:
        last = db.get_last_checkpoint(resume_tick_id)
        if last:
            tick_id = resume_tick_id
            db.log_agent(tick_id, "cio_brain", "resume",
                         f"resumed from stage={last.get('stage')}")
        else:
            tick_id = uuid.uuid4().hex[:8]
    else:
        tick_id = uuid.uuid4().hex[:8]
    t0 = time.time()
    db.save_checkpoint(tick_id, "start")

    # 决策前注入"长期记忆摘要"作为额外的 system 提示
    mem_block = memory.context_block()
    sys = BRAIN_SYS_TMPL.format(
        watchlist=", ".join(WATCHLIST),
        now=time.strftime("%Y-%m-%d %H:%M %A"),
    )
    if mem_block:
        sys += "\n\n" + mem_block

    messages: list[dict] = [
        {"role": "system", "content": sys},
        {"role": "user",
         "content": (
             "开始本轮决策。流程严格按 system 中 1-11 步执行。"
             "**绝对要求**："
             "(a) 必须草拟 **8-15 只票**（不少于 8、不多于 15），单票 5%-15%；"
             "(b) **科技/AI/半导体/机器人 合计 ≥ 25%**，顺应当前主线；"
             "(c) 调 consult_financial_expert 时 draft_orders 必须是非空数组（>=8 项），"
             "    格式 [{code,side,target_weight,reason}, ...]，并附 cio_thesis、market_view；"
             "(d) **如果专家返回 ERROR（网络问题），采纳自己原方案直接 place_orders，"
             "    不要因专家不可用而完全空仓**；"
             "(e) 如果专家 APPROVE/MODIFY，按其建议调整后 place_orders；"
             "(f) 最终 place_orders 必须包含 **8-15 笔**（除非判断需全空仓且明确说明）。"
             "(g) **必须用 search_stocks 主动搜 'AI'/'半导体'/'机器人'/'CPO'/'算力' "
             "    至少 3 次**，从 5500 只全 A 股里挖候选，不要只盯观察池。"
         )},
    ]

    db.log_agent(tick_id, "cio_brain", "prompt",
                 messages[-1]["content"] + "\n[mem_len]=" + str(len(mem_block)))

    final_text = ""
    rounds = 0
    MAX_ROUNDS = 18

    while rounds < MAX_ROUNDS:
        rounds += 1
        resp = llm.chat(messages=messages, tools=TOOL_SCHEMAS,
                        json_mode=False, max_tokens=1200,
                        tool_choice="auto" if rounds < MAX_ROUNDS - 1 else "none")
        if not resp.get("ok"):
            db.log_agent(tick_id, "cio_brain", "error",
                         resp.get("error", "unknown"))
            db.save_checkpoint(tick_id, "error", str(resp.get("error", ""))[:500])
            break
        text = resp.get("text", "")
        tool_calls = resp.get("tool_calls", [])
        if text:
            final_text += text
        # checkpoint：每轮记录
        db.save_checkpoint(tick_id, f"round_{rounds}",
                            payload=f"tools={[tc['name'] for tc in tool_calls]}; text_len={len(text)}")
        # 把 assistant 的回复（含 tool_calls）追加到 messages
        assistant_msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": tc["arguments"] or "{}"},
                }
                for i, tc in enumerate(tool_calls)
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            break  # LLM 不再调工具就结束

        # 依次执行工具
        for i, tc in enumerate(tool_calls):
            name = tc["name"]
            try:
                args = json.loads(tc["arguments"] or "{}")
            except Exception:
                args = {}
            db.log_agent(tick_id, "cio_brain", "tool_call",
                         f"{name}({json.dumps(args, ensure_ascii=False)[:300]})")
            db.save_checkpoint(tick_id, f"tool_call:{name}",
                               json.dumps(args, ensure_ascii=False)[:500])
            result = dispatch(name, args)
            # 写日志
            db.log_agent(tick_id, "cio_brain", "tool_result",
                         f"{name} -> {json.dumps(result, ensure_ascii=False)[:500]}")
            db.save_checkpoint(tick_id, f"tool_result:{name}",
                               str(result)[:500])
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{i}",
                "name": name,
                "content": json.dumps(result, ensure_ascii=False)[:6000],
            })

    # 把最终 thesis 落库 + 写记忆
    if final_text:
        db.log_agent(tick_id, "cio_brain", "thesis", final_text[:4000])
        memory.remember(
            kind="decision",
            title=f"CIO 决策 {time.strftime('%m-%d %H:%M')}",
            content=final_text[:1500],
            importance=3,
            ttl_sec=14 * 86400,
        )
    # ★ 修复 2：CIO 跑完后仍 0 笔成交 → 兜底规则下单（防止"白跑一轮"）
    fallback_used = False
    fallback_orders: list[dict] = []
    today_start = int(time.time()) - 6 * 3600  # 本轮开始前 6h 内
    with db.conn() as c:
        n_orders_today = c.execute(
            "SELECT COUNT(*) FROM orders WHERE ts>=?", (today_start,)
        ).fetchone()[0]
    if n_orders_today == 0 and not db.get_positions():
        # 没有任何持仓 + 本轮没下单 → 兜底
        try:
            from ..core import market as _market
            from ..core import broker as _broker
            # 从观察池前 18 只科技/AI 票里选 8 只价格合适的
            candidates = WATCHLIST[:18]
            quotes = {q["code"]: q for q in _market.fetch_quotes(candidates)}
            picked = []
            for code in candidates:
                q = quotes.get(code)
                if not q or not q.get("price"):
                    continue
                price = q["price"]
                # 单票预算 6%（约 ¥6000），价格 < ¥60 才能买够 100 股
                if price < 65:
                    picked.append((code, price))
                if len(picked) >= 8:
                    break
            if len(picked) < 8:
                # 价格筛得太严，放宽到 ¥150
                for code in candidates:
                    if any(p[0] == code for p in picked):
                        continue
                    q = quotes.get(code)
                    if q and q.get("price") and q["price"] < 150:
                        picked.append((code, q["price"]))
                    if len(picked) >= 8:
                        break

            if len(picked) >= 5:
                cash = db.get_cash()
                per = (cash * 0.80) / len(picked)  # 总仓 80%，平均分散
                for code, price in picked:
                    qty = int(per / price / 100) * 100
                    if qty < 100:
                        continue
                    r = _broker.execute(
                        code, "BUY", qty, price,
                        reason="brain_fallback：CIO 未能下单/网络故障，规则兜底科技分散",
                    )
                    if r.get("ok"):
                        fallback_orders.append(r)
                if fallback_orders:
                    fallback_used = True
                    db.log_agent(tick_id, "cio_brain", "fallback",
                                 f"CIO 未下单，已兜底买入 {len(fallback_orders)} 只科技股")
                    memory.remember(
                        kind="trade",
                        title=f"⚠️ 兜底交易 {len(fallback_orders)} 笔",
                        content=f"CIO 决策异常或专家持续报错，规则自动买入科技分散组合：{[o['code'] for o in fallback_orders]}",
                        importance=4,
                        ttl_sec=30 * 86400,
                    )
        except Exception as e:
            db.log_agent(tick_id, "cio_brain", "fallback_error", f"{type(e).__name__}: {e}")

    elapsed = round(time.time() - t0, 2)
    db.save_checkpoint(tick_id, "done", f"elapsed={elapsed}s rounds={rounds} fb={fallback_used}")
    db.log_agent(tick_id, "cio_brain", "summary",
                 f"rounds={rounds} elapsed={elapsed}s fallback={fallback_used}")
    return {"tick_id": tick_id, "rounds": rounds, "elapsed_sec": elapsed,
            "thesis": final_text,
            "fallback_used": fallback_used,
            "fallback_orders": fallback_orders}
