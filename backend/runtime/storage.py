# -*- coding: utf-8 -*-
"""
知行 Agent Runtime — 市场级统一持久化与工作区存储底座 (SQLite WAL + FTS5 + 物理文件工作区)
无任何写死假学生账号或硬编码专业表单，支撑通用智能体核心原语：
  • 多轮会话与内部工具事件记录 (sessions / messages)
  • 自进化长期记忆库 (memories: profile / goal / preference / fact)
  • 物理磁盘文件库 + SQLite FTS5 全文检索 (files / file_chunks_fts)
  • 可交互产物画布持久化 (artifacts: html / markdown / svg / code / ics / csv)
  • 实时信息流连接器与缓存 (feed_sources / feed_items)
"""
from contextlib import contextmanager
from datetime import datetime
import io
import json
import os
import re
import sqlite3
from tempfile import NamedTemporaryFile
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib


_THINK_BLOCK = re.compile(r"<(think|analysis|reasoning)\b[^>]*>.*?(?:</\1\s*>|\Z)", re.I | re.S)
_SEARCH_NARRATION = re.compile(
    r"(?:^|[。！？；，])\s*我(?:来|先|再|继续|直接|改用|换用|尝试|去|也|将|需要|会|接着)*"
    r"(?:检索|搜索|抓取|查询|访问|核实|尝试|改从|改用|换用|换个|换一|看一|查看|翻阅|查找|改走)"
    r"(?!到|了|出)"
)


def clean_answer_text(text: str, strip_search_narration: bool = False) -> str:
    text = _THINK_BLOCK.sub("", text).strip()
    if strip_search_narration:
        paragraphs = re.split(r"\n\s*\n", text)
        text = "\n\n".join(p for p in paragraphs if not _SEARCH_NARRATION.search(p))
    return text.strip()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(name: str) -> str:
    base = os.path.basename(name or "untitled.txt").strip()
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base)
    return cleaned[:120] or "file.txt"


def tokenize_cjk_fts(text: str) -> str:
    if not text:
        return ""
    ascii_toks = re.findall(r"[A-Za-z0-9_.\-]{2,}", text.lower())
    zh_segs = re.findall(r"[\u4e00-\u9fff]+", text)
    zh_toks: List[str] = []
    for seg in zh_segs:
        for ch in seg:
            zh_toks.append(ch)
        for i in range(len(seg) - 1):
            zh_toks.append(seg[i : i + 2])
    return " ".join(ascii_toks + zh_toks)


def decode_bytes_auto(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def extract_docx_bytes(raw: bytes) -> str:
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find(f"{W}body") or root
    for child in list(body):
        if child.tag == f"{W}p":
            txt = "".join(t.text for t in child.iter(f"{W}t") if t.text).strip()
            if txt:
                lines.append(txt)
        elif child.tag == f"{W}tbl":
            for tr in child.findall(f".//{W}tr"):
                cells: List[str] = []
                for tc in tr.findall(f"{W}tc"):
                    c_txt = " ".join(
                        "".join(t.text for t in p.iter(f"{W}t") if t.text).strip()
                        for p in tc.findall(f".//{W}p")
                    ).strip()
                    if c_txt:
                        cells.append(c_txt)
                if cells:
                    lines.append("\t".join(cells))
    return "\n".join(lines)


def extract_xlsx_bytes(raw: bytes) -> str:
    lines: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root:
                parts = [t.text for t in si.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
                shared.append("".join(parts))
        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for idx, sname in enumerate(sheets, start=1):
            if len(sheets) > 1:
                lines.append(f"# Sheet {idx}")
            s_root = ET.fromstring(zf.read(sname))
            for row in s_root.iter():
                if not (row.tag.endswith("}row") or row.tag == "row"):
                    continue
                vals: List[str] = []
                for cell in list(row):
                    if not (cell.tag.endswith("}c") or cell.tag == "c"):
                        continue
                    ctype = cell.attrib.get("t", "")
                    val = ""
                    if ctype == "inlineStr":
                        val = "".join(t.text for t in cell.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text)
                    else:
                        v_node = next((c for c in list(cell) if c.tag.endswith("}v") or c.tag == "v"), None)
                        if v_node is not None and v_node.text is not None:
                            if ctype == "s":
                                try:
                                    si_idx = int(v_node.text)
                                    val = shared[si_idx] if 0 <= si_idx < len(shared) else v_node.text
                                except ValueError:
                                    val = v_node.text
                            else:
                                val = v_node.text
                    if val.strip():
                        vals.append(val.strip())
                if vals:
                    lines.append("\t".join(vals))
    return "\n".join(lines)


def extract_pdf_bytes(raw: bytes) -> str:
    chunks: List[str] = []
    for m in re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", raw, re.DOTALL):
        blob = m.group(1)
        try:
            blob = zlib.decompress(blob)
        except Exception:
            pass
        for pm in re.finditer(rb"\(([^()]{2,240})\)", blob):
            s = pm.group(1).decode("utf-8", errors="ignore")
            s = re.sub(r"[^\w\u4e00-\u9fff\s.,;:!?()\-/，。；：、%]", "", s).strip()
            if len(s) >= 2:
                chunks.append(s)
        for hm in re.finditer(rb"<([0-9A-Fa-f]{8,480})>", blob):
            hex_str = hm.group(1).decode("ascii", errors="ignore")
            if len(hex_str) % 4 == 0 and hex_str.upper().startswith("FEFF"):
                try:
                    decoded = bytes.fromhex(hex_str).decode("utf-16-be", errors="ignore").strip()
                    decoded = re.sub(r"[^\w\u4e00-\u9fff\s.,;:!?()\-/，。；：、%]", "", decoded).strip()
                    if len(decoded) >= 2:
                        chunks.append(decoded)
                except Exception:
                    pass
    out = "\n".join(chunks)
    if len(out) < 30:
        return "[二进制PDF已保存至工作区磁盘，复杂字库或扫描件建议转换为 .docx/.txt 或通过 Python 工具处理]"
    return out


def extract_file_text(filename: str, raw: bytes) -> Dict[str, str]:
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".docx":
        return {"file_type": "docx", "text": extract_docx_bytes(raw).strip()}
    if ext == ".xlsx":
        return {"file_type": "xlsx", "text": extract_xlsx_bytes(raw).strip()}
    if ext == ".pdf":
        return {"file_type": "pdf", "text": extract_pdf_bytes(raw).strip()}
    return {"file_type": ext.lstrip(".") or "txt", "text": decode_bytes_auto(raw).strip()}


def chunk_text(text: str, max_chars: int = 460, overlap: int = 60) -> List[Dict[str, str]]:
    if not text.strip():
        return []
    sections = re.split(r"(?=^#{1,3}\s+)", text, flags=re.MULTILINE) if re.search(r"^#{1,3}\s+", text, re.MULTILINE) else [text]
    chunks: List[Dict[str, str]] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        hm = re.search(r"^#{1,3}\s+(.+)$", sec, re.MULTILINE)
        sec_title = hm.group(1).strip()[:60] if hm else "正文"
        if len(sec) <= max_chars + 120:
            chunks.append({"section": sec_title, "content": sec})
            continue
        pos = 0
        idx = 1
        while pos < len(sec):
            end = min(len(sec), pos + max_chars)
            piece = sec[pos:end].strip()
            if piece:
                chunks.append({"section": f"{sec_title} #{idx}", "content": piece})
                idx += 1
            if end >= len(sec):
                break
            pos = max(pos + 1, end - overlap)
    return chunks


DEFAULT_FEED_SOURCES = [
    {"source_id": "src_htu_teaching", "name": "河南师范大学教务处", "kind": "web_list", "target": "https://www.htu.edu.cn/teaching/3251/list.htm", "enabled": 1},
    {"source_id": "src_htu_cs", "name": "河师大计算机与信息工程学院", "kind": "web_list", "target": "https://www.htu.edu.cn/cs/1006/list.htm", "enabled": 1},
    {"source_id": "src_htu_sw", "name": "河师大软件学院", "kind": "web_list", "target": "https://www.htu.edu.cn/sw/", "enabled": 1},
    {"source_id": "src_github_agent", "name": "GitHub · AI Agent 热门开源", "kind": "github", "target": "AI agent MCP", "enabled": 1},
    {"source_id": "src_arxiv_ai", "name": "arXiv · 人工智能最新论文", "kind": "arxiv", "target": "cat:cs.AI+OR+cat:cs.CL", "enabled": 1},
]


class RuntimeStorage:
    def __init__(self, workspace_dir: str, llm_config_override: Optional[Dict[str, str]] = None):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self._llm_config_override = llm_config_override
        self.data_dir = os.path.join(self.workspace_dir, "data")
        self.files_dir = os.path.join(self.workspace_dir, "files")
        self.artifacts_dir = os.path.join(self.workspace_dir, "artifacts")
        self.skills_dir = os.path.join(self.workspace_dir, "skills")
        for d in (self.data_dir, self.files_dir, self.artifacts_dir, self.skills_dir):
            os.makedirs(d, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "muse_agent.db")
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    msg_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reasoning TEXT NOT NULL DEFAULT '',
                    tool_events_json TEXT NOT NULL DEFAULT '[]',
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'agent',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    disk_path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts USING fts5(
                    file_id UNINDEXED,
                    scope UNINDEXED,
                    filename,
                    section,
                    content,
                    search_tokens,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    disk_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feed_sources (
                    source_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_sync_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS feed_items (
                    item_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    full_content TEXT NOT NULL DEFAULT '',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    published_at TEXT NOT NULL DEFAULT '',
                    saved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )

    def bootstrap_defaults(self, builtin_kb_dir: Optional[str] = None, seed_notices_path: Optional[str] = None):
        """初始化默认连接器与可选参考文档库（零假人画像）"""
        need_kb = False
        with self._conn() as conn:
            src_cnt = conn.execute("SELECT COUNT(*) FROM feed_sources").fetchone()[0]
            if src_cnt == 0:
                for s in DEFAULT_FEED_SOURCES:
                    conn.execute(
                        "INSERT OR IGNORE INTO feed_sources(source_id, name, kind, target, enabled, last_sync_at) VALUES (?, ?, ?, ?, ?, '')",
                        (s["source_id"], s["name"], s["kind"], s["target"], s["enabled"]),
                    )

            item_cnt = conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0]
            if item_cnt == 0 and seed_notices_path and os.path.isfile(seed_notices_path):
                try:
                    raw_items = json.load(open(seed_notices_path, encoding="utf-8"))
                    for it in raw_items:
                        iid = it.get("notice_id") or ("seed_" + uuid.uuid4().hex[:8])
                        conn.execute(
                            """INSERT OR IGNORE INTO feed_items
                               (item_id, source_id, source_name, title, url, summary, full_content, attachments_json, published_at, saved, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, 0, ?)""",
                            (
                                iid,
                                "seed_verified",
                                it.get("source_dept", "校内/赛事精选"),
                                it.get("title", ""),
                                it.get("source_url", ""),
                                it.get("benefit_tag", ""),
                                it.get("full_content_inline", ""),
                                it.get("publish_date", "2026-11-25"),
                                now_str(),
                            ),
                        )
                except Exception:
                    pass

            seeded_kb = conn.execute("SELECT value_json FROM settings WHERE key='seeded_builtin_kb'").fetchone()
            if not seeded_kb and builtin_kb_dir and os.path.isdir(builtin_kb_dir):
                need_kb = True
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value_json, updated_at) VALUES ('seeded_builtin_kb', 'true', ?)",
                    (now_str(),),
                )

        if need_kb and builtin_kb_dir:
            for fname in sorted(os.listdir(builtin_kb_dir)):
                if fname.endswith(".md"):
                    raw_bytes = open(os.path.join(builtin_kb_dir, fname), "rb").read()
                    self.save_and_index_file(fname, raw_bytes, scope="reference")

    # ---- Settings ----
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value_json"])
            except Exception:
                return default

    def set_setting(self, key: str, value: Any):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now_str()),
            )

    def get_llm_config(self) -> Dict[str, str]:
        if self._llm_config_override is not None:
            return dict(self._llm_config_override)
        saved = self.get_setting("llm_config", {}) or {}
        api_url = str(saved.get("api_url", "")).strip()
        api_key = str(saved.get("api_key", "")).strip()
        model = str(saved.get("model", "")).strip()
        search_key = str(saved.get("search_api_key", "")).strip()

        # 自动回退环境变量识别
        if not api_key:
            for env_name, default_url, default_model in [
                ("DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions", "deepseek-flash"),
                ("SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1/chat/completions", "Qwen/Qwen2.5-72B-Instruct"),
                ("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-plus"),
                ("AGENTARTS_API_KEY", os.environ.get("AGENTARTS_API_URL", "https://api.modelarts-maas.com/v1/chat/completions"), "DeepSeek-V3"),
                ("OPENAI_API_KEY", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions"), "gpt-4o-mini"),
            ]:
                if os.environ.get(env_name):
                    api_key = os.environ[env_name]
                    api_url = api_url or default_url
                    model = model or default_model
                    break

        return {
            "api_url": api_url,
            "api_key": api_key,
            "model": model or ("deepseek-flash" if urlparse(api_url).hostname == "api.deepseek.com" else "deepseek-chat"),
            "search_api_key": search_key or os.environ.get("TAVILY_API_KEY", ""),
        }

    def save_llm_config(self, api_url: str, api_key: str, model: str, search_api_key: str = "", clear_key: bool = False):
        cur = self.get_setting("llm_config", {}) or {}
        clean_in_key = re.sub(r"[\s\u200b-\u200f\ufeff]", "", api_key or "")
        clean_s_key = re.sub(r"[\s\u200b-\u200f\ufeff]", "", search_api_key or "")
        final_key = "" if clear_key else (clean_in_key if clean_in_key else cur.get("api_key", ""))
        final_skey = clean_s_key if clean_s_key else cur.get("search_api_key", "")
        self.set_setting("llm_config", {
            "api_url": api_url.strip(),
            "api_key": final_key,
            "model": model.strip(),
            "search_api_key": final_skey,
        })

    def get_mcp_servers(self) -> List[Dict[str, Any]]:
        return self.get_setting("mcp_servers", []) or []

    def save_mcp_servers(self, servers: List[Dict[str, Any]]):
        self.set_setting("mcp_servers", servers)

    # ---- Sessions & Messages ----
    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT session_id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC, rowid DESC").fetchall()
            return [dict(r) for r in rows]

    def create_session(self, title: str = "新对话") -> Dict[str, Any]:
        sid = "sess_" + uuid.uuid4().hex[:10]
        ts = now_str()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions(session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, title[:48] or "新对话", ts, ts),
            )
        return {"session_id": sid, "title": title[:48] or "新对话", "created_at": ts, "updated_at": ts}

    def rename_session(self, session_id: str, title: str):
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE session_id=?", (title[:48], now_str(), session_id))

    def delete_session(self, session_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def get_messages(self, session_id: str, include_tool_events: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            columns = "msg_id, session_id, role, content, tool_events_json, artifact_ids_json, created_at"
            rows = conn.execute(
                f"SELECT {columns} FROM messages WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (session_id, -1 if limit is None else limit),
            ).fetchall()
            out = []
            for r in reversed(rows):
                tools = json.loads(r["tool_events_json"] or "[]")
                content = r["content"]
                if r["role"] == "assistant":
                    legacy_web = any(t.get("name") in ("web_search", "web_fetch") and not t.get("answer_only") for t in tools)
                    content = clean_answer_text(content, strip_search_narration=legacy_web)
                    if legacy_web and not content:
                        content = "上一轮检索未生成结论。"
                message = {
                    "msg_id": r["msg_id"],
                    "session_id": r["session_id"],
                    "role": r["role"],
                    "content": content,
                    "artifact_ids": json.loads(r["artifact_ids_json"] or "[]"),
                    "created_at": r["created_at"],
                }
                if include_tool_events:
                    message["tool_events"] = tools
                out.append(message)
            return out

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        reasoning: str = "",
        tool_events: Optional[List[Dict[str, Any]]] = None,
        artifact_ids: Optional[List[str]] = None,
    ) -> str:
        mid = "msg_" + uuid.uuid4().hex[:10]
        ts = now_str()
        with self._conn() as conn:
            sess = conn.execute("SELECT title FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not sess:
                auto_title = re.sub(r"\s+", " ", content.strip())[:26] or "新对话"
                conn.execute(
                    "INSERT INTO sessions(session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (session_id, auto_title, ts, ts),
                )
            else:
                if sess["title"] == "新对话" and role == "user" and content.strip():
                    auto_title = re.sub(r"\s+", " ", content.strip())[:26]
                    conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE session_id=?", (auto_title, ts, session_id))
                else:
                    conn.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (ts, session_id))
            conn.execute(
                """INSERT INTO messages(msg_id, session_id, role, content, reasoning, tool_events_json, artifact_ids_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    session_id,
                    role,
                    content,
                    reasoning,
                    json.dumps(tool_events or [], ensure_ascii=False),
                    json.dumps(artifact_ids or [], ensure_ascii=False),
                    ts,
                ),
            )
        return mid

    # ---- Long-Term Memories ----
    def list_memories(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT memory_id, category, content, source, updated_at FROM memories ORDER BY updated_at DESC, rowid DESC").fetchall()
            return [dict(r) for r in rows]

    def upsert_memory(self, category: str, content: str, memory_id: str = "", source: str = "agent") -> Dict[str, Any]:
        cat = category if category in ("profile", "goal", "preference", "fact") else "fact"
        clean = content.strip()
        ts = now_str()
        with self._conn() as conn:
            if memory_id:
                conn.execute("UPDATE memories SET category=?, content=?, updated_at=? WHERE memory_id=?", (cat, clean, ts, memory_id))
                mid = memory_id
            else:
                # 若存在完全相同内容则更新时间而不重复插入
                dup = conn.execute("SELECT memory_id FROM memories WHERE content=?", (clean,)).fetchone()
                if dup:
                    mid = dup["memory_id"]
                    conn.execute("UPDATE memories SET category=?, updated_at=? WHERE memory_id=?", (cat, ts, mid))
                else:
                    mid = "mem_" + uuid.uuid4().hex[:8]
                    conn.execute(
                        "INSERT INTO memories(memory_id, category, content, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (mid, cat, clean, source, ts),
                    )
        return {"memory_id": mid, "category": cat, "content": clean, "source": source, "updated_at": ts}

    def delete_memory(self, memory_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))

    def search_memories(self, query: str = "", limit: int = 12) -> List[Dict[str, Any]]:
        all_mems = self.list_memories()
        if not query.strip():
            return all_mems[:limit]
        toks = set(tokenize_cjk_fts(query).split())
        scored = []
        for m in all_mems:
            mtoks = set(tokenize_cjk_fts(m["content"]).split())
            score = len(toks & mtoks) + (2 if m["category"] in ("profile", "goal") else 0)
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    # ---- Workspace Files & SQLite FTS5 ----
    def save_and_index_file(
        self, filename: str, raw_bytes: bytes, scope: str = "user", max_index_chars: Optional[int] = None,
    ) -> Dict[str, Any]:
        sname = safe_filename(filename)
        disk_path = os.path.join(self.files_dir, sname)
        extracted = extract_file_text(sname, raw_bytes)
        text = extracted["text"]
        if max_index_chars is not None:
            if not text:
                raise ValueError("没有提取到可检索的文字，请检查文件内容。")
            if len(text) > max_index_chars:
                raise ValueError("文件正文太长，请拆分后上传。")
        ftype = extracted["file_type"]
        chunks = chunk_text(text)
        ts = now_str()

        fid = "file_" + uuid.uuid4().hex[:8]
        staged = ""
        try:
            with NamedTemporaryFile(dir=self.files_dir, delete=False) as file:
                staged = file.name
                file.write(raw_bytes)
            with self._conn() as conn:
                olds = conn.execute("SELECT file_id FROM files WHERE filename=? AND scope=?", (sname, scope)).fetchall()
                for row in olds:
                    conn.execute("DELETE FROM file_chunks_fts WHERE file_id=?", (row["file_id"],))
                    conn.execute("DELETE FROM files WHERE file_id=?", (row["file_id"],))
                conn.execute(
                    """INSERT INTO files(file_id, filename, disk_path, file_type, size_bytes, char_count, chunk_count, scope, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (fid, sname, disk_path, ftype, len(raw_bytes), len(text), len(chunks), scope, ts),
                )
                for ch in chunks:
                    tokens = tokenize_cjk_fts(f"{sname} {ch['section']} {ch['content']}")
                    conn.execute(
                        "INSERT INTO file_chunks_fts(file_id, scope, filename, section, content, search_tokens) VALUES (?, ?, ?, ?, ?, ?)",
                        (fid, scope, sname, ch["section"], ch["content"], tokens),
                    )
                os.replace(staged, disk_path)
                staged = ""
        finally:
            if staged:
                os.unlink(staged)
        return {
            "file_id": fid,
            "filename": sname,
            "disk_path": disk_path,
            "file_type": ftype,
            "size_bytes": len(raw_bytes),
            "char_count": len(text),
            "chunk_count": len(chunks),
            "scope": scope,
            "created_at": ts,
        }

    def list_files(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT file_id, filename, disk_path, file_type, size_bytes, char_count, chunk_count, scope, created_at FROM files ORDER BY scope DESC, created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            return dict(row) if row else None

    def delete_file(self, file_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT disk_path FROM files WHERE file_id=?", (file_id,)).fetchone()
            conn.execute("DELETE FROM file_chunks_fts WHERE file_id=?", (file_id,))
            conn.execute("DELETE FROM files WHERE file_id=?", (file_id,))
        if row and row["disk_path"] and os.path.isfile(row["disk_path"]):
            try:
                os.remove(row["disk_path"])
            except OSError:
                pass

    def search_knowledge_fts(self, query: str, top_k: int = 6) -> List[Dict[str, Any]]:
        raw_toks = tokenize_cjk_fts(query).split()
        strong = [t for t in dict.fromkeys(raw_toks) if len(t) >= 2]
        if not strong:
            strong = list(dict.fromkeys(raw_toks))[:8]
        if not strong:
            return []

        match_expr = " OR ".join('"' + t.replace('"', "") + '"' for t in strong[:24])
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT file_id, scope, filename, section, content, bm25(file_chunks_fts) AS rank_score
                   FROM file_chunks_fts
                   WHERE file_chunks_fts MATCH ?
                   ORDER BY rank_score ASC
                   LIMIT 30""",
                (match_expr,),
            ).fetchall()
            candidates = [dict(r) for r in rows]

        scored = []
        for ch in candidates:
            ov = 0.0
            for tok in strong:
                if tok in ch["section"]:
                    ov += 3.2
                elif tok in ch["filename"].lower():
                    ov += 2.4
                elif tok in ch["content"].lower():
                    ov += 1.2
            if ch["scope"] == "user":
                ov += 1.0
            scored.append((ov, -float(ch["rank_score"]), ch))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [
            {
                "file_id": c["file_id"],
                "scope": c["scope"],
                "filename": c["filename"],
                "section": c["section"],
                "citation": f"《{c['filename']}》· {c['section']}",
                "content": c["content"],
            }
            for _, _, c in scored[:top_k]
        ]

    # ---- Artifacts Canvas ----
    def create_artifact(self, session_id: str, title: str, kind: str, content: str, filename: str = "") -> Dict[str, Any]:
        ext_map = {"html": ".html", "markdown": ".md", "svg": ".svg", "ics": ".ics", "csv": ".csv", "code": ".py"}
        norm_kind = kind if kind in ext_map else "markdown"
        aid = "art_" + uuid.uuid4().hex[:8]
        if not filename:
            filename = f"{safe_filename(title)[:40]}_{aid[-4:]}{ext_map[norm_kind]}"
        else:
            filename = safe_filename(filename)
        disk_path = os.path.join(self.artifacts_dir, filename)
        with open(disk_path, "w", encoding="utf-8") as f:
            f.write(content)
        ts = now_str()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO artifacts(artifact_id, session_id, title, kind, filename, disk_path, content, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (aid, session_id or "", title[:80], norm_kind, filename, disk_path, content, ts, ts),
            )
        return {
            "artifact_id": aid,
            "session_id": session_id or "",
            "title": title[:80],
            "kind": norm_kind,
            "filename": filename,
            "content": content,
            "created_at": ts,
        }

    def list_artifacts(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT artifact_id, session_id, title, kind, filename, created_at, updated_at FROM artifacts ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            return dict(row) if row else None

    def delete_artifact(self, artifact_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT disk_path FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            conn.execute("DELETE FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if row and row["disk_path"] and os.path.isfile(row["disk_path"]):
            try:
                os.remove(row["disk_path"])
            except OSError:
                pass

    # ---- Feed Sources & Pulse Items ----
    def list_feed_sources(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT source_id, name, kind, target, enabled, last_sync_at FROM feed_sources ORDER BY rowid ASC").fetchall()
            return [{**dict(r), "enabled": bool(r["enabled"])} for r in rows]

    def add_feed_source(self, name: str, kind: str, target: str) -> Dict[str, Any]:
        sid = "src_" + uuid.uuid4().hex[:8]
        k = kind if kind in ("web_list", "rss", "github", "arxiv", "search_query") else "web_list"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO feed_sources(source_id, name, kind, target, enabled, last_sync_at) VALUES (?, ?, ?, ?, 1, '')",
                (sid, name.strip()[:50] or target[:30], k, target.strip()),
            )
        return {"source_id": sid, "name": name.strip()[:50] or target[:30], "kind": k, "target": target.strip(), "enabled": True}

    def toggle_feed_source(self, source_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT enabled FROM feed_sources WHERE source_id=?", (source_id,)).fetchone()
            if row:
                conn.execute("UPDATE feed_sources SET enabled=? WHERE source_id=?", (0 if row["enabled"] else 1, source_id))

    def delete_feed_source(self, source_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM feed_sources WHERE source_id=?", (source_id,))

    def mark_source_synced(self, source_id: str):
        with self._conn() as conn:
            conn.execute("UPDATE feed_sources SET last_sync_at=? WHERE source_id=?", (now_str(), source_id))

    def upsert_feed_items(self, items: List[Dict[str, Any]]) -> int:
        inserted = 0
        with self._conn() as conn:
            existing = {
                r["title"]: r["item_id"]
                for r in conn.execute("SELECT item_id, title FROM feed_items").fetchall()
            }
            for it in items:
                title = (it.get("title") or "").strip()
                if not title or title in existing:
                    continue
                iid = it.get("item_id") or ("item_" + uuid.uuid4().hex[:8])
                conn.execute(
                    """INSERT OR REPLACE INTO feed_items
                       (item_id, source_id, source_name, title, url, summary, full_content, attachments_json, published_at, saved, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (
                        iid,
                        it.get("source_id", "live"),
                        it.get("source_name", "网络动态"),
                        title,
                        it.get("url", ""),
                        it.get("summary", ""),
                        it.get("full_content", ""),
                        json.dumps(it.get("attachments", []), ensure_ascii=False),
                        it.get("published_at", datetime.now().strftime("%Y-%m-%d")),
                        now_str(),
                    ),
                )
                existing[title] = iid
                inserted += 1
        return inserted

    def list_feed_items(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT item_id, source_id, source_name, title, url, summary, full_content, attachments_json, published_at, saved, created_at
                   FROM feed_items ORDER BY saved DESC, published_at DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["saved"] = bool(d["saved"])
                d["has_full_content"] = bool(d["full_content"])
                d["attachments"] = json.loads(d.pop("attachments_json", "[]") or "[]")
                out.append(d)
            return out

    def get_feed_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM feed_items WHERE item_id=?", (item_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["saved"] = bool(d["saved"])
            d["attachments"] = json.loads(d.pop("attachments_json", "[]") or "[]")
            return d

    def update_feed_item_content(self, item_id: str, full_content: str, attachments: List[Dict[str, str]]):
        with self._conn() as conn:
            conn.execute(
                "UPDATE feed_items SET full_content=?, attachments_json=? WHERE item_id=?",
                (full_content, json.dumps(attachments, ensure_ascii=False), item_id),
            )

    def toggle_save_feed_item(self, item_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT saved FROM feed_items WHERE item_id=?", (item_id,)).fetchone()
            if not row:
                return False
            new_val = 0 if row["saved"] else 1
            conn.execute("UPDATE feed_items SET saved=? WHERE item_id=?", (new_val, item_id))
            return bool(new_val)
