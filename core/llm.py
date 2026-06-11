"""LLM 调用层：CodeBuddy v2 SSE（OpenAI 兼容），支持 tool-call。"""
from __future__ import annotations
import json
import socket
import time
import urllib.parse
import urllib.request
import urllib.error
from .config import LLM_API_KEY, LLM_ENDPOINTS, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_RETRY

_LAST_OK_ENDPOINT = None
# DNS 解析缓存：{host: (ip, expires_at)}，1 小时过期
_DNS_CACHE: dict[str, tuple[str, float]] = {}
_DNS_TTL = 3600


def _resolve_with_cache(host: str) -> str | None:
    """优先返回缓存 IP；过期或没有就重新解析。"""
    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and cached[1] > now:
        return cached[0]
    try:
        info = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        if info:
            ip = info[0][4][0]
            _DNS_CACHE[host] = (ip, now + _DNS_TTL)
            return ip
    except Exception:
        pass
    # 解析失败但有过期缓存，宁可用过期的也比 fail
    if cached:
        return cached[0]
    return None


def _expand_endpoints(endpoints: list[str]) -> list[str]:
    """对每个 endpoint，生成「域名版 + IP 直连版」两个候选。"""
    out: list[str] = []
    for ep in endpoints:
        out.append(ep)
        try:
            parsed = urllib.parse.urlparse(ep)
            host = parsed.hostname
            if not host:
                continue
            ip = _resolve_with_cache(host)
            if ip and ip != host:
                # 用 IP 替换 host，仍发原 Host header
                netloc_ip = f"{ip}:{parsed.port}" if parsed.port else ip
                ep_ip = parsed._replace(netloc=netloc_ip).geturl()
                if ep_ip not in out:
                    out.append(ep_ip)
        except Exception:
            continue
    return out


def _stream_post(endpoint: str, payload: dict, timeout: int,
                  original_host: str | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Accept": "text/event-stream",
    }
    # 如果是 IP 直连，需要带回原 Host 头让 SNI/路由对
    if original_host:
        headers["Host"] = original_host
    req = urllib.request.Request(
        endpoint, data=data, method="POST", headers=headers,
    )
    chunks: list[str] = []
    tool_acc: dict[int, dict] = {}
    finish_reason = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            ps = line[5:].strip()
            if ps == "[DONE]":
                break
            try:
                obj = json.loads(ps)
            except Exception:
                continue
            try:
                ch0 = obj["choices"][0]
                if ch0.get("finish_reason"):
                    finish_reason = ch0["finish_reason"]
                delta = ch0.get("delta") or {}
                if delta.get("content"):
                    chunks.append(delta["content"])
                msg = ch0.get("message")
                if msg:
                    if msg.get("content"):
                        chunks.append(msg["content"])
                    if msg.get("tool_calls"):
                        for i, tc in enumerate(msg["tool_calls"]):
                            slot = tool_acc.setdefault(
                                i, {"id": "", "name": "", "arguments": ""}
                            )
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] = fn.get("name") or slot["name"]
                            slot["arguments"] = (slot["arguments"]
                                                 + (fn.get("arguments") or ""))
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
            except Exception:
                continue
    return {
        "content": "".join(chunks),
        "tool_calls": [tool_acc[k] for k in sorted(tool_acc.keys())],
        "finish_reason": finish_reason,
    }


def chat(system: str | None = None,
         user: str | None = None,
         json_mode: bool = True,
         max_tokens: int = 800,
         messages: list[dict] | None = None,
         tools: list[dict] | None = None,
         tool_choice: str | dict | None = None,
         model: str | None = None) -> dict:
    global _LAST_OK_ENDPOINT
    if messages is None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if user:
            messages.append({"role": "user", "content": user})

    payload: dict = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if json_mode and not tools:
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    endpoints = []
    if _LAST_OK_ENDPOINT:
        endpoints.append(_LAST_OK_ENDPOINT)
    for e in LLM_ENDPOINTS:
        if e not in endpoints:
            endpoints.append(e)
    # 加入 IP 直连备用版本
    expanded = _expand_endpoints(endpoints)

    # 提前算好 host 映射
    ep_host_map: dict[str, str | None] = {}
    for ep in endpoints:
        try:
            ep_host_map[ep] = urllib.parse.urlparse(ep).hostname
        except Exception:
            ep_host_map[ep] = None
    # IP 备用版需要原 host
    for ep in expanded:
        if ep in ep_host_map:
            continue
        # 这是 IP 直连版，找原 endpoint 的 host
        try:
            parsed = urllib.parse.urlparse(ep)
            ip_host = parsed.hostname
            # 在 _DNS_CACHE 里反查
            for h, (cached_ip, _) in _DNS_CACHE.items():
                if cached_ip == ip_host:
                    ep_host_map[ep] = h
                    break
            else:
                ep_host_map[ep] = None
        except Exception:
            ep_host_map[ep] = None

    last_err = None
    MAX_RETRY = max(LLM_MAX_RETRY, 5)
    for ep in expanded:
        for attempt in range(MAX_RETRY):
            try:
                # 仅 IP 备用版需要传 original_host
                orig_host = ep_host_map.get(ep)
                if orig_host and orig_host in ep:
                    orig_host = None  # 域名版不需要
                res = _stream_post(ep, payload, LLM_TIMEOUT, original_host=orig_host)
                if res["content"] or res["tool_calls"]:
                    _LAST_OK_ENDPOINT = ep
                    return {
                        "ok": True,
                        "text": res["content"],
                        "tool_calls": res["tool_calls"],
                        "finish_reason": res["finish_reason"],
                        "endpoint": ep,
                    }
                last_err = "empty stream"
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                last_err = f"HTTP {e.code}: {body}"
                if e.code in (401, 403):
                    return {"ok": False, "text": "", "tool_calls": [],
                            "error": last_err, "endpoint": ep}
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                # DNS 失败时清缓存，下次会重新解析
                if "getaddrinfo" in str(e) or "Name or service" in str(e):
                    _DNS_CACHE.clear()
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                break
            # 阶梯式 backoff：1s, 2s, 4s, 6s, 8s
            time.sleep(min(8, 1 + attempt * 2))
    return {"ok": False, "text": "", "tool_calls": [],
            "error": last_err, "endpoint": None}


def parse_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            try:
                return json.loads(chunk)
            except Exception:
                continue
    s = text.find("{"); e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            return None
    return None
