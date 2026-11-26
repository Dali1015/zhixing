"""Shared HTTP protections for browser-facing deployments."""

import re


class ProtectedBrowserRequests:
    def _send_cors(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        # Same-origin form POSTs need the Origin header for the checks below.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )

    def _accept_post(self, limit, require_browser_origin=False):
        origin = self.headers.get("Origin", "")
        site = self.headers.get("Sec-Fetch-Site", "")
        host = self.headers.get("Host", "")
        public_origin = self.server.public_origin
        allowed = {public_origin} if public_origin else {f"https://{host}", f"http://{host}"}
        if (site == "cross-site" or (origin and origin not in allowed)
                or (require_browser_origin and not origin and site != "same-origin")):
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

    def do_OPTIONS(self):
        self._send_json({"error": "请求方式不受支持。"}, status=405)
