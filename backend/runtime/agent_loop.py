# -*- coding: utf-8 -*-
"""
知行 Agent Runtime — 市场级通用自主规划 ReAct 循环引擎
多轮 Tool Calling、限时联网检索与最终答复生成。
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
import re
import time
from typing import Any, Callable, Collection, Dict, Generator, List, Optional
from urllib.parse import urldefrag, urlsplit

from .llm_client import is_llm_configured, resolve_model, stream_chat_completion
from .mcp_bridge import MCPClient
from .sandbox import execute_python_code
from .skills import SkillManager
from .storage import RuntimeStorage, clean_answer_text, now_str
from .web_tools import normalize_http_url, sync_all_sources, web_fetch, web_search


MAX_AGENT_STEPS = 8
_WEB_WORKERS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="zhixing-web")
_DEEP_REQUEST = re.compile(r"深入调研|深度调研|深度检索|深度研究|全面检索|逐页核验|系统性调研|\bdeep research\b", re.I)
_CONTINUATION = re.compile(
    r"^(?:(?:请|帮我|那就)?(?:继续|接着)(?:查|找|搜|回答|核实|上一个问题)?"
    r"|结果(?:呢|如何|出来了吗)|刚才查到什么|然后呢|上一问呢)[\s，。！？,.!?]*$"
)
_WEB_TOOLS = {"web_search", "web_fetch"}


def build_builtin_tools_schema() -> List[Dict[str, Any]]:
    """构建标准 OpenAI Function Calling 规范的 9 个通用智能体原语工具"""
    return [
        {
            "type": "function",
            "function": {
                "name": "plan_tasks",
                "description": "面对复杂多步骤调研、数据分析或创作任务时，制定或更新有序执行计划列表（简单问答无需调用）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "description": "完整步骤列表",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string", "description": "简明步骤标题"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "done"],
                                        "description": "步骤当前状态",
                                    },
                                },
                                "required": ["title", "status"],
                            },
                        }
                    },
                    "required": ["steps"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "全网实时搜索公开网页、高校通知、百科词条与技术资料。请使用 2～4 个精炼核心关键词。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "精炼搜索关键词（如 '河南师范大学 软件学院 专业'）"},
                        "max_results": {"type": "integer", "description": "返回结果条数（默认 6）"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "实时抓取指定 HTTP/HTTPS URL（支持含中文 URL，如百度百科），返回页面正文 content、页面内真实导航链接列表 links 以及文档附件 attachments。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "完整网页 URL"},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "python_sandbox",
                "description": (
                    "在受控 Python 3 代码解释器沙箱中运行脚本并获取 stdout 输出。"
                    "沙箱当前目录即为工作区文件目录 FILES_DIR，且已预置辅助函数："
                    "load_xlsx_rows(filename) 直接返回 Excel 所有非空行二维列表，"
                    "load_docx_text(filename) 直接读取 Word 文档纯文本。"
                    "涉及成绩核算、GPA 加权统计、数据清洗或复杂计算时必须调用本工具，严禁心算。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "完整可运行的 Python 3 脚本代码（用 print 输出关键结果）"},
                        "purpose": {"type": "string", "description": "本次运行脚本的目的简述"},
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "在本地 SQLite FTS5 全文索引中检索用户上传的文件切片与内置参考知识文档。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词或短语"},
                        "top_k": {"type": "integer", "description": "返回最相关切片数（默认 6）"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_store",
                "description": "向用户长期记忆库写入、更新或删除个人背景(profile)、长期目标(goal)、偏好习惯(preference)或关键事实(fact)。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["upsert", "delete"]},
                        "category": {"type": "string", "enum": ["profile", "goal", "preference", "fact"]},
                        "content": {"type": "string", "description": "简明准确的记忆陈述句"},
                        "memory_id": {"type": "string", "description": "更新或删除已有条目时的 ID（新增可留空）"},
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_recall",
                "description": "按关键词检索用户的长期记忆库条目。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "可选检索关键词，留空返回全部核心记忆"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "artifact_create",
                "description": (
                    "在独立交互画布（Artifact Canvas）中创建持久化交付物。"
                    "支持类型：html（自包含内联 CSS/JS/SVG 的交互式数据看板或可视化应用）、"
                    "markdown（深度调研报告/方案书）、svg（矢量图表）、ics（标准 RFC 5545 日历文件，可导入手机/电脑日历）、"
                    "csv（结构化表格导出）、code（完整源代码文件）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "产物标题"},
                        "kind": {"type": "string", "enum": ["html", "markdown", "svg", "ics", "csv", "code"]},
                        "content": {"type": "string", "description": "完整的产物文件内容"},
                        "filename": {"type": "string", "description": "可选导出文件名，如 dashboard.html 或 schedule.ics"},
                    },
                    "required": ["title", "kind", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "feed_subscribe",
                "description": "为用户右侧实时动态流新增自定义监控订阅源。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "订阅源显示名称"},
                        "kind": {
                            "type": "string",
                            "enum": ["web_list", "rss", "github", "arxiv", "search_query"],
                            "description": "连接器类型",
                        },
                        "target": {"type": "string", "description": "URL 地址或检索表达式"},
                    },
                    "required": ["name", "kind", "target"],
                },
            },
        },
    ]


def build_merged_tools_schema(storage: RuntimeStorage) -> List[Dict[str, Any]]:
    """合并内置原语工具与外部已挂载 MCP Server 的动态工具"""
    tools = build_builtin_tools_schema()
    for srv in storage.get_mcp_servers():
        if not srv.get("enabled", True):
            continue
        sid = re.sub(r"[^a-zA-Z0-9_]", "_", str(srv.get("server_id", "ext")))
        sname = srv.get("name", sid)
        for t in srv.get("tools") or []:
            tname = t.get("name")
            if not tname:
                continue
            safe_tname = re.sub(r"[^a-zA-Z0-9_\-]", "_", tname)
            tools.append({
                "type": "function",
                "function": {
                    "name": f"mcp__{sid}__{safe_tname}",
                    "description": f"[外部 MCP: {sname}] {t.get('description', '')}".strip(),
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
    return tools


def execute_runtime_tool(
    tool_name: str,
    args: Dict[str, Any],
    storage: RuntimeStorage,
    session_id: str = "",
    mcp_transport_override: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    web_timeout: int = 8,
    related_query: str = "",
) -> Dict[str, Any]:
    """统一执行运行时内置原语或外部 MCP 工具"""
    if tool_name == "plan_tasks":
        raw_steps = args.get("steps") or []
        norm_steps = []
        for s in raw_steps[:10]:
            if isinstance(s, dict) and s.get("title"):
                st = s.get("status", "pending")
                norm_steps.append({
                    "title": str(s["title"])[:80],
                    "status": st if st in ("pending", "in_progress", "done") else "pending",
                })
        done_cnt = sum(1 for s in norm_steps if s["status"] == "done")
        return {
            "ok": True,
            "summary": f"已规划 {len(norm_steps)} 个执行步骤（已完成 {done_cnt}/{len(norm_steps)}）",
            "steps": norm_steps,
            "detail": {"steps": norm_steps},
            "llm_payload": {"ok": True, "steps": norm_steps},
        }

    if tool_name == "web_search":
        q = str(args.get("query", "")).strip()
        max_r = int(args.get("max_results", 6) or 6)
        skey = storage.get_llm_config().get("search_api_key", "")
        res = web_search(q, max_results=min(10, max(1, max_r)), search_api_key=skey, timeout=web_timeout)
        cnt = len(res.get("results", []))
        summary = (
            f"通过 {res['engine']} 检索「{q}」，命中 {cnt} 条高相关结果"
            if res["ok"]
            else f"检索「{q}」未命中高相关公开页"
        )
        return {
            "ok": res["ok"],
            "summary": summary,
            "detail": res,
            "llm_payload": res,
        }

    if tool_name == "web_fetch":
        url = str(args.get("url", "")).strip()
        res = web_fetch(url, timeout=web_timeout, related_query=related_query)
        links_cnt = len(res.get("links", []))
        summary = (
            f"已抓取《{res.get('title') or url[:45]}》（正文 {len(res.get('content', ''))} 字 · 子链接 {links_cnt} 条）"
            if res["ok"]
            else f"抓取失败 ({url[:42]})：{res.get('error', '')}"
        )
        return {
            "ok": res["ok"],
            "summary": summary,
            "detail": {
                "url": res.get("url", url),
                "title": res.get("title", ""),
                "char_count": len(res.get("content", "")),
                "preview": res.get("content", "")[:600],
                "links_count": links_cnt,
                "links_sample": res.get("links", [])[:15],
                "attachments": res.get("attachments", []),
                "error": res.get("error", ""),
            },
            "llm_payload": {**res, "content": res.get("content", "")[:4500]},
        }

    if tool_name == "python_sandbox":
        code = str(args.get("code", ""))
        purpose = str(args.get("purpose", "")).strip()
        res = execute_python_code(code, storage.files_dir, storage.artifacts_dir)
        first_line = (res.get("stdout") or "").strip().splitlines()
        out_hint = first_line[0][:60] if first_line else ("执行成功" if res["ok"] else res.get("stderr", "")[:60])
        summary = (
            f"Python 沙箱运行完成 ({res['duration_ms']}ms)：{out_hint}"
            if res["ok"]
            else f"Python 沙箱报错：{out_hint}"
        )
        return {
            "ok": res["ok"],
            "summary": summary,
            "detail": {
                "purpose": purpose,
                "code": code,
                "stdout": res.get("stdout", ""),
                "stderr": res.get("stderr", ""),
                "exit_code": res.get("exit_code", 0),
                "duration_ms": res.get("duration_ms", 0),
                "generated_files": res.get("generated_files", []),
            },
            "llm_payload": res,
        }

    if tool_name == "search_knowledge":
        q = str(args.get("query", "")).strip()
        top_k = int(args.get("top_k", 6) or 6)
        hits = storage.search_knowledge_fts(q, top_k=min(12, max(1, top_k)))
        summary = f"本地 FTS5 检索「{q}」，召回 {len(hits)} 个相关文档切片"
        return {
            "ok": True,
            "summary": summary,
            "detail": {"query": q, "hits": hits},
            "llm_payload": {"query": q, "hits": hits},
        }

    if tool_name == "memory_store":
        action = str(args.get("action", "upsert")).strip()
        if action == "delete" and args.get("memory_id"):
            mid = str(args["memory_id"]).strip()
            storage.delete_memory(mid)
            return {
                "ok": True,
                "summary": f"已移除长期记忆条目 {mid}",
                "detail": {"action": "delete", "memory_id": mid},
                "llm_payload": {"ok": True, "deleted": mid},
            }
        cat = str(args.get("category", "fact")).strip()
        content = str(args.get("content", "")).strip()
        mid = str(args.get("memory_id", "")).strip()
        if not content:
            return {
                "ok": False,
                "summary": "记忆写入失败：内容为空",
                "detail": {"error": "content required"},
                "llm_payload": {"ok": False, "error": "content required"},
            }
        saved = storage.upsert_memory(cat, content, memory_id=mid, source="agent")
        return {
            "ok": True,
            "summary": f"已写入长期记忆 [{saved['category']}]：{saved['content'][:42]}",
            "memory": saved,
            "detail": saved,
            "llm_payload": {"ok": True, "memory": saved},
        }

    if tool_name == "memory_recall":
        q = str(args.get("query", "")).strip()
        mems = storage.search_memories(q, limit=12)
        return {
            "ok": True,
            "summary": f"检索到 {len(mems)} 条长期记忆",
            "detail": {"query": q, "memories": mems},
            "llm_payload": {"memories": mems},
        }

    if tool_name == "artifact_create":
        title = str(args.get("title", "未命名产物")).strip() or "未命名产物"
        kind = str(args.get("kind", "markdown")).strip()
        content = str(args.get("content", ""))
        filename = str(args.get("filename", "")).strip()
        art = storage.create_artifact(session_id, title, kind, content, filename=filename)
        return {
            "ok": True,
            "summary": f"已生成画布产物《{art['title']}》({art['kind']} · {len(content)} 字符)",
            "artifact": art,
            "detail": {
                "artifact_id": art["artifact_id"],
                "title": art["title"],
                "kind": art["kind"],
                "filename": art["filename"],
                "char_count": len(content),
            },
            "llm_payload": {
                "ok": True,
                "artifact_id": art["artifact_id"],
                "title": art["title"],
                "kind": art["kind"],
                "filename": art["filename"],
            },
        }

    if tool_name == "feed_subscribe":
        name = str(args.get("name", "")).strip()
        kind = str(args.get("kind", "web_list")).strip()
        target = str(args.get("target", "")).strip()
        if not target:
            return {
                "ok": False,
                "summary": "订阅失败：缺少目标 URL 或检索词",
                "detail": {"error": "target required"},
                "llm_payload": {"ok": False, "error": "target required"},
            }
        src = storage.add_feed_source(name or target[:30], kind, target)
        return {
            "ok": True,
            "summary": f"已新增动态流订阅源「{src['name']}」({src['kind']})",
            "source": src,
            "detail": src,
            "llm_payload": {"ok": True, "source": src},
        }

    if tool_name == "sync_feeds":
        rep = sync_all_sources(storage)
        return {
            "ok": True,
            "summary": f"已同步全部启用订阅源：抓取 {rep['fetched_count']} 条，新增 {rep['newly_added']} 条",
            "detail": rep,
            "llm_payload": rep,
        }

    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3:
            _, target_sid, ext_tool_prefix = parts
            for srv in storage.get_mcp_servers():
                sid = re.sub(r"[^a-zA-Z0-9_]", "_", str(srv.get("server_id", "ext")))
                if sid == target_sid:
                    orig_name = ext_tool_prefix
                    for t in srv.get("tools") or []:
                        if re.sub(r"[^a-zA-Z0-9_\-]", "_", t.get("name", "")) == ext_tool_prefix:
                            orig_name = t["name"]
                            break
                    ext_res = MCPClient.call_external_tool(srv, orig_name, args, transport_override=mcp_transport_override)
                    return {
                        "ok": ext_res.get("ok", False),
                        "summary": f"调用外部 MCP [{srv.get('name', sid)}] · {orig_name} " + ("成功" if ext_res.get("ok") else f"失败：{ext_res.get('error', '')}"),
                        "detail": ext_res,
                        "llm_payload": ext_res,
                    }

    return {
        "ok": False,
        "summary": f"未知工具：{tool_name}",
        "detail": {"error": f"Unknown tool: {tool_name}"},
        "llm_payload": {"ok": False, "error": f"Unknown tool: {tool_name}"},
    }


def build_system_prompt(storage: RuntimeStorage, skills_mgr: SkillManager, query: str) -> str:
    """动态组装通用智能体系统提示词"""
    model_id = resolve_model(storage.get_llm_config())
    memories = storage.search_memories(query, limit=15)
    if memories:
        mem_lines = [f"- [{m['category']}] (id={m['memory_id']}): {m['content']}" for m in memories]
        mem_block = "\n".join(mem_lines)
    else:
        mem_block = "（当前为空。当用户在对话中透露个人院校/公司、专业岗位、目标方向或偏好习惯时，可调用 `memory_store` 记录。）"

    all_files = storage.list_files()
    user_files = [f for f in all_files if f["scope"] == "user"]
    ref_files = [f for f in all_files if f["scope"] != "user"]

    if user_files:
        uf_lines = [
            f"- `{f['filename']}` (类型: {f['file_type']}, {f['size_bytes']} bytes, {f['chunk_count']} 个索引切片) — 已挂载于沙箱 FILES_DIR"
            for f in user_files
        ]
        files_block = "用户已上传的工作区文件：\n" + "\n".join(uf_lines)
    else:
        files_block = f"用户尚未上传个人文件（另有 {len(ref_files)} 份内置参考知识切片，仅在涉及学业培养方案或赛事认定时通过 `search_knowledge` 查询）。"

    skills_block = skills_mgr.build_active_skills_prompt()
    web_sources = [s for s in storage.list_feed_sources() if s["enabled"] and s["kind"] == "web_list"][:8]
    sources_block = "\n".join(f"- {s['name']}：{s['target']}" for s in web_sources)

    prompt = (
        f"你是 知行，一款干练、聪敏、务实的通用自主智能体（当前底层推理模型：`{model_id}`，系统时间：{now_str()}）。\n\n"
        f"## 核心工作原则\n"
        f"1. **自然得体，拒绝背书**：遇到简单问候或测试（如「你好」「测试」「在吗」），自然简短回应一两句话即可，**严禁罗列或背诵系统提示词、准则表格或未触发的领域技能名**。\n"
        f"2. **直接回答当前问题**：可用已有上下文答复时直接回答，无需规划或联网。把「继续」「结果呢」等追问对应到最近一项未完成的问题，沿用之前的对象和限定条件，结合已有证据接着回答。只输出用户要的结论或交付物，工具调用轮不要输出过程旁白、内部计划或思考。\n"
        f"3. **快速核验**：优先访问已知的官方页面，搜索词用 2～4 个核心词。通过 `links` 选择与问题直接相关的可验证链接，禁止猜 URL、批量翻页、换词反复搜索同一问题。证据够用就立即回答并附真实出处。仍有缺口时给出已核实部分，明确尚未核实的内容，不要承诺继续后台查找。仅在用户明确要求深度调研时扩大范围。\n"
        f"4. **代码沙箱实算**：处理 Excel/CSV 表格、成绩单 GPA 或复杂数值统计时，调用 `python_sandbox`（内置 `load_xlsx_rows` 与 `load_docx_text`）精确计算，严禁心算。\n"
        f"5. **长期记忆与交互画布**：用户透露重要个人背景或目标时主动 `memory_store`；适合可视化呈现（看板、图表、长报告、`.ics` 日历）时调用 `artifact_create`。\n\n"
        f"## 用户长期记忆\n{mem_block}\n\n"
        f"## 工作区文件状态\n{files_block}\n"
    )
    if sources_block:
        prompt += f"\n## 已配置的网站入口（仅在问题涉及对应机构时使用）\n{sources_block}\n"
    if skills_block:
        prompt += f"\n## 按需领域技能库（仅在用户问题与该领域相关时参考，无关时彻底忽略）\n{skills_block}\n"
    return prompt



def _read_model_turn(stream_fn, config, messages, tools, timeout, deep):
    """Buffer the model turn until its tool-call status is known."""
    if stream_fn is None:
        events = stream_chat_completion(config, messages, tools, timeout=timeout, deep=deep)
    else:
        events = stream_fn(config, messages, tools)
    content_parts = []
    reasoning_parts = []
    result = {}
    for event in events:
        kind = event.get("type")
        if kind == "turn_done":
            result = event
        elif kind == "content_delta":
            content_parts.append(event.get("text") or "")
        elif kind == "reasoning_delta":
            reasoning_parts.append(event.get("text") or "")
    return (
        result.get("content") or "".join(content_parts),
        result.get("reasoning") or "".join(reasoning_parts),
        result.get("tool_calls") or [],
    )


def _recent_web_evidence(history):
    evidence = []
    for msg in reversed(history):
        if msg["role"] == "user" and not _CONTINUATION.match(msg["content"]):
            break
        for event in reversed(msg.get("tool_events", [])):
            if not event.get("ok"):
                continue
            detail = event["detail"]
            if event["name"] == "web_fetch":
                evidence.append({k: detail[k] for k in ("title", "url", "preview") if detail.get(k)})
            elif event["name"] == "web_search":
                evidence.extend(detail.get("results", [])[:3])
            if len(evidence) >= 6:
                return evidence[:6]
    return evidence


def _tool_signature(name, args):
    if name in ("web_search", "search_knowledge"):
        return name, " ".join(sorted(str(args.get("query", "")).casefold().split()))
    if name == "web_fetch":
        url = str(args.get("url", "")).strip()
        try:
            return name, urldefrag(normalize_http_url(url)).url
        except ValueError:
            return name, url
    return name, json.dumps(args, sort_keys=True, ensure_ascii=False)


def _backup_answer(web_attempted, evidence, model_failed, artifacts):
    if web_attempted:
        lines = ["暂时未能核实到足以回答这道问题的信息。"]
        links = {}
        for item in evidence:
            url = item.get("url") or ""
            if not url.startswith(("http://", "https://")) or any(c in url for c in " \r\n()<>"):
                continue
            title = re.sub(r"[\[\]\r\n]", "", item.get("title") or "").strip()[:100]
            if title:
                links[url] = title
        if links:
            lines.append("\n目前可参考的来源\n")
            lines.extend(f"- [{title}]({url})" for url, title in list(links.items())[:3])
        if artifacts:
            lines.append("\n已生成的文件可在下方打开。")
        return "\n".join(lines)
    if artifacts:
        return "已生成文件，可在下方打开。"
    if model_failed:
        return "推理服务暂时未能返回答复。请检查引擎设置中的连接状态后重试。"
    return "本轮未能生成完整答复。请重新发送这条消息。"


def run_agent_stream(
    query: str,
    session_id: str,
    storage: RuntimeStorage,
    skills_mgr: SkillManager,
    focus_item_id: Optional[str] = None,
    stream_fn: Optional[Callable[..., Generator[Dict[str, Any], None, None]]] = None,
    mcp_transport_override: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    allowed_tools: Optional[Collection[str]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """执行工具并交付答复，工具轮的模型输出仅用于当前请求。"""
    started = time.monotonic()
    clean_query = query.strip()
    if not session_id:
        sess = storage.create_session(clean_query[:26] or "新对话")
        session_id = sess["session_id"]

    yield {"type": "session_init", "session_id": session_id}

    llm_cfg = storage.get_llm_config()

    if stream_fn is None and not is_llm_configured(llm_cfg):
        storage.append_message(session_id, "user", clean_query)
        fts_hits = storage.search_knowledge_fts(clean_query, top_k=3)
        recorded_events: List[Dict[str, Any]] = []
        if fts_hits:
            recorded_events.append({
                "name": "search_knowledge",
                "ok": True,
                "summary": f"本地 FTS5 索引命中 {len(fts_hits)} 条片段",
                "detail": {"query": clean_query, "hits": fts_hits},
            })


        lines = [
            "### 尚未连接大模型推理引擎",
            "",
            "当前工作区未检测到有效的大模型 API Key。请点击左下角 **引擎设置** 接入任意真实推理服务后即可启用完整自主 Agent 能力：",
            "",
            "- **华为云 ModelArts MaaS / AgentArts**：填入专属 Endpoint 与 API Key",
            "- **DeepSeek 官方 / SiliconFlow / 阿里云百炼**：填入对应 API Key，点击「测试连接」验证延迟",
            "- **本地开源模型（免 Key）**：若本机已运行 Ollama，可将接口地址设为 `http://127.0.0.1:11434/v1/chat/completions`",
        ]
        if allowed_tools is not None:
            lines = ["模型服务尚未配置。" if not fts_hits else "模型服务尚未配置，以下是从工作区检索到的资料片段。"]
        if fts_hits:
            lines.append("")
            lines.append("---")
            lines.append("#### 本地工作区 FTS5 全文索引直出片段（未经大模型推理加工）")
            for idx, h in enumerate(fts_hits, start=1):
                snippet = re.sub(r"\s+", " ", h["content"]).strip()[:220]
                lines.append(f"{idx}. **{h['citation']}**：{snippet}")

        honest_msg = "\n".join(lines)
        storage.append_message(session_id, "assistant", honest_msg, tool_events=recorded_events)
        yield {"type": "engine_required"}
        yield {"type": "content_delta", "text": honest_msg}
        yield {
            "type": "done",
            "session_id": session_id,
            "sessions": storage.list_sessions(),
            "artifact_ids": [],
        }
        return

    history = storage.get_messages(session_id, include_tool_events=True, limit=14)
    storage.append_message(session_id, "user", clean_query)
    continuation = bool(_CONTINUATION.match(clean_query))
    prior_request = next((m["content"] for m in reversed(history)
                          if m["role"] == "user" and not _CONTINUATION.match(m["content"])), "")
    deep = bool(_DEEP_REQUEST.search(clean_query) or (continuation and _DEEP_REQUEST.search(prior_request)))
    web_budget = {"web_search": 5 if deep else 2, "web_fetch": 10 if deep else 3}
    max_web_rounds = 6 if deep else 3
    exploration_end = started + (65 if deep else 26)
    web_timeout = 8 if deep else 6
    model_timeout = 40 if deep else 22
    web_rounds = 0
    web_attempted = False
    previous_evidence = _recent_web_evidence(history) if continuation else []
    evidence = []
    observed = []
    tool_events = []
    artifact_ids = []

    task_context = ""
    related_query = prior_request if continuation and prior_request else clean_query
    focus = storage.get_feed_item(focus_item_id) if focus_item_id else None
    if focus:
        full_text = focus.get("full_content") or ""
        attachments = focus.get("attachments") or []
        url = focus.get("url", "")
        if not full_text and url.startswith(("http://", "https://")):
            web_attempted = True
            web_budget["web_fetch"] -= 1
            task = _WEB_WORKERS.submit(web_fetch, url, timeout=web_timeout, related_query=related_query)
            try:
                fetched = task.result(timeout=web_timeout)
            except Exception:
                task.cancel()
                fetched = {}
            if fetched.get("ok") and fetched.get("content"):
                full_text = fetched["content"]
                attachments = fetched.get("attachments", [])
                storage.update_feed_item_content(focus_item_id, full_text, attachments)
                detail = {"url": url, "title": focus["title"], "preview": full_text[:600]}
                evidence.append(detail)
                tool_events.append({"name": "web_fetch", "ok": True, "detail": detail, "answer_only": True})
        task_context += (
            "\n\n[用户选中的动态]\n"
            f"标题：{focus['title']}\n链接：{url}\n"
            f"来源：{focus.get('source_name', '')} ({focus.get('published_at', '')})\n"
            f"附件：{json.dumps(attachments, ensure_ascii=False)}\n"
            f"{(full_text or focus.get('summary', ''))[:5000]}"
        )

    if continuation and prior_request:
        task_context += "\n\n[本次是追问] 继续回答上一项任务：" + prior_request
        if previous_evidence:
            task_context += "\n[上一问取得的来源线索，仅用于核实]\n" + json.dumps(previous_evidence, ensure_ascii=False)[:4500]

    prompt = build_system_prompt(storage, skills_mgr, clean_query)
    if allowed_tools is not None:
        prompt += (
            "\n当前为组内试用，只能调用以下工具："
            + "、".join(sorted(allowed_tools))
            + "。其他操作请直接说明该功能未在试用环境开放。只用可用资料回答，避免猜测来源。"
        )
    messages = [{"role": "system", "content": prompt}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history
                    if m["role"] in ("user", "assistant") and m["content"])
    messages.append({"role": "user", "content": clean_query + task_context})
    original_messages = list(messages)
    tools_schema = build_merged_tools_schema(storage)
    if allowed_tools is not None:
        tools_schema = [tool for tool in tools_schema if tool["function"]["name"] in allowed_tools]
    seen_calls = set()
    if web_attempted:
        seen_calls.add(_tool_signature("web_fetch", {"url": url}))
    final_content = ""
    model_failed = False

    try:
        for step in range(1, MAX_AGENT_STEPS + 1):
            remaining = exploration_end - time.monotonic() if web_attempted else model_timeout
            if remaining <= 0:
                break
            available_tools = [
                tool for tool in tools_schema
                if tool["function"]["name"] not in _WEB_TOOLS
                or (web_rounds < max_web_rounds and web_budget[tool["function"]["name"]] > 0)
            ]
            text, reasoning, tool_calls = _read_model_turn(
                stream_fn, llm_cfg, messages, available_tools, min(model_timeout, remaining), deep,
            )
            if not tool_calls:
                final_content = clean_answer_text(text, strip_search_narration=web_attempted)
                break

            assistant_turn = {"role": "assistant", "content": "", "tool_calls": tool_calls}
            if reasoning and urlsplit(llm_cfg.get("api_url") or "").hostname == "api.deepseek.com":
                assistant_turn["reasoning_content"] = reasoning
            messages.append(assistant_turn)

            scheduled = []
            searched = False
            for index, call in enumerate(tool_calls):
                call_id = call.get("id") or f"call_{step}_{index}"
                call["id"] = call_id
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                signature = _tool_signature(name, args)
                skip = ""
                future = None
                deadline = 0
                if allowed_tools is not None and name not in allowed_tools:
                    skip = "此工具未在组内试用开放。"
                elif signature in seen_calls:
                    skip = "本轮已执行相同请求，请基于已有证据答复。"
                elif name in _WEB_TOOLS:
                    web_attempted = True
                    remaining = exploration_end - time.monotonic()
                    if remaining <= 0 or web_rounds >= max_web_rounds or not web_budget[name]:
                        skip = "本轮联网预算已用完，请根据现有资料回答并标出未核实之处。"
                    else:
                        if name == "web_search" and not deep:
                            args = {**args, "max_results": 5}
                        searched = True
                        web_budget[name] -= 1
                        timeout = min(web_timeout, remaining)
                        deadline = time.monotonic() + timeout
                        future = _WEB_WORKERS.submit(
                            execute_runtime_tool, name, args, storage, session_id,
                            mcp_transport_override, web_timeout=timeout, related_query=related_query,
                        )
                if not skip and name in (_WEB_TOOLS | {"search_knowledge", "plan_tasks"}):
                    seen_calls.add(signature)
                scheduled.append((call_id, name, args, skip, future, deadline))

            web_rounds += int(searched)
            if all(call[3] for call in scheduled):
                break

            for call_id, name, args, skip, future, deadline in scheduled:
                result = None
                if not skip:
                    try:
                        if future:
                            result = future.result(timeout=max(0, deadline - time.monotonic()))
                        else:
                            result = execute_runtime_tool(name, args, storage, session_id, mcp_transport_override)
                    except FutureTimeout:
                        if future:
                            future.cancel()
                        skip = "该网页请求超时。请使用已有证据答复。"
                    except Exception:
                        skip = "工具执行失败。请使用已有证据答复。"

                if result is None:
                    result = {"ok": False, "detail": {"error": skip}, "llm_payload": {"ok": False, "error": skip}}
                tool_events.append({"name": name, "ok": result["ok"], "detail": result["detail"], "answer_only": True})

                if result["ok"] and name == "web_fetch":
                    evidence.append(result["detail"])
                elif result["ok"] and name == "web_search":
                    evidence.extend(result["detail"]["results"][:3])
                elif result["ok"] and name == "memory_store":
                    yield {"type": "memory_updated", "memories": storage.list_memories()}
                elif result.get("artifact"):
                    artifact = result["artifact"]
                    artifact_ids.append(artifact["artifact_id"])
                    yield {"type": "artifact_created", "artifact": artifact}
                elif result["ok"] and name in ("feed_subscribe", "sync_feeds"):
                    yield {"type": "sources_updated", "sources": storage.list_feed_sources()}

                payload = json.dumps(result["llm_payload"], ensure_ascii=False)
                if len(payload) > 14000 and name not in _WEB_TOOLS:
                    payload = json.dumps({"ok": result["ok"], "excerpt": payload[:12000]}, ensure_ascii=False)
                observed.append((name, result["ok"], payload))
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": payload})

    except Exception:
        model_failed = True

    if not final_content and (observed or not model_failed):
        useful = [(name, payload) for name, ok, payload in observed if ok and name != "plan_tasks"]
        findings = "\n".join(f"{name}: {payload[:4400]}" for name, payload in useful[:12])
        synth_messages = original_messages + [{
            "role": "user",
            "content": (
                "[收尾] 请直接回答上面的用户问题，依据下方已经获得的资料。"
                "证据有限时报告已核实的内容、对应来源与未核实项。只交付结论，不描述检索过程或接下来的计划。\n"
                + findings
            ),
        }]
        try:
            text, _, calls = _read_model_turn(stream_fn, llm_cfg, synth_messages, None, model_timeout, False)
            if not calls:
                final_content = clean_answer_text(text, strip_search_narration=web_attempted)
        except Exception:
            model_failed = True

    if not final_content:
        final_content = _backup_answer(
            web_attempted or bool(previous_evidence), evidence + previous_evidence, model_failed, artifact_ids,
        )
    storage.append_message(session_id, "assistant", final_content, tool_events=tool_events, artifact_ids=artifact_ids)
    yield {"type": "content_delta", "text": final_content}
    yield {"type": "done", "session_id": session_id, "sessions": storage.list_sessions(), "artifact_ids": artifact_ids}
