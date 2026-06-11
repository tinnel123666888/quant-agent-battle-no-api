"""市场数据：akshare 实盘行情 + 近 2 年日线，带本地缓存。

注意：A 股网盘代理可能拦截行情接口；akshare 调用失败时回退合成行情。
"""
from __future__ import annotations
import time
import random
import math
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
import json

from .config import DATA_DIR

try:
    import akshare as ak  # type: ignore
    HAVE_AK = True
except Exception:
    HAVE_AK = False


# -------- trading session --------

def is_trading_now() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")


# -------- 现价（带 30s 缓存） --------

_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTE_TTL = 30  # 秒


def _synth_state_path() -> Path:
    return DATA_DIR / "synth_state.json"


def _load_synth() -> dict:
    p = _synth_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_synth(d: dict):
    try:
        _synth_state_path().write_text(json.dumps(d))
    except Exception:
        pass


def _synth_quote(code: str, base: float | None = None) -> dict:
    state = _load_synth()
    # 优先用已记录的合成态 → 传入 base → 真实日线最后收盘 → 兜底 hash
    anchor = state.get(code) or base
    if anchor is None:
        # ★ 尽量锚定真实历史收盘价，避免 hash() 编出离谱价格（10x bug 根因）
        try:
            hist = fetch_history(code, days=5)
            if hist:
                anchor = float(hist[-1].get("close") or 0) or None
        except Exception:
            anchor = None
    if anchor is None:
        anchor = 10 + (hash(code) % 5000) / 10.0
    px = anchor
    drift = random.gauss(0, 0.004)
    px = max(1.0, px * (1 + drift))
    state[code] = px
    _save_synth(state)
    return {
        "ts": int(time.time()),
        "code": code,
        "price": round(px, 2),
        "open": round(px * 0.998, 2),
        "high": round(px * 1.005, 2),
        "low": round(px * 0.995, 2),
        "volume": random.randint(100_000, 5_000_000),
        "pct": round(drift * 100, 3),
        "synthetic": True,
    }


def _sina_prefix(code: str) -> str:
    if code.startswith(("60", "68", "9")):
        return "sh"
    if code.startswith(("8", "4")):
        return "bj"
    return "sz"


def _fetch_via_sina_hq(codes: list[str]) -> list[dict]:
    """直连 http://hq.sinajs.cn —— 实测在沙箱里稳定且快。
    一次最多 ~80 只；超过分批。
    """
    out: list[dict] = []
    BATCH = 60
    now_ts = int(time.time())
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        sym_list = ",".join(f"{_sina_prefix(c)}{c}" for c in chunk)
        url = f"http://hq.sinajs.cn/list={sym_list}"
        req = urllib.request.Request(
            url,
            headers={"Referer": "http://finance.sina.com.cn",
                     "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("gbk", errors="replace")
        except Exception:
            continue
        for line in raw.split(";"):
            line = line.strip()
            if not line.startswith("var hq_str_"):
                continue
            try:
                head, body = line.split("=", 1)
                code = head.replace("var hq_str_", "")[2:].strip()  # 去掉 sh/sz/bj
                payload = body.strip().strip('"')
                if not payload:
                    continue
                fields = payload.split(",")
                # 索引：0 名称 1 今开 2 昨收 3 现价 4 最高 5 最低 8 成交量 9 成交额 30/31 日期/时间
                if len(fields) < 10:
                    continue
                try:
                    open_p = float(fields[1] or 0)
                    prev_close = float(fields[2] or 0)
                    price = float(fields[3] or 0)
                    high = float(fields[4] or 0)
                    low = float(fields[5] or 0)
                    volume = float(fields[8] or 0)
                except Exception:
                    continue
                if price <= 0:
                    continue
                pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
                out.append({
                    "ts": now_ts, "code": code, "price": price,
                    "open": open_p or None, "high": high or None,
                    "low": low or None, "volume": volume or None,
                    "pct": round(pct, 3), "synthetic": False,
                })
            except Exception:
                continue
    return out


def fetch_quotes(codes: list[str]) -> list[dict]:
    """返回每只股票实时行情。优先 hq.sinajs.cn（稳定）；失败再试 ak；最后合成。"""
    now = time.time()
    out: list[dict] = []
    pending: list[str] = []
    for code in codes:
        cached = _QUOTE_CACHE.get(code)
        if cached and now - cached[0] < _QUOTE_TTL:
            out.append(cached[1])
        else:
            pending.append(code)
    if not pending:
        return out

    # ① hq.sinajs.cn（最快，直连）
    fetched = _fetch_via_sina_hq(pending)
    for q in fetched:
        _QUOTE_CACHE[q["code"]] = (now, q)
        out.append(q)
    got = {q["code"] for q in fetched}
    still_pending = [c for c in pending if c not in got]

    # ② akshare fallback
    if still_pending and HAVE_AK:
        for fn_name in ("stock_zh_a_spot", "stock_zh_a_spot_em"):
            try:
                fn = getattr(ak, fn_name)
                df = fn()
                df = df.copy()
                df["__c"] = df["代码"].astype(str).str.replace(
                    r"^(sh|sz|bj)", "", regex=True
                )
                sub = df[df["__c"].isin(still_pending)]
                ts = int(now)
                for _, r in sub.iterrows():
                    try:
                        price = float(r.get("最新价") or 0)
                    except Exception:
                        continue
                    if price <= 0:
                        continue
                    q = {
                        "ts": ts, "code": str(r["__c"]),
                        "price": price,
                        "open": float(r.get("今开", 0) or 0) or None,
                        "high": float(r.get("最高", 0) or 0) or None,
                        "low": float(r.get("最低", 0) or 0) or None,
                        "volume": float(r.get("成交量", 0) or 0) or None,
                        "pct": float(r.get("涨跌幅", 0) or 0),
                        "synthetic": False,
                    }
                    _QUOTE_CACHE[q["code"]] = (now, q)
                    out.append(q)
                got = {q["code"] for q in out}
                still_pending = [c for c in pending if c not in got]
                if not still_pending:
                    break
            except Exception:
                continue

    # ③ 合成兜底
    for c in still_pending:
        q = _synth_quote(c)
        _QUOTE_CACHE[c] = (now, q)
        out.append(q)
    return out


# -------- 全市场快照（涨跌排行榜） --------

_MARKET_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MARKET_TTL = 60  # 秒
_CODE_NAME_CACHE: dict[str, str] = {}


def _load_code_names() -> dict[str, str]:
    """全 A 股代码↔名称映射；akshare stock_info_a_code_name() 在沙箱可用。"""
    if _CODE_NAME_CACHE:
        return _CODE_NAME_CACHE
    cache_path = DATA_DIR / "code_names.json"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 7 * 86400:
        try:
            _CODE_NAME_CACHE.update(json.loads(cache_path.read_text(encoding="utf-8")))
            return _CODE_NAME_CACHE
        except Exception:
            pass
    if HAVE_AK:
        try:
            df = ak.stock_info_a_code_name()
            for _, r in df.iterrows():
                _CODE_NAME_CACHE[str(r["code"])] = str(r["name"])
            cache_path.write_text(
                json.dumps(_CODE_NAME_CACHE, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
    return _CODE_NAME_CACHE


def fetch_market_snapshot() -> list[dict]:
    """返回全市场快照（A股 ~5500 只）。
    策略：用 akshare stock_info_a_code_name() 拿全代码列表，
    再用 hq.sinajs.cn 分批拉行情（沙箱里这个直连最稳）。
    一次大约需 30-40 秒。
    """
    cached = _MARKET_CACHE.get("all")
    if cached and time.time() - cached[0] < _MARKET_TTL:
        return cached[1]

    code_names = _load_code_names()
    if not code_names:
        return []

    # 过滤北交所 4/8 开头的（部分接口不支持），但保留沪深所有 A 股
    codes = [c for c in code_names if c.startswith(("60", "00", "30", "68"))]

    rows = _fetch_via_sina_hq(codes)  # 内部分批 60 只一组

    # 关联名称
    out: list[dict] = []
    for q in rows:
        out.append({
            "ts": q["ts"], "code": q["code"],
            "name": code_names.get(q["code"], ""),
            "price": q["price"],
            "pct": q.get("pct", 0),
            "volume": q.get("volume") or 0,
            "amount": (q.get("volume") or 0) * (q.get("price") or 0),  # 近似成交额
            "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
            "synthetic": False,
        })
    if out:
        _MARKET_CACHE["all"] = (time.time(), out)
    return out


# -------- 近 2 年日线（磁盘缓存） --------

def _hist_cache_path(code: str) -> Path:
    return DATA_DIR / f"hist_{code}.json"


def fetch_history(code: str, days: int = 500) -> list[dict]:
    """近两年（默认 500 个交易日）日线，文件缓存 24h。"""
    p = _hist_cache_path(code)
    if p.exists() and (time.time() - p.stat().st_mtime) < 86_400:
        try:
            return json.loads(p.read_text())[-days:]
        except Exception:
            pass

    if HAVE_AK:
        # 优先东财 stock_zh_a_hist；失败用 stock_zh_a_daily（新浪 sh/sz 前缀）
        rows: list[dict] = []
        try:
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                    start_date=start, end_date=end, adjust="qfq")
            for _, r in df.iterrows():
                rows.append({
                    "date": str(r["日期"]),
                    "open": float(r["开盘"]),
                    "close": float(r["收盘"]),
                    "high": float(r["最高"]),
                    "low": float(r["最低"]),
                    "volume": float(r["成交量"]),
                    "pct": float(r.get("涨跌幅", 0) or 0),
                })
        except Exception:
            pass
        if not rows:
            try:
                # 新浪带前缀
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
                end = datetime.now().strftime("%Y%m%d")
                df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
                                         start_date=start, end_date=end, adjust="qfq")
                for _, r in df.iterrows():
                    rows.append({
                        "date": str(r["date"]),
                        "open": float(r["open"]),
                        "close": float(r["close"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "volume": float(r["volume"]),
                        "pct": 0.0,
                    })
            except Exception:
                pass
        if rows:
            p.write_text(json.dumps(rows))
            return rows[-days:]

    # 合成兜底
    px = 10 + (hash(code) % 5000) / 10.0
    out = []
    for i in range(days):
        px = px * (1 + random.gauss(0.0008, 0.018))
        out.append({"date": f"D-{days-i}", "open": round(px, 2),
                    "close": round(px, 2), "high": round(px * 1.01, 2),
                    "low": round(px * 0.99, 2),
                    "volume": random.randint(1_000_000, 9_000_000)})
    return out


def compute_indicators(history: list[dict]) -> dict:
    closes = [h["close"] for h in history]
    if len(closes) < 20:
        return {}
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / max(1, min(60, len(closes)))
    ma120 = sum(closes[-120:]) / max(1, min(120, len(closes)))
    vol = math.sqrt(sum((c - ma20) ** 2 for c in closes[-20:]) / 20) / ma20
    ret_5d = closes[-1] / closes[-5] - 1
    ret_20d = closes[-1] / closes[-20] - 1
    ret_60d = closes[-1] / closes[-60] - 1 if len(closes) >= 60 else 0
    ret_240d = closes[-1] / closes[-240] - 1 if len(closes) >= 240 else 0
    high_240 = max(closes[-240:]) if len(closes) >= 240 else max(closes)
    low_240 = min(closes[-240:]) if len(closes) >= 240 else min(closes)
    return {
        "last": closes[-1],
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "ma120": round(ma120, 2),
        "vol_20d": round(vol, 4),
        "ret_5d": round(ret_5d * 100, 2),
        "ret_20d": round(ret_20d * 100, 2),
        "ret_60d": round(ret_60d * 100, 2),
        "ret_240d": round(ret_240d * 100, 2),
        "high_52w": round(high_240, 2),
        "low_52w": round(low_240, 2),
        "near_52w_high": round((closes[-1] / high_240 - 1) * 100, 2),
    }


# -------- 新闻：akshare 多源 + 简易 RSS（可选） --------

def _hash_id(*parts: str) -> str:
    h = hashlib.md5()
    for p in parts:
        h.update((p or "").encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


def fetch_news(limit: int = 80, hot_codes: list[str] | None = None) -> list[dict]:
    """聚合多源财经新闻：
      - 财新主新闻 stock_news_main_cx        (~100 条/次，行业/市场动态最丰富)
      - 东方财富个股新闻 stock_news_em        (针对 hot_codes 中每只票拉 10 条)
      - CCTV 国内时政 news_cctv               (少量，宏观信号)
      - 财联社全球电报 stock_info_global_cls  (能命中就拉)
    返回去重后的列表。
    """
    rows: list[dict] = []

    if HAVE_AK:
        # ① 财新主新闻
        try:
            df = ak.stock_news_main_cx()
            for _, r in df.iterrows():
                tag = str(r.get("tag", ""))
                summary = str(r.get("summary", ""))
                url = str(r.get("url", ""))
                if not summary:
                    continue
                rows.append({
                    "id": _hash_id("caixin", summary),
                    "source": f"财新-{tag}" if tag else "财新",
                    "title": summary[:200],
                    "content": summary[:1000],
                    "url": url[:500],
                })
        except Exception:
            pass

        # ② 东财个股新闻 — 用线程池并发拉，整体 60s 上限
        em_codes = [c for c in (hot_codes or [])[:30]]
        if em_codes:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def _fetch_em(code):
                try:
                    df = ak.stock_news_em(symbol=code)
                    out = []
                    for _, r in df.head(8).iterrows():
                        title = str(r.get("新闻标题") or "").strip()
                        content = str(r.get("新闻内容") or "").strip()
                        src = str(r.get("文章来源") or "")
                        pub = str(r.get("发布时间") or "")
                        url = str(r.get("新闻链接") or "")
                        if not title:
                            continue
                        out.append({
                            "id": _hash_id("em", code, title),
                            "source": f"东财·{src}·[{code}]",
                            "title": title[:200],
                            "content": (f"[{pub}] " + content)[:1000],
                            "url": url[:500],
                        })
                    return out
                except Exception:
                    return []
            t_start = time.time()
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(_fetch_em, c): c for c in em_codes}
                for fut in as_completed(futs, timeout=60):
                    if time.time() - t_start > 55:
                        break
                    try:
                        rows.extend(fut.result(timeout=5) or [])
                    except Exception:
                        continue

        # ③ 财联社全球电报（如果可用，但单源 timeout 严控以免卡住）
        try:
            fn_cls = getattr(ak, "stock_info_global_cls", None)
            if fn_cls:
                import socket
                old_to = socket.getdefaulttimeout()
                socket.setdefaulttimeout(8)
                try:
                    df = fn_cls()
                except Exception:
                    df = None
                finally:
                    socket.setdefaulttimeout(old_to)
                if df is not None and len(df):
                    for _, r in df.head(20).iterrows():
                        title = str(r.get("标题") or r.get("title") or "").strip()
                        content = str(r.get("内容") or r.get("content") or "").strip()
                        if not title and not content:
                            continue
                        rows.append({
                            "id": _hash_id("cls", title or content[:60]),
                            "source": "财联社·电报",
                            "title": (title or content[:200])[:200],
                            "content": content[:1000],
                            "url": "",
                        })
        except Exception:
            pass

        # ④ CCTV 时政（量少，作为宏观补充）
        try:
            df = ak.news_cctv().head(15)
            for _, r in df.iterrows():
                title = str(r.get("title", "") or r.get("新闻标题", "")).strip()
                if not title:
                    continue
                content = str(r.get("content", "") or r.get("新闻内容", "")).strip()
                rows.append({
                    "id": _hash_id("cctv", title),
                    "source": "CCTV·时政",
                    "title": title[:200],
                    "content": content[:1000],
                    "url": "",
                })
        except Exception:
            pass

        # ⑤ 全球宏观经济日历（百度，含全球各国数据/事件，每天 ~100 条）
        try:
            fn_baidu = getattr(ak, "news_economic_baidu", None)
            if fn_baidu:
                df = fn_baidu()
                for _, r in df.head(50).iterrows():
                    date = str(r.get("日期", "")).strip()
                    t_ = str(r.get("时间", "")).strip()
                    area = str(r.get("地区", "")).strip()
                    event = str(r.get("事件", "")).strip()
                    if not event:
                        continue
                    pub = r.get("公布")
                    forecast = r.get("预期")
                    prev = r.get("前值")
                    title = f"[{area}] {event}"
                    content = f"日期 {date} {t_}; 公布 {pub}; 预期 {forecast}; 前值 {prev}"
                    rows.append({
                        "id": _hash_id("baidu_econ", date, t_, event),
                        "source": f"全球·宏观日历·{area}",
                        "title": title[:200],
                        "content": content[:1000],
                        "url": "",
                    })
        except Exception:
            pass

        # ⑥ 涨停板异动（市场情绪/题材风向）
        try:
            fn_zt = getattr(ak, "stock_zt_pool_em", None)
            if fn_zt:
                import socket
                old_to = socket.getdefaulttimeout()
                socket.setdefaulttimeout(6)
                try:
                    df = fn_zt(date=time.strftime("%Y%m%d"))
                except Exception:
                    df = None
                finally:
                    socket.setdefaulttimeout(old_to)
                if df is not None and len(df):
                    for _, r in df.head(20).iterrows():
                        name = str(r.get("名称") or "").strip()
                        code = str(r.get("代码") or "").strip()
                        reason = str(r.get("涨停原因") or r.get("封板原因") or "").strip()
                        if not name:
                            continue
                        rows.append({
                            "id": _hash_id("zt", code, name),
                            "source": "异动·涨停板",
                            "title": f"{name}({code}) 涨停 {('— '+reason[:80]) if reason else ''}",
                            "content": reason[:500],
                            "url": "",
                        })
        except Exception:
            pass

        # ⑦ 全球期货/外汇要闻（如有）
        for fn_name, src_label in [
            ("futures_news_shmet", "全球·有色金属"),
            ("news_yangshi", "央视新闻·全球"),
        ]:
            try:
                fn = getattr(ak, fn_name, None)
                if not fn:
                    continue
                import socket
                old_to = socket.getdefaulttimeout()
                socket.setdefaulttimeout(6)
                try:
                    df = fn()
                except Exception:
                    df = None
                finally:
                    socket.setdefaulttimeout(old_to)
                if df is None or not len(df):
                    continue
                for _, r in df.head(10).iterrows():
                    title = str(r.get("标题") or r.get("title") or
                                 r.get("新闻标题") or "").strip()
                    if not title:
                        continue
                    content = str(r.get("内容") or r.get("content") or
                                   r.get("新闻内容") or "").strip()
                    rows.append({
                        "id": _hash_id(fn_name, title),
                        "source": src_label,
                        "title": title[:200],
                        "content": content[:1000],
                        "url": "",
                    })
            except Exception:
                continue

    # 去重 + 截断
    seen = set()
    uniq: list[dict] = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)

    if not uniq:
        return [
            {"id": _hash_id("synth", t), "source": "synthetic",
             "title": t, "content": "", "url": ""}
            for t in [
                "央行：保持流动性合理充裕",
                "美联储官员鸽派表态，A 股北向资金流入扩大",
                "AI 算力板块强势反弹",
            ]
        ]
    return uniq[:limit]

