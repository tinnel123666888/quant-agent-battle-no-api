"""长期记忆：写入/读取/格式化为 prompt 片段。

存储字段（见 db.SCHEMA -> memory）：
  ts, kind, importance, code, title, content, expires_at

kind 约定：
  - news        被新闻 Agent 标记为重要的新闻
  - decision    CIO 每次最终决策摘要
  - trade       已成交订单
  - observation 行情/技术/基本面层 Agent 提炼的"模式"或"异动"
  - fact        长期事实（公司战略、政策窗口、季节性等）
"""
from __future__ import annotations
import time
from . import db


def remember(kind: str, title: str, content: str,
             importance: int = 1, code: str | None = None,
             ttl_sec: int | None = None):
    expires_at = int(time.time()) + ttl_sec if ttl_sec else None
    db.write_memory(kind=kind, title=title, content=content,
                    importance=importance, code=code, expires_at=expires_at)


def recall(kinds: list[str] | None = None,
           limit: int = 30,
           min_importance: int = 1,
           since_ts: int | None = None) -> list[dict]:
    return db.query_memory(kinds=kinds, limit=limit,
                           min_importance=min_importance, since_ts=since_ts)


def format_for_prompt(items: list[dict], header: str = "记忆要点") -> str:
    if not items:
        return ""
    lines = [f"=== {header} ==="]
    for m in items:
        ts = time.strftime("%m-%d %H:%M", time.localtime(m["ts"]))
        tag = f"[{m['kind']}|★{m['importance']}]"
        code = f"({m['code']})" if m.get("code") else ""
        lines.append(f"- {ts} {tag}{code} {m['title']}: {m['content'][:300]}")
    return "\n".join(lines)


def context_block(limit_per_kind: int = 8) -> str:
    """决策前注入的"长期 + 短期"记忆混合摘要。"""
    parts: list[str] = []

    decisions = recall(kinds=["decision", "trade"], limit=limit_per_kind, min_importance=1)
    if decisions:
        parts.append(format_for_prompt(decisions, header="近期决策与成交"))

    facts = recall(kinds=["fact", "observation"], limit=limit_per_kind, min_importance=2)
    if facts:
        parts.append(format_for_prompt(facts, header="重要观察与事实"))

    # 24h 内的高重要新闻
    recent_news = recall(kinds=["news"], limit=limit_per_kind,
                         min_importance=3,
                         since_ts=int(time.time()) - 24 * 3600)
    if recent_news:
        parts.append(format_for_prompt(recent_news, header="24小时内关键新闻"))

    return "\n\n".join(parts)
