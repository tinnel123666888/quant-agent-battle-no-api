"""互联网搜索工具：给分析师 LLM 调用，免 key、中文财经优先。

后端：
  1) 百度网页搜索 (www.baidu.com/s)        — 中文综合
  2) 东方财富全文搜索 (so.eastmoney.com)   — 财经垂直
  3) (可选) 抓取单页正文 web_fetch         — 给到 LLM 看具体新闻

设计原则：
  - 不需要 API key，直接抓 HTML，用正则/简单 DOM 解析
  - 失败静默：搜不到 → 返回空列表，让 LLM 走 fallback
  - 限速：单次最多 10 条结果，避免抓爆
  - 缓存：5 分钟内重复 query 直接复用，省网络
"""
from __future__ import annotations
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from html import unescape

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# {(backend, query): (ts, results)}
_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_TTL = 300  # 5 min


def _http_get(url: str, timeout: int = 8, headers: dict | None = None) -> str:
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            # 多种编码兜底
            for enc in ("utf-8", "gbk", "gb2312"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ============ 百度搜索 ============

def search_baidu(query: str, limit: int = 10) -> list[dict]:
    """百度网页搜索。返回 [{title, url, snippet, source}]。"""
    url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}&rn={limit}"
    html = _http_get(url, headers={"Referer": "https://www.baidu.com/"})
    if not html:
        return []
    results: list[dict] = []
    # 百度结果块通常是 <div class="result"> 或 <div class="c-container">
    blocks = re.findall(
        r'<div[^>]*class="[^"]*(?:result|c-container)[^"]*"[^>]*>([\s\S]*?)</div>\s*</div>',
        html,
    )
    for blk in blocks[:limit * 2]:
        # 标题 + URL
        m_title = re.search(
            r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*"[^>]*>([\s\S]*?)</a>',
            blk,
        )
        if not m_title:
            m_title = re.search(r'<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', blk)
        if not m_title:
            continue
        href = m_title.group(1)
        title = _strip_html(m_title.group(2))
        if not title or len(title) < 3:
            continue
        # 摘要
        m_snip = re.search(
            r'<span[^>]*class="[^"]*content-right[^"]*"[^>]*>([\s\S]*?)</span>', blk
        )
        if not m_snip:
            m_snip = re.search(
                r'<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>([\s\S]*?)</div>', blk
            )
        snippet = _strip_html(m_snip.group(1)) if m_snip else ""
        results.append({
            "title": title[:120],
            "url": href,
            "snippet": snippet[:300],
            "source": "baidu",
        })
        if len(results) >= limit:
            break
    return results


# ============ 东方财富搜索 ============

def search_eastmoney(query: str, limit: int = 10) -> list[dict]:
    """东财搜索（财经新闻为主）。
    使用 so.eastmoney.com 全文搜索 API（已知公开 endpoint）。
    """
    # 东财搜索 v3 API（公开）
    url = (
        "https://search-api-web.eastmoney.com/search/jsonp"
        "?cb=jQuery&param=" + urllib.parse.quote(
            '{"uid":"","keyword":"' + query.replace('"', '') + '",'
            '"type":["cmsArticleWebOld"],"client":"web","clientType":"web",'
            '"clientVersion":"curr","param":{"cmsArticleWebOld":'
            '{"searchScope":"default","sort":"default","pageIndex":1,'
            '"pageSize":' + str(limit) + ',"preTag":"<em>","postTag":"</em>"}}}'
        )
    )
    txt = _http_get(url, timeout=8, headers={"Referer": "https://so.eastmoney.com/"})
    if not txt:
        return []
    # 剥 jsonp 外壳
    m = re.search(r"jQuery\s*\(([\s\S]+)\)\s*;?\s*$", txt)
    if not m:
        m = re.search(r"\(\s*(\{[\s\S]+\})\s*\)\s*;?\s*$", txt)
    if not m:
        return []
    try:
        import json as _json
        data = _json.loads(m.group(1))
        items = (
            data.get("result", {})
            .get("cmsArticleWebOld", []) or []
        )
    except Exception:
        return []
    results: list[dict] = []
    for it in items[:limit]:
        title = _strip_html(it.get("title", ""))
        if not title:
            continue
        results.append({
            "title": title[:120],
            "url": it.get("url", ""),
            "snippet": _strip_html(it.get("content", ""))[:300],
            "source": "eastmoney",
            "date": it.get("date", "")[:10],
        })
    return results


# ============ 统一入口 ============

def web_search(query: str, limit: int = 10,
                backend: str = "auto") -> dict:
    """统一搜索入口。
    backend: auto | baidu | eastmoney
      auto = 财经类问题先走东财，再补百度
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "msg": "empty query", "results": []}

    cache_key = (backend, query)
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _TTL:
        return {"ok": True, "from": "cache", "query": query,
                "results": cached[1][:limit]}

    results: list[dict] = []
    if backend in ("auto", "eastmoney"):
        try:
            results.extend(search_eastmoney(query, limit=limit))
        except Exception:
            pass
    if backend in ("auto", "baidu") and len(results) < limit:
        try:
            need = max(5, limit - len(results))
            results.extend(search_baidu(query, limit=need))
        except Exception:
            pass

    # 去重（按 title 前 30 字）
    seen = set()
    dedup = []
    for r in results:
        key = (r.get("title") or "")[:30]
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)

    _CACHE[cache_key] = (time.time(), dedup)
    return {"ok": True, "query": query, "backend": backend,
            "count": len(dedup), "results": dedup[:limit]}


def web_fetch(url: str, max_chars: int = 3000) -> dict:
    """抓取单个 URL 的正文（粗剥 HTML）。失败返回 ok=False。"""
    if not url or not url.startswith(("http://", "https://")):
        return {"ok": False, "msg": "invalid url"}
    html = _http_get(url, timeout=10)
    if not html:
        return {"ok": False, "msg": "fetch failed", "url": url}
    # 取 <title>
    m_title = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.IGNORECASE)
    title = _strip_html(m_title.group(1)) if m_title else ""
    # 去 script/style
    body = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.IGNORECASE)
    # 提取所有 <p> / <article> 文本
    ps = re.findall(r"<(?:p|article|div)[^>]*>([\s\S]*?)</(?:p|article|div)>", body)
    text_parts = [_strip_html(p) for p in ps]
    text_parts = [t for t in text_parts if len(t) > 20]
    text = "\n".join(text_parts)[:max_chars]
    if not text:
        text = _strip_html(body)[:max_chars]
    return {"ok": True, "url": url, "title": title[:200], "content": text}
