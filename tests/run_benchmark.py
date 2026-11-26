#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知行 Personal AI Agent Runtime — 市场级全栈硬核实测与端到端基准套件
全面核验：
  1. 分片 SSE 思维链 + 流式 Tool-Calls JSON 重组解析器
  2. 真实 Excel (.xlsx) 上传 + 受控 Python 代码沙箱精确计算 + AST 危险指令拦截
  3. 真实华为 ICT 大赛官方 Word (.docx) 上传解析 + SQLite FTS5 全文检索
  4. 未配置 API Key 时的诚实边界（零虚假人设话术 + 直出本地 FTS5 片段）
  5. 多轮自主 ReAct 流式闭环（plan_tasks -> python_sandbox -> memory_store -> artifact_create -> 动态流重排）
  6. 可插拔 Markdown Skills 管理 + 双向 MCP (POST /mcp Server & MCPClient 动态工具注入)
  7. 全网搜索/网页提取解析器 + 前端零 Emoji / 零假人设 / JavaScript 语法静态审计
"""
import base64
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from threading import BoundedSemaphore
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

TEST_WS = tempfile.mkdtemp(prefix="zhixing_runtime_bench_")
os.environ["ZHIXING_WORKSPACE_DIR"] = TEST_WS
os.environ["ZHIXING_DISABLE_DAEMON"] = "1"
os.environ["ZHIXING_QUIET_LOG"] = "1"
# 确保初始测试环境无外部残留环境变量密钥，先验证未配置诚实态
for _k in ("DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "AGENTARTS_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import run as app_run
from product_server import configure_server, create_product_handler
from team_server import Invite, create_handler
from backend.runtime.agent_loop import build_merged_tools_schema
from backend.runtime.llm_client import parse_sse_stream_lines, stream_chat_completion
from backend.runtime.sandbox import execute_python_code
from backend.runtime.web_tools import (
    extract_webpage_content,
    parse_bing_search_html,
    parse_ddg_search_html,
    parse_rss_or_atom,
)


class _DummySocket:
    def makefile(self, *args, **kwargs):
        return io.BytesIO()


class _InMemoryHTTP:
    """无端口绑定的内存级 AppHandler 驱动器，完整走通 HTTP 路由、Headers 与 SSE 分帧写出"""

    @staticmethod
    def request(method: str, path: str, body_obj=None, *, handler_class=None, server=None, headers=None, raw_body=None):
        raw_req_body = raw_body or b""
        headers_str = "Host: 127.0.0.1:8765\r\n"
        if body_obj is not None:
            raw_req_body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
            headers_str += "Content-Type: application/json\r\n"
        if raw_req_body:
            headers_str += f"Content-Length: {len(raw_req_body)}\r\n"
        for name, value in (headers or {}).items():
            headers_str += f"{name}: {value}\r\n"
        raw_http = f"{method} {path} HTTP/1.1\r\n{headers_str}\r\n".encode("utf-8") + raw_req_body

        handler_class = handler_class or app_run.AppHandler
        handler = handler_class.__new__(handler_class)
        handler.rfile = io.BytesIO(raw_http)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 54321)
        handler.server = server
        handler.close_connection = True
        handler.raw_requestline = handler.rfile.readline()
        handler.parse_request()
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        else:
            handler.do_OPTIONS()

        raw_resp = handler.wfile.getvalue()
        head_bytes, _, payload = raw_resp.partition(b"\r\n\r\n")
        status_line = head_bytes.splitlines()[0].decode("utf-8")
        status_code = int(status_line.split()[1])
        return status_code, head_bytes.decode("utf-8", errors="ignore"), payload


def _build_minimal_xlsx_bytes(rows) -> bytes:
    """用标准库生成真实 OOXML .xlsx 压缩包供上传与沙箱实测"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Transcript" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>'
            '</workbook>',
        )
        row_xml = []
        for r_idx, row in enumerate(rows, start=1):
            cells = []
            for c_idx, val in enumerate(row):
                col = chr(ord("A") + c_idx)
                if isinstance(val, (int, float)):
                    cells.append(f'<c r="{col}{r_idx}"><v>{val}</v></c>')
                else:
                    cells.append(f'<c r="{col}{r_idx}" t="inlineStr"><is><t>{val}</t></is></c>')
            row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml)}</sheetData>'
            '</worksheet>',
        )
    return buf.getvalue()


def test_1_sse_stream_parser():
    sse_lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"First analyze "}}]}\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"the transcript."}}]}\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_py1","function":{"name":"python_","arguments":"{\\"co"}}]}}]}\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"sandbox","arguments":"de\\": \\"print(42)\\"}"}}]}}]}\n',
        b'data: [DONE]\n',
    ]
    events = list(parse_sse_stream_lines(sse_lines))
    r_deltas = [e["text"] for e in events if e["type"] == "reasoning_delta"]
    done_evt = [e for e in events if e["type"] == "turn_done"][0]
    assert "".join(r_deltas) == "First analyze the transcript."
    assert len(done_evt["tool_calls"]) == 1
    tc = done_evt["tool_calls"][0]
    assert tc["id"] == "call_py1"
    assert tc["function"]["name"] == "python_sandbox"
    assert json.loads(tc["function"]["arguments"]) == {"code": "print(42)"}
    print("[PASS] 1. SSE 逐帧思维链流与跨分片 tool_calls JSON 重组验证通过")


def test_2_xlsx_upload_and_python_sandbox():
    xlsx_bytes = _build_minimal_xlsx_bytes([
        ["课程名称", "学分", "绩点"],
        ["数据结构", 4.0, 3.9],
        ["操作系统", 3.5, 3.7],
        ["人工智能导论", 2.5, 4.0],
        ["博约经典导读", 2.0, 3.6],
    ])
    code, _, payload = _InMemoryHTTP.request("POST", "/api/files/upload", {
        "filename": "成绩单_实测.xlsx",
        "content_base64": base64.b64encode(xlsx_bytes).decode("ascii"),
    })
    assert code == 200
    resp = json.loads(payload.decode("utf-8"))
    assert resp["ok"] is True
    assert os.path.isfile(os.path.join(app_run.STORAGE.files_dir, "成绩单_实测.xlsx"))

    # 调用受控 Python 沙箱计算精确加权 GPA: (4*3.9 + 3.5*3.7 + 2.5*4.0 + 2.0*3.6) / 12.0 = 45.75 / 12.0 = 3.8125
    py_res = execute_python_code(
        "rows = load_xlsx_rows('成绩单_实测.xlsx')[1:]\n"
        "tot_c = sum(float(r[1]) for r in rows)\n"
        "w_gpa = sum(float(r[1]) * float(r[2]) for r in rows) / tot_c\n"
        "print(f'CREDITS={tot_c:.1f},GPA={w_gpa:.4f}')\n",
        app_run.STORAGE.files_dir,
        app_run.STORAGE.artifacts_dir,
    )
    assert py_res["ok"] is True, py_res
    assert "CREDITS=12.0,GPA=3.8125" in py_res["stdout"]

    # 验证 AST 安全拦截器
    blocked_1 = execute_python_code("import os\nos.system('id')", app_run.STORAGE.files_dir, app_run.STORAGE.artifacts_dir)
    blocked_2 = execute_python_code("import ctypes", app_run.STORAGE.files_dir, app_run.STORAGE.artifacts_dir)
    assert blocked_1["ok"] is False and "安全策略拦截" in blocked_1["stderr"]
    assert blocked_2["ok"] is False and "安全策略拦截" in blocked_2["stderr"]
    print("[PASS] 2. 真实 Excel 上传 + Python 代码沙箱精确核算 (GPA=3.8125) + AST 安全拦截通过")


def test_3_real_docx_upload_and_fts5():
    real_docx = "/Users/xiaoma/Documents/Project/02华为ict大赛/【赛题2-模板1】创新赛-作品信息收集表-学校-队名-队长姓名-手机号 （初赛阶段）.docx"
    assert os.path.isfile(real_docx), "缺少真实比赛模板 docx 文件"
    raw = open(real_docx, "rb").read()
    code, _, payload = _InMemoryHTTP.request("POST", "/api/files/upload", {
        "filename": os.path.basename(real_docx),
        "content_base64": base64.b64encode(raw).decode("ascii"),
    })
    assert code == 200
    res = json.loads(payload.decode("utf-8"))
    assert res["file"]["char_count"] > 80
    hits = app_run.STORAGE.search_knowledge_fts("华为云 AgentArts 作品信息", top_k=5)
    assert len(hits) >= 1
    print(f"[PASS] 3. 真实比赛 Word 文档解析 ({res['file']['char_count']} 字) 与 SQLite FTS5 检索 ({len(hits)} 切片) 通过")


def test_4_unconfigured_honesty():
    code, _, payload = _InMemoryHTTP.request("POST", "/api/chat/stream", {
        "query": "河南师范大学博约通识学分要求是什么？",
        "session_id": "",
    })
    assert code == 200
    sse_txt = payload.decode("utf-8")
    events = [json.loads(ln[5:].strip()) for ln in sse_txt.splitlines() if ln.startswith("data:")]
    types = [e["type"] for e in events]
    assert "engine_required" in types
    full_reply = "".join(e.get("text", "") for e in events if e["type"] == "content_delta")
    assert "尚未连接大模型推理引擎" in full_reply
    assert "博约" in full_reply  # 本地 FTS5 真实检索直出
    print("[PASS] 4. 未配置 API Key 诚实边界验证通过（零虚假对话模板 + 真实回显本地 FTS5 片段）")


def test_5_multiturn_react_loop_memory_and_artifacts():
    # 配置测试用模型参数并挂载 3 轮真实 ReAct 推演流函数
    _InMemoryHTTP.request("POST", "/api/settings/llm", {
        "api_url": "https://api.deepseek.com/chat/completions",
        "api_key": "sk-test-benchmark-key-9999",
        "model": "deepseek-reasoner",
    })

    call_counter = {"n": 0}

    def _mock_react_stream(cfg, messages, tools):
        call_counter["n"] += 1
        step = call_counter["n"]
        if step == 1:
            yield {"type": "reasoning_delta", "text": "用户需要核算上传的成绩单并保存华为ICT备赛目标，先制定计划并运行Python脚本。"}
            yield {"type": "content_delta", "text": "我先制定计划，然后运行沙箱。"}
            yield {
                "type": "turn_done",
                "content": "我先制定计划，然后运行沙箱。",
                "reasoning": "用户需要核算上传的成绩单并保存华为ICT备赛目标，先制定计划并运行Python脚本。",
                "tool_calls": [
                    {
                        "id": "c_plan",
                        "type": "function",
                        "function": {
                            "name": "plan_tasks",
                            "arguments": json.dumps({
                                "steps": [
                                    {"title": "用 Python 沙箱核算成绩单加权 GPA", "status": "in_progress"},
                                    {"title": "记录长期记忆并生成 HTML 看板与 .ics 日历", "status": "pending"},
                                ]
                            }, ensure_ascii=False),
                        },
                    },
                    {
                        "id": "c_py",
                        "type": "function",
                        "function": {
                            "name": "python_sandbox",
                            "arguments": json.dumps({
                                "purpose": "读取成绩单_实测.xlsx计算GPA",
                                "code": (
                                    "rows = load_xlsx_rows('成绩单_实测.xlsx')[1:]\n"
                                    "gpa = sum(float(r[1])*float(r[2]) for r in rows)/sum(float(r[1]) for r in rows)\n"
                                    "print(f'CALC_GPA={gpa:.4f}')\n"
                                ),
                            }, ensure_ascii=False),
                        },
                    },
                ],
            }
        elif step == 2:
            # 验证上一轮沙箱真实计算结果已回传到 messages 供模型读取
            tool_outputs = " ".join(m.get("content", "") for m in messages if m.get("role") == "tool")
            assert "CALC_GPA=3.8125" in tool_outputs, f"沙箱结果未进入上下文: {tool_outputs}"
            yield {
                "type": "turn_done",
                "content": "",
                "reasoning": "",
                "tool_calls": [
                    {
                        "id": "c_mem",
                        "type": "function",
                        "function": {
                            "name": "memory_store",
                            "arguments": json.dumps({
                                "action": "upsert",
                                "category": "goal",
                                "content": "河南师范大学计算机学院大三，冲刺华为ICT大赛创新赛与保研推免",
                            }, ensure_ascii=False),
                        },
                    },
                    {
                        "id": "c_art_html",
                        "type": "function",
                        "function": {
                            "name": "artifact_create",
                            "arguments": json.dumps({
                                "title": "学业 GPA 审计看板",
                                "kind": "html",
                                "filename": "gpa_dashboard.html",
                                "content": "<!DOCTYPE html><html><body><h1>加权 GPA: 3.8125</h1></body></html>",
                            }, ensure_ascii=False),
                        },
                    },
                    {
                        "id": "c_art_ics",
                        "type": "function",
                        "function": {
                            "name": "artifact_create",
                            "arguments": json.dumps({
                                "title": "华为ICT备赛排期",
                                "kind": "ics",
                                "filename": "ict_sprint.ics",
                                "content": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nSUMMARY:华为ICT初赛作品提交\r\nDTSTART:20261130T100000\r\nDTEND:20261130T120000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
                            }, ensure_ascii=False),
                        },
                    },
                ],
            }
        else:
            for tok in ["已完成沙箱精确核算：", "你的加权 GPA 为 **3.8125**，", "看板与日历已推送至右侧画布。"]:
                yield {"type": "content_delta", "text": tok}
            yield {
                "type": "turn_done",
                "content": "已完成沙箱精确核算：你的加权 GPA 为 **3.8125**，看板与日历已推送至右侧画布。",
                "reasoning": "",
                "tool_calls": [],
            }

    app_run.CUSTOM_STREAM_FN = _mock_react_stream
    try:
        code, _, payload = _InMemoryHTTP.request("POST", "/api/chat/stream", {
            "query": "核算我的成绩单并记录河师大华为ICT备赛目标，生成看板和日历",
            "session_id": "",
        })
        assert code == 200
        evts = [json.loads(ln[5:].strip()) for ln in payload.decode("utf-8").splitlines() if ln.startswith("data:")]
        etypes = {e["type"] for e in evts}
        for req_t in ("memory_updated", "artifact_created", "content_delta", "done"):
            assert req_t in etypes, f"缺少流事件: {req_t}"
        assert etypes.isdisjoint({"reasoning_delta", "plan_updated", "tool_start", "tool_end"})
        assert "制定计划" not in payload.decode("utf-8")
        assert sum(evt["type"] == "content_delta" for evt in evts) == 1

        # 验证长期记忆持久化并触发动态流相关度重算
        _, _, boot_raw = _InMemoryHTTP.request("GET", "/api/bootstrap")
        boot = json.loads(boot_raw.decode("utf-8"))
        assert not any("reasoning" in message or "tool_events" in message for message in boot["messages"])
        assert "3.8125" in boot["messages"][-1]["content"]
        assert "制定计划" not in boot["messages"][-1]["content"]
        assert any("华为ICT大赛" in m["content"] for m in boot["memories"])
        top_feed = boot["feed_items"][0]
        assert top_feed["relevance_score"] >= 80
        assert "关联记忆" in top_feed["relevance_reason"]

        # 验证 HTML 与 .ics 产物下载
        assert len(boot["artifacts"]) == 2
        ics_art = [a for a in boot["artifacts"] if a["kind"] == "ics"][0]
        dl_code, dl_hdrs, dl_bytes = _InMemoryHTTP.request("GET", f"/api/artifacts/download?artifact_id={ics_art['artifact_id']}")
        assert dl_code == 200
        assert "text/calendar" in dl_hdrs
        assert b"BEGIN:VCALENDAR" in dl_bytes
    finally:
        app_run.CUSTOM_STREAM_FN = None

    print("[PASS] 5. 三轮自主 ReAct 循环（规划->沙箱->记忆写入->双画布产物->动态流重排）全链路验证通过")


def test_6_skills_and_bidirectional_mcp():
    # 1) 技能包保存与启停
    code, _, payload = _InMemoryHTTP.request("POST", "/api/skills", {
        "action": "save",
        "slug": "paper-reviewer",
        "title": "论文结构审阅技能",
        "description": "检查实验消融与引用规范",
        "enabled": True,
        "body": "审阅论文时重点核查 Baseline 对比表。",
    })
    assert code == 200
    sk_list = json.loads(payload.decode("utf-8"))["skills"]
    assert any(s["slug"] == "paper-reviewer" for s in sk_list)

    # 2) 对外 MCP Server (POST /mcp) + 对内 MCPClient 自环挂载验证
    def _loopback_mcp_transport(rpc_req):
        _, _, resp_bytes = _InMemoryHTTP.request("POST", "/mcp", rpc_req)
        return json.loads(resp_bytes.decode("utf-8"))

    # 直接测试 POST /mcp tools/call -> python_sandbox
    mcp_call_resp = _loopback_mcp_transport({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {"name": "python_sandbox", "arguments": {"code": "print(7 * 8)"}},
    })
    inner_json = json.loads(mcp_call_resp["result"]["content"][0]["text"])
    assert inner_json["ok"] is True and "56" in inner_json["stdout"]

    # 测试通过 /api/mcp_servers 挂载外部 MCP Server 并自动注入 Agent 工具箱
    app_run.CUSTOM_MCP_TRANSPORT = _loopback_mcp_transport
    try:
        code, _, payload = _InMemoryHTTP.request("POST", "/api/mcp_servers", {
            "action": "add",
            "name": "AgentArts-Loopback",
            "transport": "http",
            "target": "http://127.0.0.1:8765/mcp",
        })
        res = json.loads(payload.decode("utf-8"))
        assert res["ok"] is True and res["discovered_count"] == 8
        merged_schema = build_merged_tools_schema(app_run.STORAGE)
        merged_names = [t["function"]["name"] for t in merged_schema]
        assert len(merged_names) == 9 + 8  # 9 内置 + 8 外部动态注入
    finally:
        app_run.CUSTOM_MCP_TRANSPORT = None

    print("[PASS] 6. 可插拔 Markdown 技能包与双向 MCP (8 原语暴露 + 外部工具动态合并) 验证通过")


def test_7_web_parsers_and_frontend_audit():
    # 搜索与网页正文提取单元测试
    bing_sample = '<ol id="b_results"><li class="b_algo"><h2><a href="https://www.htu.edu.cn/teaching/3251/list.htm">河南师范大学教务处通知</a></h2><div class="b_caption"><p>最新博约通识选课公告</p></div></li></ol>'
    b_res = parse_bing_search_html(bing_sample)
    assert len(b_res) == 1 and b_res[0]["url"].startswith("https://www.htu.edu.cn")

    ddg_sample = '<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe.huawei.com%2Fcn%2Ftalent">华为ICT大赛官网</a><a class="result__snippet">中国区创新赛赛题二</a></div>'
    d_res = parse_ddg_search_html(ddg_sample)
    assert len(d_res) == 1 and d_res[0]["url"] == "https://e.huawei.com/cn/talent"

    page_html = (
        '<html><head><title>关于做好2026年度通知</title></head><body>'
        '<div class="wp_articlecontent"><p>请各学院于12月5日前提交申报表。</p>'
        '<a href="/_upload/article/files/apply_form.docx">附件1：申报表.docx</a></div></body></html>'
    )
    extracted = extract_webpage_content("https://www.htu.edu.cn/cs/2026/1125/page.htm", page_html)
    assert "12月5日前" in extracted["content"]
    assert len(extracted["attachments"]) == 1 and extracted["attachments"][0]["url"].endswith("apply_form.docx")
    menu = "".join(f'<a href="/nav/{i}">导航栏目{i}</a>' for i in range(50))
    notice = '<a href="/notice/1">软件学院竞赛报名通知</a>'
    listed = extract_webpage_content("https://www.htu.edu.cn/sw/", menu + notice, "软件学院 竞赛")
    assert listed["links"][0]["url"].endswith("/notice/1")

    rss_xml = '<rss version="2.0"><channel><item><title>Agent Runtime Release</title><link>https://example.com/1</link><description>New MCP bridge</description></item></channel></rss>'
    assert len(parse_rss_or_atom(rss_xml, "s1", "Tech")) == 1

    # 前端零 Emoji、零假人设、JavaScriptCore 语法严格校验
    fe_path = os.path.join(PROJECT_DIR, "frontend", "index.html")
    fe_text = open(fe_path, encoding="utf-8").read()

    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u26FF\u2700-\u2712\u2714-\u27BF]")
    emoji_hits = emoji_re.findall(fe_text)
    assert not emoji_hits, f"前端检测到违禁 Emoji 字符: {emoji_hits}"

    for fake_name in ("林知夏", "陈宇航", "周思源"):
        assert fake_name not in fe_text, f"前端仍残留假学生数据: {fake_name}"

    scripts = re.findall(r"<script>([\s\S]*?)</script>", fe_text)
    assert len(scripts) == 1
    jsc_bin = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
    if os.path.isfile(jsc_bin):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
            # 用 Function 构造器仅做严格语法编译检查而不执行浏览器 DOM
            tf.write("new Function(" + json.dumps(scripts[0]) + ");\n")
            js_tmp = tf.name
        try:
            proc = subprocess.run([jsc_bin, js_tmp], capture_output=True, text=True, timeout=8)
            assert proc.returncode == 0, f"前端 JS 语法错误: {proc.stderr} {proc.stdout}"
        finally:
            os.remove(js_tmp)

    print("[PASS] 7. 全网搜索/网页提取器 + 前端零 Emoji / 零假人设 / JSC 语法编译校验全部通过")


def test_8_search_budgets_and_followup():
    question = "河南师范大学软件学院近期有哪些竞赛通知？"
    source_url = "https://example.org/notices/ict"
    answer = f"已核实 1 条竞赛通知：[校内 ICT 竞赛通知]({source_url})。其他竞赛暂未核实。"
    calls = {"web_search": [], "web_fetch": []}
    model_turns = 0

    def model(_cfg, messages, tools):
        nonlocal model_turns
        model_turns += 1
        if messages[-1]["content"].startswith("继续"):
            assert question in messages[-1]["content"]
            assert source_url in messages[-1]["content"]
            yield {"type": "turn_done", "content": "接着上一问回答，当前有一条竞赛通知。"}
            return
        if tools is None:
            assert source_url in messages[-1]["content"]
            yield {"type": "turn_done", "content": "<think>内部推理</think>" + answer}
            return
        requests = [
            ("web_search", {"query": "河师大 竞赛", "max_results": 10}),
            ("web_search", {"query": "竞赛  河师大", "max_results": 2}),
            ("web_search", {"query": "软件学院 竞赛"}),
            ("web_search", {"query": "学校 竞赛 报名"}),
            ("web_fetch", {"url": source_url}),
            ("web_fetch", {"url": source_url + "#section"}),
            ("web_fetch", {"url": source_url + "/1"}),
            ("web_fetch", {"url": source_url + "/2"}),
            ("web_fetch", {"url": source_url + "/3"}),
        ]
        yield {"type": "content_delta", "text": "我来检索比赛。"}
        yield {
            "type": "turn_done",
            "tool_calls": [
                {"id": f"web_{index}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for index, (name, args) in enumerate(requests)
            ]
        }

    def search(query, max_results, **_):
        calls["web_search"].append((query, max_results))
        return {"ok": True, "engine": "test", "results": [{"title": "校内 ICT 竞赛通知", "url": source_url}]}

    def fetch(url, **_):
        calls["web_fetch"].append(url)
        return {"ok": True, "title": "校内 ICT 竞赛通知", "url": url,
                "content": "比赛报名公告正文", "links": []}

    def send(query, session_id=""):
        code, _, body = _InMemoryHTTP.request("POST", "/api/chat/stream", {"query": query, "session_id": session_id})
        assert code == 200
        return [json.loads(line[5:]) for line in body.decode().splitlines() if line.startswith("data:")]

    app_run.CUSTOM_STREAM_FN = model
    try:
        with patch("backend.runtime.agent_loop.web_search", search), patch("backend.runtime.agent_loop.web_fetch", fetch):
            events = send(question)
            session_id = events[-1]["session_id"]
            assert [e["text"] for e in events if e["type"] == "content_delta"] == [answer]
            assert events[-1]["type"] == "done"
            assert sorted(calls["web_search"]) == [("河师大 竞赛", 5), ("软件学院 竞赛", 5)]
            assert sorted(calls["web_fetch"]) == [source_url, source_url + "/1", source_url + "/2"]
            assert model_turns == 3
            events = send("继续", session_id)
            assert events[-1]["session_id"] == session_id
            assert any(e.get("text") == "接着上一问回答，当前有一条竞赛通知。" for e in events)
            assert model_turns == 4

        def no_answer(*_):
            yield {"type": "turn_done", "content": "<think>无法生成</think>"}

        app_run.CUSTOM_STREAM_FN = no_answer
        events = send("结果呢？", session_id)
        assert any(source_url in e.get("text", "") and "未能核实" in e["text"] for e in events)
        assert events[-1]["type"] == "done"
    finally:
        app_run.CUSTOM_STREAM_FN = None

    old = app_run.STORAGE.create_session()["session_id"]
    app_run.STORAGE.append_message(
        old, "assistant",
        "我来检索河南师范大学软件学院的竞赛。\n\n首页没有结果，我再抓取动态栏目。\n\n本周有一项校内比赛。",
        reasoning="历史私有推理", tool_events=[{"name": "web_fetch", "ok": True, "detail": {"url": source_url}}],
    )
    _, _, history = _InMemoryHTTP.request("GET", f"/api/sessions/messages?session_id={old}")
    message = json.loads(history)["messages"][0]
    assert message["content"] == "本周有一项校内比赛。"
    assert "私有推理" not in history.decode()
    print("[PASS] 8. 普通搜索、抓取上限与去重，追问沿用原问题，空结果兜底及历史清理")


def test_9_search_timeout_and_model_upgrade():
    from backend.runtime.web_tools import _score_and_filter_results, web_search

    legit = {"title": "河南师范大学软件学院竞赛", "url": "https://www.htu.edu.cn/sw/notice", "snippet": ""}
    unrelated = {"title": "河南竞赛与景区", "url": "https://example.org/", "snippet": "河南体育竞赛"}
    assert _score_and_filter_results("河南师范大学 软件学院 竞赛", [legit, unrelated], 6) == [legit]

    def slow_request(*_, **__):
        time.sleep(0.4)
        raise OSError("slow upstream")

    with patch("backend.runtime.web_tools._http_get", slow_request):
        start = time.monotonic()
        res = web_search("软件学院 竞赛", timeout=0.08)
        assert not res["ok"] and time.monotonic() - start < 0.3

    class Response(io.BytesIO):
        headers = {"Content-Type": "application/json"}

    observed = []

    def _fake_upstream(request, **_):
        observed.append(json.loads(request.data))
        return Response(b'{"choices":[{"message":{"content":"ok"}}]}')

    with patch("backend.runtime.llm_client._open_llm_request", _fake_upstream):
        cfg = {"api_url": "https://api.deepseek.com/chat/completions", "model": "deepseek-chat"}
        list(stream_chat_completion(cfg, []))
        list(stream_chat_completion(cfg, [], deep=True))
        cfg = {"api_url": "https://compat.example.org/chat/completions", "model": "deepseek-chat"}
        list(stream_chat_completion(cfg, []))

    assert observed[0]["model"] == "deepseek-flash" and observed[0]["thinking"]["type"] == "disabled"
    assert observed[1]["thinking"]["type"] == "enabled"
    assert observed[2]["model"] == "deepseek-chat" and "thinking" not in observed[2]
    print("[PASS] 9. 搜索引擎共享总预算、排除弱匹配，兼容 DeepSeek 官方旧模型配置")


def test_10_team_private_chat():
    invite = Invite("test-code-only-12345", "independent-signing-key-for-test")
    handler = create_handler(app_run.AppHandler)
    public_origin = "https://zhixing-preview.example"
    server = SimpleNamespace(invite=invite, data_dir=Path(TEST_WS) / "team",
                             public_origin=public_origin, chat_slots=BoundedSemaphore(4))

    def request(method, path, body=None, cookie="", **kwargs):
        headers = {"Cookie": cookie} if cookie else {}
        headers.update(kwargs.pop("headers", {}))
        return _InMemoryHTTP.request(method, path, body, handler_class=handler, server=server, headers=headers, **kwargs)

    assert request("GET", "/")[0] == 303
    assert request("GET", "/api/bootstrap")[0] == 401
    assert "Referrer-Policy: same-origin" in request("GET", "/login")[1]
    browser_headers = {"Origin": public_origin, "Sec-Fetch-Site": "same-origin"}
    assert request("POST", "/login", raw_body=b"code=wrong", headers=browser_headers)[0] == 401
    assert request("POST", "/login", raw_body=b"code=wrong",
                   headers={"Origin": "https://other.example", "X-Forwarded-Host": "other.example"})[0] == 403

    def login():
        status, headers, _ = request("POST", "/login", raw_body=b"code=test-code-only-12345", headers=browser_headers)
        assert status == 303
        cookie = re.search(r"Set-Cookie: (zhixing_team=[^;]+);.*Secure; HttpOnly; SameSite=Lax", headers)
        assert cookie
        return cookie.group(1)

    alice = login()
    bob = login()
    assert alice != bob
    assert request("GET", "/api/bootstrap", cookie=alice + "bad")[0] == 401
    _, _, data = request("GET", "/api/bootstrap", cookie=alice)
    boot = json.loads(data)
    assert boot["llm"]["team_mode"] and "masked_key" not in boot["llm"]
    session_id = boot["active_session_id"]
    query = {"session_id": session_id, "query": "这里是浏览器 A 的私人测试问题"}
    assert request("POST", "/api/chat/stream", query, bob)[0] == 404
    assert request("POST", "/api/settings/llm", {"model": "test"}, alice)[0] == 404
    assert request("POST", "/mcp", {"id": 1}, alice)[0] == 404
    assert request("POST", "/api/chat/stream", query, alice, headers={"Origin": "https://other.example"})[0] == 403

    def model(_cfg, messages, tools):
        assert {t["function"]["name"] for t in tools} == {"search_knowledge", "web_search"}
        yield {"type": "turn_done", "content": "这是 A 浏览器的答复"}

    app_run.CUSTOM_STREAM_FN = model
    try:
        status, _, stream = request("POST", "/api/chat/stream", query, alice, headers=browser_headers)
        assert status == 200 and "这是 A 浏览器的答复" in stream.decode()
    finally:
        app_run.CUSTOM_STREAM_FN = None

    _, _, alice_history = request("GET", f"/api/sessions/messages?session_id={session_id}", cookie=alice)
    _, _, bob_history = request("GET", f"/api/sessions/messages?session_id={session_id}", cookie=bob)
    assert len(json.loads(alice_history)["messages"]) == 2
    assert json.loads(bob_history)["messages"] == []
    print("[PASS] 10. 组内邀请码、签名会话隔离、受限工具和接口、问答及历史读取")


def test_11_product_registration_isolation_and_score():
    handler = create_product_handler(app_run.AppHandler)
    server = SimpleNamespace()
    origin = "https://study.example"
    configure_server(server, Path(TEST_WS) / "product", {}, public_origin=origin, secure=True,
                     registration_code="test-registration-code", chat_limit=1)
    password = "correct horse battery staple"

    def request(method, path, body=None, cookie="", browser_origin=origin, raw_body=None):
        headers = {"Origin": browser_origin} if browser_origin else {}
        if cookie:
            headers["Cookie"] = cookie
        return _InMemoryHTTP.request(method, path, body, handler_class=handler, server=server,
                                     headers=headers, raw_body=raw_body)

    def form(path, **fields):
        return request("POST", path, raw_body=urlencode(fields).encode())

    def cookie_from(headers):
        found = re.search(r"Set-Cookie: (zhixing_account=[^;]+); .*HttpOnly; SameSite=Lax; .*Secure", headers)
        assert found, headers
        return found.group(1)

    assert request("GET", "/")[0] == 303
    assert request("GET", "/api/bootstrap")[0] == 401
    assert "创建账号" in request("GET", "/register")[2].decode()
    assert form("/register", username="alice", password="short", invite="test-registration-code")[0] == 400
    assert form("/register", username="alice", password=password, invite="wrong")[0] == 403
    status, headers, _ = form("/register", username="alice", password=password, invite="test-registration-code")
    assert status == 303
    first = cookie_from(headers)
    status, headers, _ = form("/register", username="bob", password=password, invite="test-registration-code")
    assert status == 303
    second = cookie_from(headers)
    assert first != second
    assert form("/login", username="alice", password="incorrect password 123")[0] == 401
    assert request("GET", "/api/bootstrap", cookie=first + "tampered")[0] == 401

    _, _, bootstrap_payload = request("GET", "/api/bootstrap", cookie=first)
    boot = json.loads(bootstrap_payload)
    assert boot["llm"]["username"] == "alice" and boot["llm"]["product_mode"]
    assert boot["skills"] == [] and "masked_key" not in boot["llm"]
    sid = boot["active_session_id"]

    marker = "翠湖专属测试资料只属于 Alice"
    body = {"filename": "校园研修资料.md", "content_text": "# 翠湖研修\n" + marker}
    status, _, upload = request("POST", "/api/files/upload", body, first)
    assert status == 200
    document = json.loads(upload)["file"]
    assert "disk_path" not in document
    upload_route = "/api/files/download?file_id=" + document["file_id"]
    assert marker in request("GET", upload_route, cookie=first)[2].decode()
    assert request("GET", upload_route, cookie=second)[0] == 404
    assert request("POST", "/api/files/upload", {"filename": "blocked.py", "content_text": "hello"}, first)[0] == 400
    assert request("POST", "/api/files/upload", {"filename": "bad.docx", "content_base64": "abcd"}, first)[0] == 400
    assert request("POST", "/api/files/upload", {"filename": ".txt", "content_text": " "}, first)[0] == 400
    assert request("POST", "/api/settings/llm", {"model": "other"}, first)[0] == 404
    assert request("POST", "/mcp", {"id": 1}, first)[0] == 404

    score = {"rows": "学分,成绩\n3\t92\n2，85\n1.5,88"}
    assert request("POST", "/api/study/weighted-score", score, first, "https://other.example")[0] == 403
    status, _, result = request("POST", "/api/study/weighted-score", score, first)
    assert status == 200
    assert json.loads(result) == {"ok": True, "course_count": 3, "credits": "6.5", "weighted_score": "88.92"}
    assert request("POST", "/api/study/weighted-score", {"rows": "0,100"}, first)[0] == 400
    assert request("POST", "/api/study/weighted-score", {"rows": "3,,100"}, first)[0] == 400
    assert request("POST", "/api/study/weighted-score", {"rows": "3,101"}, first)[0] == 400

    query = {"session_id": sid, "query": "翠湖研修有什么资料？"}

    def model(_cfg, messages, tools):
        assert {t["function"]["name"] for t in tools} == {"web_search", "search_knowledge", "memory_recall"}
        if messages[-1]["role"] != "tool":
            yield {"type": "turn_done", "tool_calls": [
                {"id": "fts", "type": "function",
                 "function": {"name": "search_knowledge", "arguments": '{"query":"翠湖研修"}'}}
            ]}
        else:
            assert marker in messages[-1]["content"] and "校园研修资料.md" in messages[-1]["content"]
            yield {"type": "turn_done", "content": "已从《校园研修资料.md》找到相关内容。"}

    app_run.CUSTOM_STREAM_FN = model
    try:
        status, _, stream = request("POST", "/api/chat/stream", query, first)
        assert status == 200 and "已从《校园研修资料.md》" in stream.decode()
        assert request("POST", "/api/chat/stream", query, first)[0] == 429
        assert request("POST", "/api/chat/stream", query, second)[0] == 404
    finally:
        app_run.CUSTOM_STREAM_FN = None

    assert request("POST", "/logout", cookie=first, raw_body=b"action=logout")[0] == 303
    assert request("GET", "/api/bootstrap", cookie=first)[0] == 401
    configure_server(server, Path(TEST_WS) / "product", {}, public_origin=origin, secure=True,
                     registration_code="test-registration-code", chat_limit=1)
    assert request("GET", "/api/bootstrap", cookie=first)[0] == 401
    status, headers, _ = form("/login", username="ALICE", password=password)
    assert status == 303
    reauthenticated = cookie_from(headers)
    _, _, data = request("GET", f"/api/sessions/messages?session_id={sid}", cookie=reauthenticated)
    assert len(json.loads(data)["messages"]) == 2
    _, _, data = request("GET", f"/api/sessions/messages?session_id={sid}", cookie=second)
    assert json.loads(data)["messages"] == []
    print("[PASS] 11. 产品账号、跨账号私有资料和会话、检索问答、成绩精算、限额与退出")


def test_12_academic_engine_audit_what_if_and_ics():
    code, _, raw = _InMemoryHTTP.request("GET", "/api/academic/catalog")
    assert code == 200
    cat = json.loads(raw.decode("utf-8"))
    assert cat["ok"] is True and len(cat["curriculum_modules"]) == 8 and len(cat["competitions"]) >= 6
    assert "agentarts_topology" in cat and len(cat["agentarts_topology"]["agents"]) == 4

    # 1) 确定性成绩单解析 + 160学分/博约通识审计 + 机会资格自动对账
    sample_rows = (
        "课程名称,学分,成绩,课程模块\n"
        "数据结构,4.0,74,专业必修\n"
        "操作系统,3.5,88,专业必修\n"
        "人工智能导论,2.5,93,专业选修\n"
        "博约经典：哲学导读,2.0,90,博约经典\n"
        "博约核心：科学伦理,4.0,86,博约核心\n"
    )
    code, _, raw = _InMemoryHTTP.request("POST", "/api/academic/analyze", {
        "rows": sample_rows,
        "grade": 2024,
        "cet4": 540,
        "cet6": 460,
        "volunteer_hours": 24,
        "moral_score": 88,
        "innovation_raw": 6,
        "target_direction": "保研",
    })
    assert code == 200, raw
    res = json.loads(raw.decode("utf-8"))
    assert res["ok"] is True
    summary = res["audit"]["summary"]
    # 加权分验证: (4*74 + 3.5*88 + 2.5*93 + 2*90 + 4*86) / 16.0 = 1360.5 / 16.0 = 85.03125 -> 85.03
    assert summary["weighted_avg"] == 85.03, summary
    assert summary["passed_credits"] == 16.0
    assert summary["boyue_earned"] == 6.0 and summary["boyue_gap"] == 12.0
    # 博约百花为 0 学分，必触发 danger 预警
    assert any("百花" in a["title"] and a["level"] == "danger" for a in res["audit"]["alerts"])
    assert res["opportunities"]["counts"]["eligible"] >= 1

    # 2) What-If 重修 (0.3+0.7 折算) vs 华为ICT大赛 ROI 推演
    code, _, raw = _InMemoryHTTP.request("POST", "/api/academic/what-if", {
        "current_weighted_avg": 85.03,
        "total_credits": 90.0,
        "moral_score": 88.0,
        "current_innovation_raw": 6.0,
        "retake_course_name": "数据结构",
        "retake_credits": 4.0,
        "retake_old_score": 74.0,
        "retake_target_score": 92.0,
        "retake_est_hours": 60.0,
        "selected_competition_ids": ["huawei_ict_prov1", "huawei_ict_nat1", "huawei_hcip"],
    })
    assert code == 200
    wi = json.loads(raw.decode("utf-8"))
    assert wi["ok"] is True and len(wi["scenarios"]) >= 5
    retake_sc = next(s for s in wi["scenarios"] if s["id"] == "retake_course")
    # 有效新成绩 = 74*0.3 + 92*0.7 = 86.60
    assert "86.60" in retake_sc["detail"], retake_sc
    ict_nat1 = next(s for s in wi["scenarios"] if s["id"] == "huawei_ict_nat1")
    assert ict_nat1["zongce_gain"] == 3.6

    # 3) RFC 5545 .ics 导出 + HTML 体检看板产物 + 长期记忆同步
    code, _, raw = _InMemoryHTTP.request("POST", "/api/academic/export-ics", {
        "title": "第十一届华为ICT大赛冲刺",
        "deadline": "2026-12-15 18:00",
        "prep_tasks": [{"step": "录制AgentArts演示视频", "est_hours": 2.0, "days_before_deadline": 2}],
        "required_materials": ["作品信息收集表Word"],
    })
    assert code == 200
    ics_res = json.loads(raw.decode("utf-8"))
    assert ics_res["ok"] is True and "BEGIN:VCALENDAR" in ics_res["ics"] and "BEGIN:VALARM" in ics_res["ics"]
    assert ics_res["artifact"] and ics_res["artifact"]["kind"] == "ics"

    code, _, raw = _InMemoryHTTP.request("POST", "/api/academic/save-report", {
        "audit": res["audit"],
        "what_if": wi,
    })
    assert code == 200
    rep_res = json.loads(raw.decode("utf-8"))
    assert rep_res["ok"] is True and rep_res["artifact"]["kind"] == "html"
    assert "知行" in rep_res["artifact"]["content"] and "85.03" in rep_res["artifact"]["content"]

    code, _, raw = _InMemoryHTTP.request("POST", "/api/academic/sync-memory", {"summary": summary})
    assert code == 200
    sync_res = json.loads(raw.decode("utf-8"))
    assert sync_res["ok"] is True and any("85.03" in m["content"] for m in sync_res["memories"])
    print("[PASS] 12. 确定性学业体检、博约漏修预警、What-If ROI 沙盘、.ics 日历与 HTML 看板全闭环通过")


def test_13_knowledge_playground_and_session_export():
    # 1) 本地 FTS5 BM25 检索台接口
    code, _, raw = _InMemoryHTTP.request("POST", "/api/knowledge/search", {"query": "博约通识 经典 核心", "top_k": 4})
    assert code == 200
    kres = json.loads(raw.decode("utf-8"))
    assert kres["ok"] is True and len(kres["hits"]) >= 1
    assert any("博约" in h["content"] for h in kres["hits"])

    # 2) 会话 Markdown 导出
    sess = app_run.STORAGE.create_session("学业规划导出测试")
    app_run.STORAGE.append_message(sess["session_id"], "user", "我距离推免线还差多少？")
    app_run.STORAGE.append_message(sess["session_id"], "assistant", "当前预估综测 85.40，建议冲刺华为ICT大赛。")
    code, hdrs, raw = _InMemoryHTTP.request("GET", f"/api/sessions/export?session_id={sess['session_id']}")
    assert code == 200 and "text/markdown" in hdrs
    exported = raw.decode("utf-8")
    assert "学业规划导出测试" in exported and "华为ICT大赛" in exported
    print("[PASS] 13. 本地 FTS5 知识检索实验台与会话 Markdown 导出验证通过")


def test_14_account_template_and_product_academic_isolation():
    acct_html = (Path(PROJECT_DIR) / "frontend" / "account.html").read_text(encoding="utf-8")
    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u26FF\u2700-\u2712\u2714-\u27BF]")
    assert not emoji_re.findall(acct_html), "account.html 检测到违禁 Emoji"
    rendered = acct_html.format_map({
        "title": "登录", "action": "/login", "alternate": "",
        "password_autocomplete": "current-password", "message": "",
        "register_code": "", "data_hint": "",
    })
    assert "知行" in rendered and "{title}" not in rendered

    # 验证产品模式下已登录账号可使用确定性学业体检与沙盘，且严格保持 Origin 校验与高危接口隔离
    handler = create_product_handler(app_run.AppHandler)
    server = SimpleNamespace()
    origin = "https://study.example"
    configure_server(server, Path(TEST_WS) / "product_v2", {}, public_origin=origin, secure=True)
    status, headers, _ = _InMemoryHTTP.request(
        "POST", "/register", handler_class=handler, server=server,
        headers={"Origin": origin},
        raw_body=urlencode({"username": "student01", "password": "a very secure passphrase"}).encode(),
    )
    assert status == 303
    cookie = re.search(r"Set-Cookie: (zhixing_account=[^;]+);", headers).group(1)
    code, _, raw = _InMemoryHTTP.request(
        "POST", "/api/academic/analyze",
        {"rows": "课程,学分,成绩,模块\n高等数学,5,90,专业必修\n博约经典,2,88,博约经典"},
        handler_class=handler, server=server,
        headers={"Origin": origin, "Cookie": cookie},
    )
    assert code == 200 and json.loads(raw)["audit"]["summary"]["weighted_avg"] == 89.43
    # 跨站 Origin 仍被 403 拒绝
    code, _, _ = _InMemoryHTTP.request(
        "POST", "/api/academic/analyze", {"rows": "3,90"},
        handler_class=handler, server=server,
        headers={"Origin": "https://evil.example", "Cookie": cookie},
    )
    assert code == 403
    print("[PASS] 14. 登录模板审计与产品模式学业精算/安全隔离验证通过")


if __name__ == "__main__":
    try:
        test_1_sse_stream_parser()
        test_2_xlsx_upload_and_python_sandbox()
        test_3_real_docx_upload_and_fts5()
        test_4_unconfigured_honesty()
        test_5_multiturn_react_loop_memory_and_artifacts()
        test_6_skills_and_bidirectional_mcp()
        test_7_web_parsers_and_frontend_audit()
        test_8_search_budgets_and_followup()
        test_9_search_timeout_and_model_upgrade()
        test_10_team_private_chat()
        test_11_product_registration_isolation_and_score()
        test_12_academic_engine_audit_what_if_and_ics()
        test_13_knowledge_playground_and_session_export()
        test_14_account_template_and_product_academic_isolation()
        print("\n=== ALL 14 AGENT RUNTIME BENCHMARKS PASSED ===")
    finally:
        shutil.rmtree(TEST_WS, ignore_errors=True)
