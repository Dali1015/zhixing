"""个人注册登录版托管入口：每位注册用户在自己的设置里填写模型 API Key。

启动方式（单端口 HTTP 服务，托管平台会自动注入 PORT）：
    PORT=8000 python app.py

与 `python run.py --product`（product_server.start_product）的区别：
1. 不要求服务端预置模型密钥：`llm_config_override` 传 None，
   每个账号在自己的工作区 SQLite 里保存各自的接口地址、Key 与模型名；
2. 不要求提前知道公网域名：public_origin 留空时，CSRF 校验退化为同 Host 校验；
3. 注册默认开放，可用 MUSE_REGISTRATION_CODE 追加邀请码、MUSE_ALLOW_REGISTRATION=0 关闭注册。
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MUSE_ACCOUNT_DATA_DIR") or (BASE_DIR / ".account_data"))

# 单用户运行时所需的种子工作区与后台守护线程：托管环境里放到数据目录并关闭
os.environ.setdefault("MUSE_WORKSPACE_DIR", str(DATA_DIR / "service"))
os.environ.setdefault("MUSE_DISABLE_DAEMON", "1")

sys.path.insert(0, str(BASE_DIR))

from backend.runtime.accounts import SESSION_TTL  # noqa: E402
from backend.runtime.llm_client import probe_llm_connection  # noqa: E402
from product_server import COOKIE_NAME, configure_server, create_product_handler  # noqa: E402
from run import AppHandler, _ZhixingHTTPServer, _public_llm_status  # noqa: E402

# 产品版默认把模型配置锁在服务端；这里把「我的模型配置」开放给已登录账号
SETTINGS_ROUTES = frozenset({"/api/settings/llm", "/api/settings/llm/test"})


class PersonalWorkspaceHandler(create_product_handler(AppHandler)):
    """账号隔离的工作台，并允许每个账号配置自己的推理引擎。"""

    def public_llm_status(self):
        status = _public_llm_status(self.storage)
        status["product_mode"] = True
        status["username"] = self.account.username
        return status

    def _secure_request(self):
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _public_host(self):
        """网关会把 Host 改写成内网地址，优先取反向代理透传的公网域名。"""
        for header in ("X-Forwarded-Host", "X-Original-Host"):
            value = (self.headers.get(header) or "").split(",")[0].strip()
            if value:
                return value
        forwarded = self.headers.get("Forwarded") or ""
        for part in forwarded.split(",")[0].split(";"):
            name, _, value = part.partition("=")
            if name.strip().lower() == "host" and value.strip().strip('"'):
                return value.strip().strip('"')
        return (self.headers.get("Host") or "").strip()

    def _accept_post(self, limit, require_browser_origin=False):
        """与 backend.runtime.http_guard 的同源校验等价，但按公网 Host 判定。

        托管网关改写了 Host，因此额外接受浏览器写入的 Sec-Fetch-Site: same-origin
        （该请求头由浏览器生成，第三方站点无法伪造），跨站请求仍然一律拒绝。
        """
        origin = self.headers.get("Origin", "")
        site = self.headers.get("Sec-Fetch-Site", "")
        public_origin = self.server.public_origin
        host = self._public_host()
        allowed = {public_origin} if public_origin else {f"https://{host}", f"http://{host}"}
        browser_same_origin = site == "same-origin"
        if (site == "cross-site"
                or (origin and origin not in allowed and not browser_same_origin)
                or (require_browser_origin and not origin and not browser_same_origin)):
            self.close_connection = True
            self._send_json({"error": "请求来源无效。"}, status=403)
            return False

        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or not 0 < int(lengths[0]) <= limit):
            self.close_connection = True
            self._send_json({"error": "请求大小无效。"}, status=413)
            return False
        return True

    def _redirect_account(self, path, token=None):
        # 与产品版一致，但 Secure 标记按当前请求协议判定（本地 http 调试不会丢 Cookie）
        self.send_response(303)
        self._send_cors()
        self.send_header("Location", path)
        self.send_header("Cache-Control", "no-store")
        if token is not None:
            lifetime = SESSION_TTL if token else 0
            secure = "; Secure" if self._secure_request() else ""
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={lifetime}{secure}",
            )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _save_llm(self, body):
        self.storage.save_llm_config(
            api_url=str(body.get("api_url", "")),
            api_key=str(body.get("api_key", "")),
            model=str(body.get("model", "")),
            search_api_key=str(body.get("search_api_key", "")),
            clear_key=bool(body.get("clear_key", False)),
        )
        self._send_json({"ok": True, "llm": self.public_llm_status()})

    def _probe_llm(self, body):
        saved = self.storage.get_llm_config()
        raw_key = str(body.get("api_key", "")).strip()
        test_cfg = {
            "api_url": str(body.get("api_url") or saved["api_url"]).strip(),
            "api_key": raw_key or saved["api_key"],
            "model": str(body.get("model") or saved["model"]).strip(),
        }
        result = probe_llm_connection(test_cfg)
        if result.get("ok"):
            self.storage.save_llm_config(
                api_url=test_cfg["api_url"],
                api_key=raw_key,
                model=test_cfg["model"],
                search_api_key=str(body.get("search_api_key", "")),
            )
            result["llm"] = self.public_llm_status()
        self._send_json(result)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in SETTINGS_ROUTES:
            super().do_POST()
            return
        if not self._accept_post(65536, require_browser_origin=True):
            return
        if not self._open_account():
            self.close_connection = True
            self._send_json({"error": "登录已失效，请重新登录。"}, status=401)
            return
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json({"error": str(exc) or "请求内容不是有效的 JSON。"}, status=400)
            return
        if path == "/api/settings/llm":
            self._save_llm(body)
        else:
            self._probe_llm(body)


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with _ZhixingHTTPServer((host, port), PersonalWorkspaceHandler) as server:
        configure_server(
            server,
            DATA_DIR,
            None,  # 关键：不做全局覆盖，每个账号读取自己工作区里的模型配置
            public_origin=(os.environ.get("MUSE_PUBLIC_ORIGIN") or "").strip(),
            secure=False,  # 由 _redirect_account 按请求协议动态判定
            registration_code=os.environ.get("MUSE_REGISTRATION_CODE", ""),
            registration_enabled=os.environ.get("MUSE_ALLOW_REGISTRATION", "1") != "0",
            chat_limit=int(os.environ.get("MUSE_DAILY_CHAT_LIMIT", "50")),
        )
        print(f"知行个人学业工作台已启动 -> http://{host}:{port}", flush=True)
        print(f"  数据目录: {DATA_DIR}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
