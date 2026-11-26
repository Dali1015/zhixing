"""Authenticated student workspace. The single-user runtime keeps its separate entry point."""

import base64
from functools import lru_cache
from html import escape
import hmac
from http.cookies import CookieError, SimpleCookie
import io
import os
from pathlib import Path
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, urlsplit
from weakref import WeakValueDictionary
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from backend.runtime.accounts import AccountStore, SESSION_TTL
from backend.runtime.http_guard import ProtectedBrowserRequests
from backend.runtime.llm_client import is_llm_configured, resolve_model
from backend.runtime.skills import SkillManager
from backend.runtime.storage import RuntimeStorage, safe_filename
from backend.runtime.study import weighted_score


COOKIE_NAME = "muse_account"
FILE_LIMIT = 2 * 1024 * 1024
DOCX_XML_LIMIT = 4 * 1024 * 1024
ACCOUNT_FILE_LIMIT = 20
ACCOUNT_BYTES_LIMIT = 24 * 1024 * 1024
PRODUCT_TOOLS = frozenset({"web_search", "search_knowledge", "memory_recall"})
FILE_TYPES = {".txt", ".md", ".csv", ".docx"}
READ_ROUTES = {
    "/", "/index.html", "/api/bootstrap", "/api/sessions/messages", "/api/files/download",
    "/api/academic/catalog", "/api/sessions/export",
}
WRITE_ROUTES = {
    "/api/chat/stream", "/api/sessions", "/api/files/upload", "/api/files/delete",
    "/api/memories", "/api/study/weighted-score",
    "/api/academic/analyze", "/api/academic/what-if", "/api/academic/export-ics",
    "/api/academic/sync-memory", "/api/knowledge/search",
}
account_template_path = Path(__file__).with_name("frontend") / "account.html"


class ProductStorage(RuntimeStorage):
    def list_files(self):
        files = super().list_files()
        for metadata in files:
            metadata.pop("disk_path", None)
        return files


def make_workspace_loader(directory, model_config):
    @lru_cache(maxsize=32)
    def workspace_for(account_id):
        storage = ProductStorage(str(directory / account_id), llm_config_override=model_config)
        return storage, SkillManager(storage.skills_dir, install_defaults=False)

    return workspace_for


def decode_upload(body):
    supplied_name = str(body.get("filename") or "").strip()
    if not supplied_name or "/" in supplied_name or "\\" in supplied_name:
        raise ValueError("请输入有效的文件名。")
    filename = safe_filename(supplied_name)
    ext = Path(filename).suffix.lower()
    if ext not in FILE_TYPES:
        raise ValueError("请选择 TXT、Markdown、CSV 或 Word（.docx）文件。")

    encoded = body.get("content_base64")
    plain = body.get("content_text")
    if isinstance(encoded, str) and encoded:
        if encoded.startswith("data:") and "," in encoded:
            encoded = encoded.split(",", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("文件上传内容不完整，请重新选择文件。") from exc
    elif isinstance(plain, str) and ext != ".docx":
        raw = plain.encode("utf-8")
    else:
        raise ValueError("文件内容为空。")
    if not 0 < len(raw) <= FILE_LIMIT:
        raise ValueError("单个文件须大于 0 且不超过 2 MB。")

    if ext == ".docx":
        try:
            with ZipFile(io.BytesIO(raw)) as package:
                body_part = package.getinfo("word/document.xml")
                if body_part.file_size > DOCX_XML_LIMIT:
                    raise ValueError("Word 文件正文太大，请拆分后上传。")
                markup = package.read(body_part).replace(b"\0", b"").upper()
                if b"<!DOCTYPE" in markup or b"<!ENTITY" in markup:
                    raise ValueError("此 Word 文件包含无法处理的文档结构。")
        except (BadZipFile, KeyError, RuntimeError, NotImplementedError) as exc:
            raise ValueError("无法读取此 Word 文件，请确认文件为可打开的 .docx。") from exc
    elif b"\0" in raw:
        raise ValueError("文件看起来不是文本，请导出为 TXT 或 CSV 再上传。")
    return filename, raw


def create_product_handler(base_handler):
    class ProductHandler(ProtectedBrowserRequests, base_handler):
        allowed_chat_tools = PRODUCT_TOOLS

        @property
        def storage(self):
            return self._storage

        @property
        def skill_manager(self):
            return self._skills

        def public_llm_status(self):
            config = self.storage.get_llm_config()
            return {
                "engine_ready": is_llm_configured(config),
                "model": resolve_model(config),
                "product_mode": True,
                "username": self.account.username,
            }

        def can_continue_session(self, session_id):
            return any(s["session_id"] == session_id for s in self.storage.list_sessions())

        def log_request(self, code="-", size="-"):
            if os.environ.get("MUSE_QUIET_LOG") != "1":
                self.log_message("%s %s %s", self.command, urlsplit(self.path).path, code)

        def _read_json_body(self):
            data = super()._read_json_body()
            if not isinstance(data, dict):
                raise ValueError("请求内容必须为 JSON 对象。")
            return data

        def _account_token(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE_NAME)
            except CookieError:
                return ""
            return cookie.value if cookie else ""

        def _redirect_account(self, path, token=None):
            self.send_response(303)
            self._send_cors()
            self.send_header("Location", path)
            self.send_header("Cache-Control", "no-store")
            if token is not None:
                secure = "; Secure" if self.server.secure_cookies else ""
                lifetime = SESSION_TTL if token else 0
                self.send_header("Set-Cookie", f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={lifetime}{secure}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _account_page(self, register=False, message="", status=200):
            if register and not self.server.registration_enabled:
                self._redirect_account("/login")
                return
            label = "创建账号" if register else "登录"
            action = "/register" if register else "/login"
            switch_link = '<a href="/login">已有账号，去登录</a>' if register else (
                '<a href="/register">创建一个账号</a>' if self.server.registration_enabled else ""
            )
            code_field = (
                '<label for="invite">注册邀请码</label><input name="invite" id="invite" type="password" maxlength="256" required>'
                if register and self.server.registration_code else ""
            )
            data_hint = (
                '<p class="privacy">回答资料相关问题时，所需的资料片段会发送给配置的模型服务。</p>'
                if register else ""
            )
            values = {
                "title": label, "action": action, "alternate": switch_link,
                "password_autocomplete": "new-password" if register else "current-password",
                "message": f'<p class="alert" role="alert">{escape(message)}</p>' if message else "",
                "register_code": code_field, "data_hint": data_hint,
            }
            page = self.server.account_page.format_map(values)
            self._send_bytes(page.encode("utf-8"), "text/html; charset=utf-8", status=status)

        def _authenticate(self, register):
            if register and not self.server.registration_enabled:
                self._redirect_account("/login")
                return
            if not self.server.accounts.reserve_auth_attempt(self.client_address[0]):
                self.close_connection = True
                self._account_page(register, "尝试过于频繁，请稍后再试。", status=429)
                return
            length = int(self.headers["Content-Length"])
            try:
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=8)
            except (ValueError, UnicodeDecodeError):
                self._account_page(register, "无法读取表单，请重新填写。", status=400)
                return
            username = fields.get("username", [""])[0]
            password = fields.get("password", [""])[0]
            if not self.server.auth_slots.acquire(blocking=False):
                self._account_page(register, "登录服务正忙，请稍后再试。", status=503)
                return
            try:
                if register:
                    invite = fields.get("invite", [""])[0].encode("utf-8")
                    if self.server.registration_code and not hmac.compare_digest(invite, self.server.registration_code):
                        self._account_page(register, "注册邀请码有误。", status=403)
                        return
                    account = self.server.accounts.register(username, password)
                else:
                    account = self.server.accounts.authenticate(username, password)
            except ValueError as exc:
                self._account_page(register, str(exc), status=400)
                return
            finally:
                self.server.auth_slots.release()

            if account is None:
                self._account_page(register, "账号或密码不正确。", status=401)
                return
            self.server.accounts.end_session(self._account_token())
            self._redirect_account("/", self.server.accounts.start_session(account))

        def _open_account(self):
            account = self.server.accounts.account_for_session(self._account_token())
            if not account:
                return False
            self.account = account
            self._storage, self._skills = self.server.workspace_for(account.account_id)
            return True

        def _workspace_lock(self):
            with self.server.workspace_lock_guard:
                lock = self.server.workspace_locks.get(self.account.account_id)
                if lock is None:
                    lock = Lock()
                    self.server.workspace_locks[self.account.account_id] = lock
                return lock

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._send_json({"status": "ok"})
            elif path in {"/login", "/register"}:
                self._account_page(register=(path == "/register"))
            elif path not in READ_ROUTES:
                self._send_json({"error": "页面或功能不存在。"}, status=404)
            elif self._open_account():
                super().do_GET()
            elif path in {"/", "/index.html"}:
                self._redirect_account("/login")
            else:
                self._send_json({"error": "登录已失效，请重新登录。"}, status=401)

        def _save_upload(self):
            try:
                filename, raw = decode_upload(self._read_json_body())
                current = [file for file in self.storage.list_files() if file["filename"] != filename]
                used = sum(file["size_bytes"] for file in current)
                if len(current) >= ACCOUNT_FILE_LIMIT or used + len(raw) > ACCOUNT_BYTES_LIMIT:
                    raise ValueError("个人资料空间已满，可以删除不再需要的文件。")
                metadata = self.storage.save_and_index_file(filename, raw, max_index_chars=160_000)
            except (ValueError, BadZipFile, ParseError, UnicodeError) as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            metadata.pop("disk_path", None)
            self._send_json({"ok": True, "file": metadata, "files": self.storage.list_files()})

        def _score(self):
            try:
                text = self._read_json_body().get("rows")
                if not isinstance(text, str):
                    raise ValueError("请输入学分和成绩。")
                outcome = weighted_score(text)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, **outcome})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in WRITE_ROUTES | {"/login", "/register", "/logout"}:
                self.close_connection = True
                self._send_json({"error": "页面或功能不存在。"}, status=404)
                return
            limits = {"/login": 4096, "/register": 4096, "/logout": 4096, "/api/files/upload": (FILE_LIMIT * 4 // 3) + 4096}
            if not self._accept_post(limits.get(path, 65536), require_browser_origin=True):
                return
            if path in {"/login", "/register"}:
                self._authenticate(register=(path == "/register"))
                return
            if path == "/logout":
                self.rfile.read(int(self.headers["Content-Length"]))
                self.server.accounts.end_session(self._account_token())
                self._redirect_account("/login", "")
                return
            if not self._open_account():
                self.close_connection = True
                self._send_json({"error": "登录已失效，请重新登录。"}, status=401)
                return

            if path == "/api/files/upload":
                with self._workspace_lock():
                    self._save_upload()
            elif path == "/api/files/delete":
                with self._workspace_lock():
                    super().do_POST()
            elif path == "/api/study/weighted-score":
                self._score()
            elif path == "/api/chat/stream":
                if not self.server.chat_slots.acquire(blocking=False):
                    self.close_connection = True
                    self._send_json({"error": "当前问答请求较多，请稍后再试。"}, status=429)
                    return
                try:
                    lock = self._workspace_lock()
                    if not lock.acquire(blocking=False):
                        self.close_connection = True
                        self._send_json({"error": "工作区正在处理另一项操作，请稍后重试。"}, status=409)
                        return
                    try:
                        if not self.server.accounts.reserve_chat_message(self.account, self.server.chat_limit):
                            self.close_connection = True
                            self._send_json({"error": "已达到 24 小时问答上限，请稍后再试。"}, status=429)
                            return
                        super().do_POST()
                    finally:
                        lock.release()
                finally:
                    self.server.chat_slots.release()
            else:
                super().do_POST()

    return ProductHandler


def configure_server(server, root, config, public_origin="", secure=False, registration_code="",
                     registration_enabled=True, chat_limit=50):
    server.accounts = AccountStore(root)
    server.workspace_for = make_workspace_loader(root / "workspaces", config)
    server.public_origin = public_origin
    server.secure_cookies = secure
    server.registration_enabled = registration_enabled
    server.registration_code = registration_code.encode("utf-8")
    server.workspace_lock_guard = Lock()
    server.workspace_locks = WeakValueDictionary()
    server.chat_slots = BoundedSemaphore(4)
    server.chat_limit = chat_limit
    server.auth_slots = BoundedSemaphore(4)
    server.account_page = account_template_path.read_text(encoding="utf-8")


def start_product(base_handler, base_server, local_storage):
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8766"))
    origin = (os.environ.get("MUSE_PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    loopback = host in {"127.0.0.1", "localhost"}
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.path or parsed.query or parsed.fragment:
            raise SystemExit("MUSE_PUBLIC_ORIGIN 须填写网站的 HTTPS 源地址。")
    if not loopback and not origin:
        raise SystemExit("对外绑定前请配置 MUSE_PUBLIC_ORIGIN，并通过 HTTPS 反向代理提供访问。")

    root = Path(os.environ.get("MUSE_PRODUCT_DATA_DIR", str(Path(local_storage.workspace_dir) / "product")))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = local_storage.get_llm_config() if loopback and not origin else RuntimeStorage(str(root / "service")).get_llm_config()
    if origin and not is_llm_configured(config):
        raise SystemExit("对外部署前请配置模型服务。")
    handler = create_product_handler(base_handler)
    with base_server((host, port), handler) as server:
        configure_server(server, root, config, origin, secure=bool(origin),
                         registration_code=os.environ.get("MUSE_REGISTRATION_CODE", ""),
                         registration_enabled=os.environ.get("MUSE_ALLOW_REGISTRATION", "1") != "0",
                         chat_limit=int(os.environ.get("MUSE_DAILY_CHAT_LIMIT", "50")))
        print(f"知行学业工作台 -> {origin or f'http://{host}:{port}'}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
