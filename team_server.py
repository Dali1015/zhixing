"""Invite-only Zhixing question-and-answer service for a small team."""

import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import os
from pathlib import Path
import re
import secrets
import tempfile
from threading import BoundedSemaphore
import time
from urllib.parse import parse_qs, urlsplit

from backend.runtime.http_guard import ProtectedBrowserRequests
from backend.runtime.storage import RuntimeStorage


COOKIE_NAME = "muse_team"
COOKIE_TTL = 7 * 24 * 60 * 60
TEAM_TOOLS = frozenset({"web_search", "search_knowledge"})
GET_ROUTES = {"/", "/index.html", "/api/bootstrap", "/api/sessions/messages"}
POST_ROUTES = {"/api/chat/stream", "/api/sessions"}

LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知行 · 组内试用</title>
<style>
  body { margin:16px; min-height:calc(100dvh - 32px); display:grid; place-items:center;
         font:15px/1.6 -apple-system,BlinkMacSystemFont,"Inter","PingFang SC",sans-serif; color:#13221d;
         background:radial-gradient(circle at 18% 20%, rgba(23,82,67,0.08), transparent 45%), #f5f6f1; }
  main { box-sizing:border-box; width:min(100%,400px); padding:32px; border:1px solid #dce3da;
         border-radius:18px; background:#fff; box-shadow:0 20px 50px rgba(14,42,34,0.1); }
  .mark { display:inline-grid; place-items:center; width:32px; height:32px; border-radius:9px;
          background:linear-gradient(135deg,#0d3127,#1f6552); color:#eefaf3; font-weight:800; margin-bottom:14px; }
  h1 { margin:0; font-size:26px; letter-spacing:-.035em; }
  p { margin:6px 0 22px; color:#52635b; font-size:14px; }
  label { display:block; font-size:13.5px; font-weight:600; color:#2b3f37; }
  input,button { box-sizing:border-box; width:100%; margin-top:8px; border:1px solid #c5d1c8;
                 border-radius:10px; padding:11px 12px; font:inherit; }
  input { background:#fafaf7; }
  input:focus { outline:3px solid rgba(23,82,67,0.14); border-color:#175243; background:#fff; }
  button { margin-top:14px; background:#175243; color:#fff; border-color:#175243; font-weight:650; cursor:pointer; }
  button:hover { background:#0f3d31; }
  [role=alert] { padding:9px 11px; border-radius:8px; background:#fef2f2; border:1px solid #fecaca;
                 color:#991b1b; margin:0 0 14px; font-size:13.5px; }
</style>
</head>
<body><main>
<div class="mark" aria-hidden="true">知</div>
<h1>知行</h1>
<p>输入团队邀请码，进入独立隔离的组内问答试用空间。</p>
{error}
<form action="/login" method="post">
  <label for="code">邀请码</label>
  <input id="code" name="code" type="password" autocomplete="current-password" required maxlength="256">
  <button type="submit">进入对话</button>
</form>
</main></body></html>"""


class Invite:
    def __init__(self, code: str, signing_key: str):
        if len(code) < 12 or len(signing_key) < 24 or code == signing_key:
            raise ValueError("请配置至少 12 位的邀请码和独立的 24 位以上签名密钥。")
        self._code_digest = hashlib.sha256(code.encode()).digest()
        self._key = signing_key.encode()

    def accepts(self, submitted: str) -> bool:
        return hmac.compare_digest(self._code_digest, hashlib.sha256(submitted.encode()).digest())

    def _signature(self, value: str) -> str:
        return hmac.new(self._key, self._code_digest + value.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        value = f"{secrets.token_hex(16)}.{int(time.time()) + COOKIE_TTL}"
        return f"{value}.{self._signature(value)}"

    def session_id(self, header: str) -> str:
        try:
            cookie = SimpleCookie(header).get(COOKIE_NAME)
        except CookieError:
            return ""
        if not cookie or not re.fullmatch(r"[0-9a-f]{32}\.[0-9]{1,12}\.[0-9a-f]{64}", cookie.value):
            return ""
        session_id, expiry, sig = cookie.value.split(".")
        if int(expiry) < time.time() or not hmac.compare_digest(sig, self._signature(f"{session_id}.{expiry}")):
            return ""
        return session_id


def create_handler(base_handler):
    class TeamHandler(ProtectedBrowserRequests, base_handler):
        allowed_chat_tools = TEAM_TOOLS

        @property
        def storage(self):
            return self._private_storage

        def public_llm_status(self):
            status = super().public_llm_status()
            return {"engine_ready": status["engine_ready"], "model": status["model"], "team_mode": True}

        def can_continue_session(self, session_id):
            return any(s["session_id"] == session_id for s in self.storage.list_sessions())

        def _redirect(self, path, token=""):
            self.send_response(303)
            self._send_cors()
            self.send_header("Location", path)
            self.send_header("Cache-Control", "no-store")
            if token:
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_TTL}; Secure; HttpOnly; SameSite=Lax",
                )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _login_page(self, failed=False):
            error = '<p role="alert">邀请码错误，请确认后重试。</p>' if failed else ""
            page = LOGIN_PAGE.replace("{error}", error).encode("utf-8")
            self._send_bytes(page, "text/html; charset=utf-8", status=401 if failed else 200)

        def _open_workspace(self):
            session_id = self.server.invite.session_id(self.headers.get("Cookie", ""))
            if not session_id:
                return False
            self._private_storage = RuntimeStorage(str(self.server.data_dir / session_id))
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._send_json({"status": "ok"})
            elif path == "/login":
                self._login_page()
            elif path not in GET_ROUTES:
                self._send_json({"error": "此功能未在组内试用开放。"}, status=404)
            elif self._open_workspace():
                super().do_GET()
            elif path in {"/", "/index.html"}:
                self._redirect("/login")
            else:
                self._send_json({"error": "请重新打开首页并输入邀请码。"}, status=401)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in POST_ROUTES | {"/login"}:
                self.close_connection = True
                self._send_json({"error": "此功能未在组内试用开放。"}, status=404)
                return
            if not self._accept_post(4096 if path == "/login" else 32768):
                return
            if path == "/login":
                length = int(self.headers["Content-Length"])
                payload = self.rfile.read(length).decode("utf-8", errors="replace")
                code = parse_qs(payload).get("code", [""])[0]
                if self.server.invite.accepts(code):
                    self._redirect("/", self.server.invite.issue())
                else:
                    self._login_page(failed=True)
                return
            if not self._open_workspace():
                self.close_connection = True
                self._send_json({"error": "请重新打开首页并输入邀请码。"}, status=401)
                return

            if path != "/api/chat/stream":
                super().do_POST()
                return
            if not self.server.chat_slots.acquire(blocking=False):
                self.close_connection = True
                self._send_json({"error": "正在处理其他答复，请稍后再试。"}, status=429)
                return
            try:
                super().do_POST()
            finally:
                self.server.chat_slots.release()

    return TeamHandler


def main():
    root = Path(os.environ.get("MUSE_TEAM_DATA_DIR", str(Path(tempfile.gettempdir()) / "muse-team")))
    invite = Invite(os.environ.get("MUSE_TEAM_CODE", ""), os.environ.get("MUSE_TEAM_COOKIE_KEY", ""))
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        raise SystemExit("请在托管平台配置 DEEPSEEK_API_KEY。")

    os.environ["MUSE_WORKSPACE_DIR"] = str(root / "service")
    os.environ["MUSE_DISABLE_DAEMON"] = "1"
    from run import AppHandler, _ZhixingHTTPServer

    root = root / "visitors"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _ZhixingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), create_handler(AppHandler)) as server:
        server.invite = invite
        server.data_dir = root
        server.public_origin = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        server.chat_slots = BoundedSemaphore(4)
        print(f"知行团队试用服务监听端口 {server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
