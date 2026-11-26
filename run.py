#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知行 Personal AI Agent Runtime — 全栈服务端入口
对标市场主流通用 Agent 底座（Claude Desktop / Manus / Dify）：
  • HTTP SSE 对话与交付物通知
  • 受控 Python 3 代码解释器沙箱（直接计算工作区 Excel / CSV / Word / 文本数据）
  • 免 Key 全网搜索 + 任意网页正文阅读 + 5 类实时信息流连接器守护进程
  • 自进化长期记忆库（驱动对话个性化与动态流实时重排）
  • 独立交互式 Artifact 画布（HTML 数据看板 / Markdown / SVG / 代码 / .ics 日历）
  • 可插拔 Markdown 技能包（Skills）+ 双向 MCP 协议网关（POST /mcp 供华为云 AgentArts 挂载）
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, quote, urlparse
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backend.runtime.academic_engine import (
    audit_academic_profile,
    generate_opportunity_ics,
    get_academic_catalog,
    match_opportunities,
    parse_courses_text,
    render_academic_report_html,
    simulate_what_if_scenarios,
)
from backend.runtime.agent_loop import execute_runtime_tool, run_agent_stream
from backend.runtime.llm_client import is_llm_configured, probe_llm_connection, resolve_model
from backend.runtime.mcp_bridge import MCPClient, build_mcp_server_manifest
from backend.runtime.skills import SkillManager
from backend.runtime.storage import RuntimeStorage, extract_file_text
from backend.runtime.web_tools import (
    fetch_source_items,
    rank_feed_by_memories,
    start_feed_daemon,
    sync_all_sources,
    web_fetch,
)

FRONTEND_FILE = os.path.join(BASE_DIR, "frontend", "index.html")
KB_DIR = os.path.join(BASE_DIR, "knowledge_base")
SEED_NOTICES_FILE = os.path.join(BASE_DIR, "backend", "data", "campus_notices.json")

WORKSPACE_DIR = os.environ.get("MUSE_WORKSPACE_DIR", os.path.join(BASE_DIR, "workspace"))
STORAGE = RuntimeStorage(WORKSPACE_DIR)
SKILLS_MGR = SkillManager(STORAGE.skills_dir)
PRODUCT_ENTRY = __name__ == "__main__" and "--product" in sys.argv[1:]
if not PRODUCT_ENTRY:
    STORAGE.bootstrap_defaults(builtin_kb_dir=KB_DIR, seed_notices_path=SEED_NOTICES_FILE)

if not PRODUCT_ENTRY and os.environ.get("MUSE_DISABLE_DAEMON") != "1":
    start_feed_daemon(STORAGE)

# 可供自动化基准测试注入流函数或本地 MCP 传输层的钩子（生产环境保持 None）
CUSTOM_STREAM_FN: Optional[Callable[..., Any]] = None
CUSTOM_MCP_TRANSPORT: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None


def _mask_secret(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "••••" + s[-4:]


def _public_llm_status(storage: RuntimeStorage) -> Dict[str, Any]:
    cfg = storage.get_llm_config()
    return {
        "api_url": cfg["api_url"],
        "model": resolve_model(cfg),
        "has_key": bool(cfg["api_key"]),
        "masked_key": _mask_secret(cfg["api_key"]),
        "has_search_key": bool(cfg["search_api_key"]),
        "masked_search_key": _mask_secret(cfg["search_api_key"]),
        "engine_ready": is_llm_configured(cfg),
    }


def _ranked_feeds(storage: RuntimeStorage, limit: int = 60):
    mems = storage.list_memories()
    items = storage.list_feed_items(limit=limit)
    return rank_feed_by_memories(items, mems)


def build_openapi_spec(host_header: str) -> Dict[str, Any]:
    base_url = f"http://{host_header}" if host_header else "http://127.0.0.1:8765"
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "知行智能体运行时 API",
            "version": "3.0.0",
            "description": "面向华为云 AgentArts 与外部系统的通用智能体运行时 REST 插件规范（含全网检索、Python 沙箱、长期记忆、FTS5 知识库与画布生成）。",
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/mcp": {
                "post": {
                    "operationId": "mcpJsonRpcEndpoint",
                    "summary": "标准 JSON-RPC 2.0 streamableHttp MCP Server 端点（暴露 8 个核心 Agent 原语工具）",
                }
            },
            "/api/chat/stream": {
                "post": {
                    "operationId": "streamAgentChat",
                    "summary": "发起多轮自主规划 ReAct SSE 流式对话",
                }
            },
            "/api/feed/sync": {
                "post": {
                    "operationId": "syncAllFeedConnectors",
                    "summary": "并发同步全部启用的网络动态连接器",
                }
            },
            "/api/academic/analyze": {
                "post": {
                    "operationId": "analyzeAcademicProfile",
                    "summary": "确定性成绩单解析、160学分毕业体检（含博约通识）与校园机会资格对账",
                }
            },
            "/api/academic/what-if": {
                "post": {
                    "operationId": "simulateWhatIfROI",
                    "summary": "What-If 重修 vs 榜单竞赛/前沿认证综测推免 ROI 确定性推演",
                }
            },
            "/api/academic/export-ics": {
                "post": {
                    "operationId": "exportAcademicIcs",
                    "summary": "生成避开满课日的标准 RFC 5545 .ics 日程包",
                }
            },
            "/api/knowledge/search": {
                "post": {
                    "operationId": "searchLocalKnowledgeFts5",
                    "summary": "检索本地 SQLite FTS5 BM25 校规与个人文件切片",
                }
            },
        },
    }


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    allowed_chat_tools = None

    @property
    def storage(self):
        return STORAGE

    @property
    def skill_manager(self):
        return SKILLS_MGR

    def public_llm_status(self):
        return _public_llm_status(self.storage)

    def can_continue_session(self, session_id):
        return True

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("MUSE_QUIET_LOG") == "1":
            return
        sys.stderr.write("[ZhixingRuntime] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, raw: bytes, content_type: str, download_name: str = "", status: int = 200):
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if download_name:
            encoded = quote(download_name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            if not os.path.isfile(FRONTEND_FILE):
                self._send_json({"error": "Frontend file missing"}, status=404)
                return
            raw = open(FRONTEND_FILE, "rb").read()
            self._send_bytes(raw, "text/html; charset=utf-8")
            return

        if path == "/api/health":
            st = self.public_llm_status()
            self._send_json({
                "status": "ok",
                "engine_ready": st["engine_ready"],
                "model": st["model"],
                "workspace_dir": self.storage.workspace_dir,
            })
            return

        if path == "/openapi.json":
            host_hdr = self.headers.get("Host", "127.0.0.1:8765")
            self._send_json(build_openapi_spec(host_hdr))
            return

        if path == "/api/bootstrap":
            sessions = self.storage.list_sessions()
            if not sessions:
                created = self.storage.create_session("新对话")
                sessions = [created]
            req_sid = (qs.get("session_id") or [""])[0].strip()
            valid_ids = {s["session_id"] for s in sessions}
            active_sid = req_sid if req_sid in valid_ids else sessions[0]["session_id"]
            memories = self.storage.list_memories()
            feed_items = rank_feed_by_memories(self.storage.list_feed_items(limit=60), memories)
            self._send_json({
                "active_session_id": active_sid,
                "sessions": sessions,
                "messages": self.storage.get_messages(active_sid),
                "memories": memories,
                "files": self.storage.list_files(),
                "artifacts": self.storage.list_artifacts(),
                "skills": self.skill_manager.list_skills(),
                "mcp_servers": self.storage.get_mcp_servers(),
                "feed_sources": self.storage.list_feed_sources(),
                "feed_items": feed_items,
                "llm": self.public_llm_status(),
            })
            return

        if path == "/api/sessions/messages":
            sid = (qs.get("session_id") or [""])[0].strip()
            self._send_json({
                "session_id": sid,
                "messages": self.storage.get_messages(sid) if sid else [],
            })
            return

        if path == "/api/artifacts/detail":
            aid = (qs.get("artifact_id") or [""])[0].strip()
            art = self.storage.get_artifact(aid)
            if not art:
                self._send_json({"ok": False, "error": "Artifact not found"}, status=404)
                return
            self._send_json({"ok": True, "artifact": art})
            return

        if path == "/api/artifacts/download":
            aid = (qs.get("artifact_id") or [""])[0].strip()
            art = self.storage.get_artifact(aid)
            if not art:
                self._send_json({"ok": False, "error": "Artifact not found"}, status=404)
                return
            mime_map = {
                "html": "text/html; charset=utf-8",
                "markdown": "text/markdown; charset=utf-8",
                "svg": "image/svg+xml; charset=utf-8",
                "ics": "text/calendar; charset=utf-8",
                "csv": "text/csv; charset=utf-8",
                "code": "text/plain; charset=utf-8",
            }
            raw = (art.get("content") or "").encode("utf-8")
            self._send_bytes(raw, mime_map.get(art["kind"], "text/plain; charset=utf-8"), download_name=art["filename"])
            return

        if path == "/api/files/download":
            fid = (qs.get("file_id") or [""])[0].strip()
            fmeta = self.storage.get_file(fid)
            if not fmeta or not os.path.isfile(fmeta["disk_path"]):
                self._send_json({"ok": False, "error": "File not found"}, status=404)
                return
            raw = open(fmeta["disk_path"], "rb").read()
            self._send_bytes(raw, "application/octet-stream", download_name=fmeta["filename"])
            return

        if path == "/api/academic/catalog":
            catalog = get_academic_catalog()
            catalog["agentarts_guide"] = ""
            guide_path = os.path.join(BASE_DIR, "agentarts_configs", "AgentArts_多智能体编排与Prompt配置手册.md")
            if os.path.isfile(guide_path):
                try:
                    catalog["agentarts_guide"] = open(guide_path, encoding="utf-8").read()
                except OSError:
                    pass
            self._send_json({"ok": True, **catalog})
            return

        if path == "/api/sessions/export":
            sid = (qs.get("session_id") or [""])[0].strip()
            sessions = self.storage.list_sessions()
            sess = next((s for s in sessions if s["session_id"] == sid), None)
            if not sess:
                self._send_json({"ok": False, "error": "Session not found"}, status=404)
                return
            msgs = self.storage.get_messages(sid)
            md_lines = [
                f"# {sess['title']}",
                "",
                f"> 导出自 知行· 会话创建于 {sess['created_at']}",
                "",
                "---",
                "",
            ]
            for m in msgs:
                role_label = "用户" if m["role"] == "user" else "知行"
                md_lines.append(f"## {role_label} ({m.get('created_at', '')})")
                md_lines.append("")
                md_lines.append(m.get("content", ""))
                md_lines.append("")
            raw = "\n".join(md_lines).encode("utf-8")
            self._send_bytes(raw, "text/markdown; charset=utf-8", download_name=f"{sess['title'][:30] or 'chat'}.md")
            return

        self._send_json({"error": f"Not found: {path}"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 1. SSE 真实流式对话入口
        if path == "/api/chat/stream":
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json({"error": f"Invalid JSON: {exc}"}, status=400)
                return

            query = str(body.get("query", "")).strip()
            if not query:
                self._send_json({"error": "query is required"}, status=400)
                return
            session_id = str(body.get("session_id", "")).strip()
            if session_id and not self.can_continue_session(session_id):
                self._send_json({"error": "会话不存在或已失效。"}, status=404)
                return
            focus_item_id = str(body.get("focus_item_id", "")).strip() or None

            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            try:
                for evt in run_agent_stream(
                    query=query,
                    session_id=session_id,
                    storage=self.storage,
                    skills_mgr=self.skill_manager,
                    focus_item_id=focus_item_id,
                    stream_fn=CUSTOM_STREAM_FN,
                    mcp_transport_override=CUSTOM_MCP_TRANSPORT,
                    allowed_tools=self.allowed_chat_tools,
                ):
                    pkt = f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(pkt)
                    self.wfile.flush()
            except BrokenPipeError:
                pass
            return

        # 其余 JSON POST 接口
        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Invalid JSON: {exc}"}, status=400)
            return

        # 2. 标准双向 MCP Server (JSON-RPC 2.0 streamableHttp)
        if path == "/mcp":
            rpc_id = body.get("id")
            method = body.get("method", "")
            params = body.get("params") or {}

            if method == "initialize":
                self._send_json({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "zhixing-personal-agent-runtime", "version": "3.0.0"},
                    },
                })
                return

            if method in ("notifications/initialized", "ping"):
                self._send_json({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
                return

            if method == "tools/list":
                self._send_json({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"tools": build_mcp_server_manifest()},
                })
                return

            if method == "tools/call":
                t_name = str(params.get("name", "")).strip()
                t_args = params.get("arguments") or {}
                exec_res = execute_runtime_tool(
                    t_name,
                    t_args,
                    storage=self.storage,
                    session_id="mcp_remote",
                    mcp_transport_override=CUSTOM_MCP_TRANSPORT,
                )
                self._send_json({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(exec_res["llm_payload"], ensure_ascii=False)}],
                        "isError": not exec_res["ok"],
                    },
                })
                return

            self._send_json({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
            return

        # 3. 会话管理
        if path == "/api/sessions":
            action = str(body.get("action", "create")).strip()
            if action == "create":
                title = str(body.get("title", "新对话")).strip() or "新对话"
                sess = self.storage.create_session(title)
                self._send_json({
                    "ok": True,
                    "session": sess,
                    "active_session_id": sess["session_id"],
                    "sessions": self.storage.list_sessions(),
                    "messages": [],
                })
                return
            if action == "rename":
                self.storage.rename_session(str(body.get("session_id", "")), str(body.get("title", "新对话")))
                self._send_json({"ok": True, "sessions": self.storage.list_sessions()})
                return
            if action == "delete":
                self.storage.delete_session(str(body.get("session_id", "")))
                sessions = self.storage.list_sessions()
                if not sessions:
                    sessions = [self.storage.create_session("新对话")]
                active_sid = sessions[0]["session_id"]
                self._send_json({
                    "ok": True,
                    "active_session_id": active_sid,
                    "sessions": sessions,
                    "messages": self.storage.get_messages(active_sid),
                })
                return

        # 4. 长期记忆 CRUD（联动重算动态流相关度）
        if path == "/api/memories":
            action = str(body.get("action", "upsert")).strip()
            if action == "delete":
                self.storage.delete_memory(str(body.get("memory_id", "")))
            else:
                content = str(body.get("content", "")).strip()
                if not content:
                    self._send_json({"ok": False, "error": "记忆内容不能为空"}, status=400)
                    return
                self.storage.upsert_memory(
                    category=str(body.get("category", "fact")),
                    content=content,
                    memory_id=str(body.get("memory_id", "")),
                    source="user",
                )
            memories = self.storage.list_memories()
            self._send_json({
                "ok": True,
                "memories": memories,
                "feed_items": rank_feed_by_memories(self.storage.list_feed_items(limit=60), memories),
            })
            return

        # 5. 工作区文件上传与删除
        if path == "/api/files/upload":
            filename = str(body.get("filename", "")).strip()
            b64 = str(body.get("content_base64", "")).strip()
            txt = body.get("content_text")
            if not filename:
                self._send_json({"ok": False, "error": "缺少文件名"}, status=400)
                return
            if b64:
                if "," in b64 and b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                raw_bytes = base64.b64decode(b64)
            elif txt is not None:
                raw_bytes = str(txt).encode("utf-8")
            else:
                self._send_json({"ok": False, "error": "文件内容为空"}, status=400)
                return

            meta = self.storage.save_and_index_file(filename, raw_bytes, scope="user")
            self._send_json({"ok": True, "file": meta, "files": self.storage.list_files()})
            return

        if path == "/api/files/delete":
            self.storage.delete_file(str(body.get("file_id", "")))
            self._send_json({"ok": True, "files": self.storage.list_files()})
            return

        # 6. 画布产物删除
        if path == "/api/artifacts/delete":
            self.storage.delete_artifact(str(body.get("artifact_id", "")))
            self._send_json({"ok": True, "artifacts": self.storage.list_artifacts()})
            return

        # 7. 可插拔技能包管理
        if path == "/api/skills":
            action = str(body.get("action", "toggle")).strip()
            slug = str(body.get("slug", "")).strip()
            if action == "save":
                self.skill_manager.save_skill(
                    slug=slug or str(body.get("title", "custom")),
                    title=str(body.get("title", "")).strip(),
                    description=str(body.get("description", "")).strip(),
                    enabled=bool(body.get("enabled", True)),
                    body=str(body.get("body", "")),
                )
            elif action == "toggle":
                self.skill_manager.toggle_skill(slug)
            elif action == "delete":
                self.skill_manager.delete_skill(slug)
            self._send_json({"ok": True, "skills": self.skill_manager.list_skills()})
            return

        # 8. 外部 MCP Server 挂载与探测
        if path == "/api/mcp_servers":
            action = str(body.get("action", "add")).strip()
            servers = self.storage.get_mcp_servers()
            if action == "add":
                srv = {
                    "server_id": "mcp_" + uuid.uuid4().hex[:6],
                    "name": str(body.get("name", "外部 MCP 服务")).strip() or "外部 MCP 服务",
                    "transport": "stdio" if body.get("transport") == "stdio" else "http",
                    "target": str(body.get("target", "")).strip(),
                    "token": str(body.get("token", "")).strip(),
                    "enabled": True,
                    "tools": [],
                }
                disc = MCPClient.discover_tools(srv, transport_override=CUSTOM_MCP_TRANSPORT)
                srv["tools"] = disc.get("tools", [])
                srv["last_error"] = disc.get("error", "")
                servers.append(srv)
                self.storage.save_mcp_servers(servers)
                self._send_json({
                    "ok": disc["ok"],
                    "discovered_count": len(srv["tools"]),
                    "error": disc.get("error", ""),
                    "mcp_servers": servers,
                })
                return

            target_id = str(body.get("server_id", "")).strip()
            if action == "test":
                found_ok = False
                err_msg = ""
                cnt = 0
                for s in servers:
                    if s.get("server_id") == target_id:
                        disc = MCPClient.discover_tools(s, transport_override=CUSTOM_MCP_TRANSPORT)
                        s["tools"] = disc.get("tools", [])
                        s["last_error"] = disc.get("error", "")
                        found_ok = disc["ok"]
                        err_msg = disc.get("error", "")
                        cnt = len(s["tools"])
                        break
                self.storage.save_mcp_servers(servers)
                self._send_json({"ok": found_ok, "discovered_count": cnt, "error": err_msg, "mcp_servers": servers})
                return

            if action == "toggle":
                for s in servers:
                    if s.get("server_id") == target_id:
                        s["enabled"] = not bool(s.get("enabled", True))
                self.storage.save_mcp_servers(servers)
                self._send_json({"ok": True, "mcp_servers": servers})
                return

            if action == "delete":
                servers = [s for s in servers if s.get("server_id") != target_id]
                self.storage.save_mcp_servers(servers)
                self._send_json({"ok": True, "mcp_servers": servers})
                return

        # 9. 动态流同步、原文提取、收藏与订阅源管理
        if path == "/api/feed/sync":
            rep = sync_all_sources(self.storage)
            self._send_json({
                "ok": True,
                **rep,
                "feed_sources": self.storage.list_feed_sources(),
                "feed_items": _ranked_feeds(self.storage),
            })
            return

        if path == "/api/feed/read":
            iid = str(body.get("item_id", "")).strip()
            item = self.storage.get_feed_item(iid)
            if not item:
                self._send_json({"ok": False, "error": "Item not found"}, status=404)
                return
            if not item.get("full_content") and item.get("url", "").startswith("http"):
                fetched = web_fetch(item["url"])
                if fetched.get("ok") and fetched.get("content"):
                    self.storage.update_feed_item_content(iid, fetched["content"], fetched.get("attachments", []))
                    item = self.storage.get_feed_item(iid) or item
            self._send_json({"ok": True, "item": item})
            return

        if path == "/api/feed/save":
            iid = str(body.get("item_id", "")).strip()
            saved_state = self.storage.toggle_save_feed_item(iid)
            self._send_json({
                "ok": True,
                "saved": saved_state,
                "feed_items": _ranked_feeds(self.storage),
            })
            return

        if path == "/api/feed/sources":
            action = str(body.get("action", "add")).strip()
            if action == "add":
                name = str(body.get("name", "")).strip()
                kind = str(body.get("kind", "web_list")).strip()
                target = str(body.get("target", "")).strip()
                if not target:
                    self._send_json({"ok": False, "error": "请输入目标链接或检索词"}, status=400)
                    return
                src = self.storage.add_feed_source(name or target[:30], kind, target)
                # 新增订阅源时尝试立即拉取首批动态（若网络受限也不阻断保存）
                try:
                    skey = self.storage.get_llm_config().get("search_api_key", "")
                    pulled = fetch_source_items(src, search_api_key=skey, timeout=6)
                    if pulled:
                        self.storage.upsert_feed_items(pulled)
                        self.storage.mark_source_synced(src["source_id"])
                except Exception:
                    pass
            elif action == "toggle":
                self.storage.toggle_feed_source(str(body.get("source_id", "")))
            elif action == "delete":
                self.storage.delete_feed_source(str(body.get("source_id", "")))

            self._send_json({
                "ok": True,
                "feed_sources": self.storage.list_feed_sources(),
                "feed_items": _ranked_feeds(self.storage),
            })
            return

        # 10. 大模型引擎配置与真实连通性探针（测试成功自动保存，避免用户遗漏二次点击）
        if path in ("/api/settings/llm", "/api/llm/config"):
            self.storage.save_llm_config(
                api_url=str(body.get("api_url", "")),
                api_key=str(body.get("api_key", "")),
                model=str(body.get("model", "")),
                search_api_key=str(body.get("search_api_key", "")),
                clear_key=bool(body.get("clear_key", False)),
            )
            self._send_json({"ok": True, "llm": self.public_llm_status()})
            return

        if path in ("/api/settings/llm/test", "/api/llm/test"):
            saved_cfg = self.storage.get_llm_config()
            raw_input_key = str(body.get("api_key", "")).strip()
            test_cfg = {
                "api_url": str(body.get("api_url") or saved_cfg["api_url"]).strip(),
                "api_key": raw_input_key or saved_cfg["api_key"],
                "model": str(body.get("model") or saved_cfg["model"]).strip(),
            }
            res = probe_llm_connection(test_cfg)
            if res.get("ok"):
                self.storage.save_llm_config(
                    api_url=test_cfg["api_url"],
                    api_key=raw_input_key,
                    model=test_cfg["model"],
                    search_api_key=str(body.get("search_api_key", "")),
                )
                res["llm"] = _public_llm_status(self.storage)
            self._send_json(res)
            return

        # 11. 确定性学业决策引擎（成绩单解析 / 160学分与博约通识体检 / What-If推演 / 机会对账 / .ics导出 / 报告画布）
        if path == "/api/academic/analyze":
            try:
                rows_text = str(body.get("rows") or "").strip()
                file_id = str(body.get("file_id") or "").strip()
                if not rows_text and file_id:
                    fmeta = self.storage.get_file(file_id)
                    if not fmeta or not os.path.isfile(fmeta["disk_path"]):
                        raise ValueError("未找到指定的工作区文件。")
                    raw_bytes = open(fmeta["disk_path"], "rb").read()
                    rows_text = extract_file_text(fmeta["filename"], raw_bytes).get("text", "")
                courses = parse_courses_text(rows_text)
                grade = int(body.get("grade") or 2024)
                cet4 = int(body.get("cet4") or 520)
                cet6 = int(body.get("cet6") or 440)
                volunteer_hours = float(body.get("volunteer_hours") if body.get("volunteer_hours") is not None else 24.0)
                moral_score = float(body.get("moral_score") if body.get("moral_score") is not None else 88.0)
                innovation_raw = float(body.get("innovation_raw") if body.get("innovation_raw") is not None else 6.0)
                target_direction = str(body.get("target_direction") or "保研").strip() or "保研"

                audit = audit_academic_profile(
                    courses=courses,
                    grade=grade,
                    cet4=cet4,
                    cet6=cet6,
                    volunteer_hours=volunteer_hours,
                    moral_score=moral_score,
                    innovation_raw=innovation_raw,
                    target_direction=target_direction,
                )
                s = audit["summary"]
                opps = match_opportunities(
                    SEED_NOTICES_FILE,
                    grade=grade,
                    gpa_4=s["gpa_4"],
                    cet4=cet4,
                    cet6=cet6,
                    volunteer_hours=volunteer_hours,
                    fail_count=s["fail_count"],
                )
                self._send_json({"ok": True, "audit": audit, "opportunities": opps})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/academic/what-if":
            try:
                res = simulate_what_if_scenarios(
                    current_weighted_avg=float(body.get("current_weighted_avg") or 85.0),
                    total_credits=float(body.get("total_credits") or 90.0),
                    moral_score=float(body.get("moral_score") if body.get("moral_score") is not None else 88.0),
                    current_innovation_raw=float(body.get("current_innovation_raw") if body.get("current_innovation_raw") is not None else 6.0),
                    retake_course_name=str(body.get("retake_course_name") or "数据结构与算法").strip() or "核心专业课",
                    retake_credits=float(body.get("retake_credits") or 4.0),
                    retake_old_score=float(body.get("retake_old_score") or 74.0),
                    retake_target_score=float(body.get("retake_target_score") or 92.0),
                    retake_est_hours=float(body.get("retake_est_hours") or 60.0),
                    selected_competition_ids=body.get("selected_competition_ids") if isinstance(body.get("selected_competition_ids"), list) else None,
                )
                self._send_json({"ok": True, **res})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/academic/export-ics":
            try:
                title = str(body.get("title") or "学业冲刺与申报排期").strip() or "学业排期"
                deadline = str(body.get("deadline") or "").strip()
                prep_tasks = body.get("prep_tasks") if isinstance(body.get("prep_tasks"), list) else []
                materials = body.get("required_materials") if isinstance(body.get("required_materials"), list) else []
                free_hour = int(body.get("free_hour_start") or 19)
                free_hour = max(7, min(22, free_hour))
                ics_text, events = generate_opportunity_ics(title, deadline, prep_tasks, materials, free_hour_start=free_hour)
                save_artifact = bool(body.get("save_artifact", True)) and not self.public_llm_status().get("product_mode")
                artifact = None
                if save_artifact:
                    artifact = self.storage.create_artifact(
                        session_id=str(body.get("session_id") or ""),
                        title=f"日程 · {title[:32]}",
                        kind="ics",
                        content=ics_text,
                        filename=f"schedule_{uuid.uuid4().hex[:6]}.ics",
                    )
                self._send_json({
                    "ok": True,
                    "ics": ics_text,
                    "filename": artifact["filename"] if artifact else "zhixing_schedule.ics",
                    "events": events,
                    "artifact": artifact,
                    "artifacts": self.storage.list_artifacts() if artifact else None,
                })
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/academic/save-report":
            try:
                audit = body.get("audit")
                if not isinstance(audit, dict) or "summary" not in audit:
                    raise ValueError("缺少有效的学业体检数据。")
                what_if = body.get("what_if") if isinstance(body.get("what_if"), dict) else None
                html_doc = render_academic_report_html(audit, what_if=what_if)
                artifact = self.storage.create_artifact(
                    session_id=str(body.get("session_id") or ""),
                    title=f"学业体检看板 · GPA {audit['summary']['gpa_4']:.2f}",
                    kind="html",
                    content=html_doc,
                    filename=f"academic_report_{uuid.uuid4().hex[:6]}.html",
                )
                self._send_json({"ok": True, "artifact": artifact, "artifacts": self.storage.list_artifacts()})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/academic/sync-memory":
            try:
                summary = body.get("summary")
                if not isinstance(summary, dict):
                    raise ValueError("缺少学业摘要数据。")
                profile_line = (
                    f"{summary.get('grade', 2024)}级本科生，已修 {summary.get('passed_credits', 0)} 学分，"
                    f"加权均分 {summary.get('weighted_avg', 0)}（4.0 GPA {summary.get('gpa_4', 0)}），"
                    f"四级 {summary.get('cet4', 0)} / 六级 {summary.get('cet6', 0)}，"
                    f"博约通识已修 {summary.get('boyue_earned', 0)}/18 学分。"
                )
                goal_line = (
                    f"当前核心学业方向：{summary.get('target_direction', '保研')}，"
                    f"当前预估综测 {summary.get('zongce_total', 0)} 分（推免参考安全线 86.80 分）。"
                )
                self.storage.upsert_memory("profile", profile_line, source="academic_engine")
                self.storage.upsert_memory("goal", goal_line, source="academic_engine")
                memories = self.storage.list_memories()
                self._send_json({
                    "ok": True,
                    "memories": memories,
                    "feed_items": _ranked_feeds(self.storage),
                })
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            return

        # 12. 本地 SQLite FTS5 BM25 知识库实时检索实验台
        if path == "/api/knowledge/search":
            q = str(body.get("query") or "").strip()
            top_k = min(12, max(1, int(body.get("top_k") or 6)))
            hits = self.storage.search_knowledge_fts(q, top_k=top_k) if q else []
            self._send_json({"ok": True, "query": q, "hits": hits})
            return

        self._send_json({"ok": False, "error": f"Unknown endpoint: {path}"}, status=404)


class _ZhixingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    if PRODUCT_ENTRY:
        from product_server import start_product
        start_product(AppHandler, _ZhixingHTTPServer, STORAGE)
        return
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = _ZhixingHTTPServer((host, port), AppHandler)
    st = _public_llm_status(STORAGE)
    print(f"知行智能体运行时已启动 -> http://{host}:{port}")
    print(f"  Workspace : {STORAGE.workspace_dir}")
    print(f"  MCP Server: http://{host}:{port}/mcp (streamableHttp JSON-RPC 2.0)")
    print(f"  LLM Ready : {st['engine_ready']} (model={st['model']})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭知行智能体运行时...")
        server.server_close()


if __name__ == "__main__":
    main()
