# -*- coding: utf-8 -*-
"""
Muse Agent Runtime — 双向 MCP (Model Context Protocol) 桥接器
1. MCP Client：连接任意外部第三方 MCP Server（支持 streamableHttp URL 与本地 stdio 进程），动态拉取工具并注入 Agent 工具箱；
2. MCP Server：通过 POST /mcp 标准 JSON-RPC 2.0 暴露本底座全部 8 个原语工具，供华为云 AgentArts 等外部平台一键挂载。
"""
import json
import shlex
import ssl
import subprocess
from typing import Any, Callable, Dict, List, Optional
import urllib.request


class MCPClient:
    """支持 streamableHttp 与 stdio 双传输协议的标准 JSON-RPC 2.0 MCP 客户端"""

    @staticmethod
    def _rpc_http(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 12) -> Dict[str, Any]:
        ctx = ssl.create_default_context()
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=hdrs,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _rpc_stdio(command_str: str, requests_list: List[Dict[str, Any]], timeout: int = 12) -> List[Dict[str, Any]]:
        argv = shlex.split(command_str)
        if not argv:
            raise ValueError("stdio 命令为空")
        stdin_data = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests_list) + "\n"
        proc = subprocess.run(argv, input=stdin_data, capture_output=True, text=True, timeout=timeout)
        responses: List[Dict[str, Any]] = []
        for ln in (proc.stdout or "").splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    responses.append(json.loads(ln))
                except Exception:
                    continue
        return responses

    @classmethod
    def discover_tools(
        cls,
        server_cfg: Dict[str, Any],
        transport_override: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "muse-agent-runtime", "version": "3.0.0"}, "capabilities": {}},
        }
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

        try:
            if transport_override is not None:
                transport_override(init_req)
                res = transport_override(list_req)
            elif server_cfg.get("transport", "http") == "stdio":
                outs = cls._rpc_stdio(server_cfg.get("target", ""), [init_req, list_req])
                res = outs[-1] if outs else {}
            else:
                url = server_cfg.get("target", "")
                hdrs = {"Authorization": f"Bearer {server_cfg['token']}"} if server_cfg.get("token") else None
                cls._rpc_http(url, init_req, headers=hdrs)
                res = cls._rpc_http(url, list_req, headers=hdrs)

            tools = (res.get("result") or {}).get("tools") or []
            return {"ok": True, "tools": tools, "error": ""}
        except Exception as exc:
            return {"ok": False, "tools": [], "error": str(exc)[:160]}

    @classmethod
    def call_external_tool(
        cls,
        server_cfg: Dict[str, Any],
        tool_name: str,
        arguments: Dict[str, Any],
        transport_override: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "muse-agent-runtime", "version": "3.0.0"}, "capabilities": {}},
        }
        call_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            if transport_override is not None:
                res = transport_override(call_req)
            elif server_cfg.get("transport", "http") == "stdio":
                outs = cls._rpc_stdio(server_cfg.get("target", ""), [init_req, call_req])
                res = outs[-1] if outs else {}
            else:
                url = server_cfg.get("target", "")
                hdrs = {"Authorization": f"Bearer {server_cfg['token']}"} if server_cfg.get("token") else None
                res = cls._rpc_http(url, call_req, headers=hdrs)
            return {"ok": True, "result": res.get("result", res)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:160]}


def build_mcp_server_manifest() -> List[Dict[str, Any]]:
    """本底座对外暴露给华为云 AgentArts 的 8 个通用原语 MCP 工具规范"""
    return [
        {
            "name": "web_search",
            "description": "全网实时搜索任意公开网页、新闻通知与技术资讯",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]},
        },
        {
            "name": "web_fetch",
            "description": "实时读取指定 URL 的完整网页正文与附件下载链接",
            "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        },
        {
            "name": "python_sandbox",
            "description": "在受控 Python 3 代码解释器沙箱中运行代码，直接处理工作区 Excel/CSV/文本并输出精确计算结果",
            "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "purpose": {"type": "string"}}, "required": ["code"]},
        },
        {
            "name": "search_knowledge",
            "description": "在本地 SQLite FTS5 知识库中全文检索用户上传文档与参考资料",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        {
            "name": "memory_store",
            "description": "向用户长期记忆库写入、更新或删除个人背景、目标、偏好与关键事实",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["upsert", "delete"]},
                    "category": {"type": "string", "enum": ["profile", "goal", "preference", "fact"]},
                    "content": {"type": "string"},
                    "memory_id": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "memory_recall",
            "description": "检索用户的长期记忆条目",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        {
            "name": "artifact_create",
            "description": "生成可交互 HTML 数据看板、Markdown 报告、SVG 图表、代码或标准 .ics 日历文件到画布",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["html", "markdown", "svg", "ics", "csv", "code"]},
                    "content": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["title", "kind", "content"],
            },
        },
        {
            "name": "sync_feeds",
            "description": "立即触发所有已启用网络信息流连接器的并发增量同步",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
