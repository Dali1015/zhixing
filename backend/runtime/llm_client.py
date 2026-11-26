# -*- coding: utf-8 -*-
"""
Muse Agent Runtime — 原生 HTTP SSE 逐 Token 流式大模型客户端
特性：
1. 真实开启 stream=True，逐行无缓冲解析远端 SSE 数据流；
2. 在当前请求内保留工具调用所需的 reasoning_content；
3. 原生支持流式分片 tool_calls（按 index 自动拼接跨 chunk 的函数名与 JSON 参数）；
4. 针对国内主流大模型域名自动优先绕过系统残留代理直连（避免 Clash/系统代理导致 TLS 握手卡死），并严格限制连接超时。
"""
import json
import re
import ssl
import time
from typing import Any, Dict, Generator, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
import urllib.request


DOMESTIC_DIRECT_HOSTS = (
    "deepseek.com",
    "siliconflow.cn",
    "aliyuncs.com",
    "huaweicloud.com",
    "modelarts",
    "bigmodel.cn",
    "moonshot.cn",
    "volces.com",
    "baidubce.com",
    "127.0.0.1",
    "localhost",
    "0.0.0.0",
)


def clean_api_key(raw_key: str) -> str:
    """剔除粘贴密钥时常见的零宽字符、首尾空白与换行，防止 HTTP Header 编码阻塞或报错"""
    if not raw_key:
        return ""
    return re.sub(r"[\s\u200b-\u200f\ufeff]", "", str(raw_key))


def is_llm_configured(config: Dict[str, str]) -> bool:
    url = (config.get("api_url") or "").strip()
    key = clean_api_key(config.get("api_key") or "")
    if not url:
        return False
    if key:
        return True
    return any(local_host in url for local_host in ("127.0.0.1", "localhost", "0.0.0.0"))


def resolve_model(config: Dict[str, str]) -> str:
    model = (config.get("model") or "deepseek-flash").strip()
    if urlparse(config.get("api_url") or "").hostname == "api.deepseek.com":
        if model in ("deepseek-chat", "deepseek-reasoner"):
            return "deepseek-flash"
    return model


def _deepseek_options(config: Dict[str, str], model: str, deep: bool) -> Dict[str, Any]:
    host = urlparse(config.get("api_url") or "").hostname
    if host != "api.deepseek.com" or model not in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"):
        return {}
    enabled = deep or config.get("model") == "deepseek-reasoner"
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def _open_llm_request(req: urllib.request.Request, timeout: int):
    """智能打开 HTTP 请求：国内直连域名优先绕过系统代理，彻底避免代理节点卡死"""
    ctx = ssl.create_default_context()
    host = (urlparse(req.full_url).hostname or "").lower()
    prefer_direct = any(d in host for d in DOMESTIC_DIRECT_HOSTS)
    deadline = time.monotonic() + timeout

    if prefer_direct:
        direct_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPHandler(),
        )
        try:
            return direct_opener.open(req, timeout=timeout)
        except HTTPError:
            # HTTP 401/400/429 等属于服务端已正常响应，直接抛出供上层提取详细错误体
            raise
        except URLError:
            # 若本机处于特殊企业内网必须走系统代理，则回退默认代理
            pass

    default_opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPHandler(),
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("模型连接超时")
    return default_opener.open(req, timeout=remaining)


def _format_http_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        try:
            err_body = exc.read().decode("utf-8", errors="ignore")
            data = json.loads(err_body)
            msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("message") or data.get("error_msg")
            if msg:
                return f"HTTP {exc.code}: {msg}"
            if err_body.strip():
                return f"HTTP {exc.code}: {err_body.strip()[:140]}"
        except Exception:
            pass
        return f"HTTP {exc.code}: {exc.reason}"
    return str(exc)[:200]


def parse_sse_stream_lines(raw_lines: Iterable[bytes], deadline: Optional[float] = None) -> Generator[Dict[str, Any], None, None]:
    """解析 OpenAI 兼容 SSE，聚合一次模型调用的正文和工具参数。"""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls_map: Dict[int, Dict[str, Any]] = {}

    for raw_line in raw_lines:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("模型生成超时")
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            pkt = json.loads(data_str)
        except Exception:
            continue

        if pkt.get("error"):
            raise RuntimeError(str(pkt["error"])[:200])
        choices = pkt.get("choices") or []
        if not choices:
            if pkt.get("answer"):
                txt = str(pkt["answer"])
                content_parts.append(txt)
                yield {"type": "content_delta", "text": txt}
            continue

        delta = choices[0].get("delta") or choices[0].get("message") or {}

        r_txt = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if r_txt:
            reasoning_parts.append(r_txt)
            yield {"type": "reasoning_delta", "text": r_txt}

        c_txt = delta.get("content") or ""
        if c_txt:
            content_parts.append(c_txt)
            yield {"type": "content_delta", "text": c_txt}

        t_deltas = delta.get("tool_calls") or []
        for tc in t_deltas:
            idx = int(tc.get("index", 0))
            if idx not in tool_calls_map:
                tool_calls_map[idx] = {
                    "id": tc.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            entry = tool_calls_map[idx]
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                entry["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                entry["function"]["arguments"] += fn["arguments"]

    ordered_tools = [tool_calls_map[k] for k in sorted(tool_calls_map.keys()) if tool_calls_map[k]["function"]["name"]]
    yield {
        "type": "turn_done",
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": ordered_tools,
    }


def _build_auth_headers(api_url: str, api_key: str, accept_sse: bool = True) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json" if accept_sse else "application/json",
        "User-Agent": "MuseAgentRuntime/3.0",
        "Connection": "close",
    }
    clean_k = clean_api_key(api_key)
    if clean_k:
        headers["Authorization"] = f"Bearer {clean_k}"
        if "huaweicloud" in api_url.lower() or "modelarts" in api_url.lower():
            headers["X-Auth-Token"] = clean_k
    return headers


def stream_chat_completion(
    config: Dict[str, str],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 3200,
    timeout: int = 45,
    deep: bool = False,
) -> Generator[Dict[str, Any], None, None]:
    api_url = (config.get("api_url") or "").strip()
    api_key = clean_api_key(config.get("api_key") or "")
    model = resolve_model(config)
    if not api_url:
        raise ValueError("未配置大模型 API Endpoint URL")

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "stream": True,
    }
    payload.update(_deepseek_options(config, model, deep))
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_build_auth_headers(api_url, api_key, accept_sse=True),
        method="POST",
    )

    try:
        deadline = time.monotonic() + timeout
        with _open_llm_request(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                for evt in parse_sse_stream_lines(resp, deadline=deadline):
                    yield evt
            else:
                raw_body = resp.read().decode("utf-8", errors="ignore")
                if raw_body.lstrip().startswith("data:"):
                    byte_lines = [ln.encode("utf-8") for ln in raw_body.splitlines()]
                    for evt in parse_sse_stream_lines(byte_lines, deadline=deadline):
                        yield evt
                    return

                data = json.loads(raw_body)
                content = ""
                reasoning = ""
                tcalls: List[Dict[str, Any]] = []
                if "choices" in data and data["choices"]:
                    msg = data["choices"][0].get("message") or {}
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                    tcalls = msg.get("tool_calls") or []
                elif "answer" in data:
                    content = str(data["answer"])
                elif "result" in data:
                    content = str(data["result"])

                if reasoning:
                    yield {"type": "reasoning_delta", "text": reasoning}
                if content:
                    yield {"type": "content_delta", "text": content}
                yield {
                    "type": "turn_done",
                    "content": content,
                    "reasoning": reasoning,
                    "tool_calls": tcalls,
                }
    except Exception as exc:
        raise RuntimeError(_format_http_error(exc)) from exc


def probe_llm_connection(config: Dict[str, str]) -> Dict[str, Any]:
    """快速实测大模型接口连通性（采用非流式极短请求 + 8 秒超时 + 国内域名直连，秒级返回结果）"""
    api_url = (config.get("api_url") or "").strip()
    api_key = clean_api_key(config.get("api_key") or "")
    model = resolve_model(config)

    if not api_url:
        return {"ok": False, "latency_ms": 0, "model": model, "reply_preview": "", "error": "API Endpoint URL 不能为空"}
    if not api_key and not any(h in api_url for h in ("127.0.0.1", "localhost", "0.0.0.0")):
        return {"ok": False, "latency_ms": 0, "model": model, "reply_preview": "", "error": "请输入有效的 API Key"}

    t0 = time.time()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }
    if urlparse(api_url).hostname == "api.deepseek.com":
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_build_auth_headers(api_url, api_key, accept_sse=False),
        method="POST",
    )
    try:
        with _open_llm_request(req, timeout=8) as resp:
            raw_body = resp.read().decode("utf-8", errors="ignore")
        ms = int((time.time() - t0) * 1000)
        preview = "OK"
        if raw_body.strip().startswith("{"):
            data = json.loads(raw_body)
            if data.get("choices"):
                msg = data["choices"][0].get("message") or {}
                preview = (msg.get("content") or "OK").strip()[:60]
            elif data.get("answer"):
                preview = str(data["answer"]).strip()[:60]
        return {
            "ok": True,
            "latency_ms": ms,
            "model": model,
            "reply_preview": preview or "OK",
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": int((time.time() - t0) * 1000),
            "model": model,
            "reply_preview": "",
            "error": _format_http_error(exc),
        }
