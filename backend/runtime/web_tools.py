# -*- coding: utf-8 -*-
"""
知行 Agent Runtime — 真·全网搜索引擎、网页正文+导航链接提取与记忆驱动动态流守护器
核心加固：
1. URL 自动百分号编码（彻底修复百度百科/维基等含中文路径 URL 报 ascii 编码异常）；
2. 国内站点（.cn / 教育网 .edu.cn / 搜索门户）自动优先绕过系统残留代理直连 + 兼容高校老旧 TLS 握手（彻底解决 _ssl.c EOF 报错）；
3. 多引擎交叉全网搜索（360搜索直连解析真实 data-mdurl + 搜狗网页 + Bing CN 简中 Cookie + DuckDuckGo）+ 查询词相关性校验网关（自动过滤反爬假结果如“河南5A景区”）；
4. 网页抓取同时提取正文、文档附件与页面内有效超链接列表 (links)，支持智能体从学校/机构主页顺藤摸瓜精准进入二级学院与栏目页。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout
from datetime import datetime
from html.parser import HTMLParser
import json
import re
import ssl
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlparse, urlsplit, urlunsplit
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from .storage import decode_bytes_auto, tokenize_cjk_fts


_SEARCH_WORKERS = ThreadPoolExecutor(max_workers=12, thread_name_prefix="zhixing-search")

DIRECT_CN_DOMAINS = (
    ".cn",
    "baidu.com",
    "bing.com",
    "so.com",
    "sogou.com",
    "zhihu.com",
    "bilibili.com",
    "gitee.com",
    "csdn.net",
    "juejin.cn",
    "127.0.0.1",
    "localhost",
)


def normalize_http_url(raw_url: str) -> str:
    """对含中文或特殊字符的 HTTP URL 做标准百分号转义与 IDNA 域名编码，防止 urllib ASCII 报错"""
    u = (raw_url or "").strip()
    if not u:
        return ""
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return u

    host = parts.hostname or ""
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except Exception:
        ascii_host = host

    netloc = ascii_host
    if parts.port:
        netloc = f"{ascii_host}:{parts.port}"

    safe_path = quote(unquote(parts.path or "/"), safe="/:@!$&'()*+,;=-._~%")
    safe_query = quote(unquote(parts.query or ""), safe="=&/:@!$'()*+,;-._~%")
    safe_frag = quote(unquote(parts.fragment or ""), safe="=&/:@!$'()*+,;-._~%")
    return urlunsplit((parts.scheme, netloc, safe_path, safe_query, safe_frag))


def _make_ssl_context(legacy_fallback: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT")
    if legacy_fallback:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        except Exception:
            pass
    return ctx


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 8) -> bytes:
    deadline = time.monotonic() + timeout
    safe_url = normalize_http_url(url)
    host = (urlparse(safe_url).hostname or "").lower()
    prefer_direct = any(host.endswith(d) or d in host for d in DIRECT_CN_DOMAINS)

    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(safe_url, headers=hdrs)

    # 1) 国内域名与 .edu.cn 优先绕过系统代理直连（防止代理节点拦截或 TLS EOF）
    if prefer_direct:
        for use_legacy in (False, True):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=_make_ssl_context(legacy_fallback=use_legacy)),
                urllib.request.HTTPHandler(),
            )
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("网页请求超时")
                with opener.open(req, timeout=remaining) as resp:
                    return resp.read(650_000)
            except HTTPError:
                raise
            except URLError as exc:
                if not use_legacy and ("SSL" in str(exc).upper() or "EOF" in str(exc).upper()):
                    continue
                break

    # 2) 默认系统网络通道（海外站点如 GitHub / arXiv / DuckDuckGo 走此通道）
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_make_ssl_context(legacy_fallback=False)),
        urllib.request.HTTPHandler(),
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("网页请求超时")
    with opener.open(req, timeout=remaining) as resp:
        return resp.read(650_000)


# ---------------- 1. 多引擎全网搜索与相关性反欺诈过滤 ----------------
class _So360HTMLParser(HTMLParser):
    """解析 360 搜索 (so.com) 结果列表，优先提取 data-mdurl 真实目标直链"""

    def __init__(self):
        super().__init__()
        self.in_res_li = False
        self.li_depth = 0
        self.depth = 0
        self.in_h3 = False
        self.in_a = False
        self.in_desc = False
        self.curr_url = ""
        self.curr_title: List[str] = []
        self.curr_snippet: List[str] = []
        self.results: List[Dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        self.depth += 1
        amap = dict(attrs)
        cls = amap.get("class", "")
        if t == "li" and "res-list" in cls:
            self.in_res_li = True
            self.li_depth = self.depth
            self.curr_url = ""
            self.curr_title = []
            self.curr_snippet = []
        elif self.in_res_li:
            if t == "h3":
                self.in_h3 = True
            elif t == "a" and self.in_h3 and not self.curr_url:
                self.in_a = True
                # data-mdurl 保存了未经跳转包装的原始目标 URL
                raw_u = amap.get("data-mdurl") or amap.get("href") or ""
                if "so.com/link?" in raw_u and "url=" in raw_u:
                    qs = parse_qs(urlparse(raw_u).query)
                    raw_u = unquote(qs.get("url", [raw_u])[0])
                self.curr_url = raw_u
            elif t in ("p", "div", "span") and any(k in cls for k in ("res-desc", "res-comm-con", "summary")):
                self.in_desc = True

    def handle_data(self, data):
        if not self.in_res_li:
            return
        if self.in_a:
            self.curr_title.append(data)
        elif self.in_desc:
            self.curr_snippet.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if self.in_res_li:
            if t == "a" and self.in_a:
                self.in_a = False
            elif t == "h3":
                self.in_h3 = False
            elif t in ("p", "div", "span") and self.in_desc:
                self.in_desc = False
            elif t == "li" and self.depth == self.li_depth:
                self.in_res_li = False
                title = re.sub(r"\s+", " ", "".join(self.curr_title)).strip()
                snippet = re.sub(r"\s+", " ", "".join(self.curr_snippet)).strip()
                if title and self.curr_url.startswith("http"):
                    self.results.append({"title": title, "url": self.curr_url, "snippet": snippet[:280]})
        if self.depth > 0:
            self.depth -= 1


class _SogouHTMLParser(HTMLParser):
    """解析搜狗网页搜索 (sogou.com/web) 结果"""

    def __init__(self):
        super().__init__()
        self.in_vr = False
        self.vr_depth = 0
        self.depth = 0
        self.in_h3 = False
        self.in_a = False
        self.in_desc = False
        self.curr_url = ""
        self.curr_title: List[str] = []
        self.curr_snippet: List[str] = []
        self.results: List[Dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        self.depth += 1
        amap = dict(attrs)
        cls = amap.get("class", "")
        if t == "div" and ("vrwrap" in cls or "rb" in cls.split()):
            self.in_vr = True
            self.vr_depth = self.depth
            self.curr_url = ""
            self.curr_title = []
            self.curr_snippet = []
        elif self.in_vr:
            if t == "h3":
                self.in_h3 = True
            elif t == "a" and self.in_h3 and not self.curr_url:
                self.in_a = True
                href = amap.get("href", "")
                if href.startswith("/link?"):
                    href = "https://www.sogou.com" + href
                self.curr_url = href
            elif t in ("p", "div") and any(k in cls for k in ("str-text-info", "star-wiki", "space-txt", "ft")):
                self.in_desc = True

    def handle_data(self, data):
        if not self.in_vr:
            return
        if self.in_a:
            self.curr_title.append(data)
        elif self.in_desc:
            self.curr_snippet.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if self.in_vr:
            if t == "a" and self.in_a:
                self.in_a = False
            elif t == "h3":
                self.in_h3 = False
            elif t in ("p", "div") and self.in_desc:
                self.in_desc = False
            elif t == "div" and self.depth == self.vr_depth:
                self.in_vr = False
                title = re.sub(r"\s+", " ", "".join(self.curr_title)).strip()
                snippet = re.sub(r"\s+", " ", "".join(self.curr_snippet)).strip()
                if title and self.curr_url.startswith("http"):
                    self.results.append({"title": title, "url": self.curr_url, "snippet": snippet[:280]})
        if self.depth > 0:
            self.depth -= 1


class _BingHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_algo = False
        self.algo_depth = 0
        self.depth = 0
        self.in_h2 = False
        self.in_a = False
        self.in_p = False
        self.curr_url = ""
        self.curr_title: List[str] = []
        self.curr_snippet: List[str] = []
        self.results: List[Dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        self.depth += 1
        amap = dict(attrs)
        cls = amap.get("class", "")
        if t == "li" and "b_algo" in cls:
            self.in_algo = True
            self.algo_depth = self.depth
            self.curr_url = ""
            self.curr_title = []
            self.curr_snippet = []
        elif self.in_algo:
            if t == "h2":
                self.in_h2 = True
            elif t == "a" and self.in_h2 and not self.curr_url:
                self.in_a = True
                self.curr_url = amap.get("href", "")
            elif t == "p":
                self.in_p = True

    def handle_data(self, data):
        if not self.in_algo:
            return
        if self.in_a:
            self.curr_title.append(data)
        elif self.in_p:
            self.curr_snippet.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if self.in_algo:
            if t == "a" and self.in_a:
                self.in_a = False
            elif t == "h2":
                self.in_h2 = False
            elif t == "p":
                self.in_p = False
            elif t == "li" and self.depth == self.algo_depth:
                self.in_algo = False
                title = re.sub(r"\s+", " ", "".join(self.curr_title)).strip()
                snippet = re.sub(r"\s+", " ", "".join(self.curr_snippet)).strip()
                if title and self.curr_url.startswith("http"):
                    self.results.append({"title": title, "url": self.curr_url, "snippet": snippet[:280]})
        if self.depth > 0:
            self.depth -= 1


class _DDGHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title_a = False
        self.in_snippet = False
        self.curr_url = ""
        self.curr_title: List[str] = []
        self.curr_snippet: List[str] = []
        self.results: List[Dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        amap = dict(attrs)
        cls = amap.get("class", "")
        if t == "a" and "result__a" in cls:
            if self.curr_title and self.curr_url:
                self._flush()
            self.in_title_a = True
            raw_href = amap.get("href", "")
            if "uddg=" in raw_href:
                qs = parse_qs(urlparse(raw_href).query)
                self.curr_url = unquote(qs.get("uddg", [raw_href])[0])
            else:
                self.curr_url = raw_href
            self.curr_title = []
            self.curr_snippet = []
        elif t in ("a", "div", "span") and "result__snippet" in cls:
            self.in_snippet = True

    def handle_data(self, data):
        if self.in_title_a:
            self.curr_title.append(data)
        elif self.in_snippet:
            self.curr_snippet.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "a" and self.in_title_a:
            self.in_title_a = False
        elif self.in_snippet and t in ("a", "div", "span"):
            self.in_snippet = False
            self._flush()

    def _flush(self):
        title = re.sub(r"\s+", " ", "".join(self.curr_title)).strip()
        snippet = re.sub(r"\s+", " ", "".join(self.curr_snippet)).strip()
        if title and self.curr_url.startswith("http"):
            self.results.append({"title": title, "url": self.curr_url, "snippet": snippet[:280]})
        self.curr_title = []
        self.curr_url = ""
        self.curr_snippet = []


def parse_so360_search_html(html_text: str, max_results: int = 8) -> List[Dict[str, str]]:
    p = _So360HTMLParser()
    p.feed(html_text)
    return p.results[:max_results]


def parse_sogou_search_html(html_text: str, max_results: int = 8) -> List[Dict[str, str]]:
    p = _SogouHTMLParser()
    p.feed(html_text)
    return p.results[:max_results]


def parse_bing_search_html(html_text: str, max_results: int = 8) -> List[Dict[str, str]]:
    p = _BingHTMLParser()
    p.feed(html_text)
    return p.results[:max_results]


def parse_ddg_search_html(html_text: str, max_results: int = 8) -> List[Dict[str, str]]:
    p = _DDGHTMLParser()
    p.feed(html_text)
    if p.curr_title and p.curr_url:
        p._flush()
    return p.results[:max_results]


def _score_and_filter_results(query: str, items: List[Dict[str, str]], max_results: int) -> List[Dict[str, str]]:
    """
    对检索结果做关键词/二元词相关性打分与去伪过滤：
    拦截搜索引擎在无 Cookie 时退化返回的泛化假结果（如搜“河南师范大学软件学院”却返回“河南5A景区/百度地图”）。
    """
    clean_q = re.sub(r"site:\S+", "", query).strip()
    q_toks = {t for t in tokenize_cjk_fts(clean_q).split() if len(t) >= 2}
    long_terms = [{t for t in tokenize_cjk_fts(term).split() if len(t) == 2}
                  for term in re.findall(r"[\u4e00-\u9fff]{4,}", clean_q)]
    site_m = re.search(r"site:([A-Za-z0-9.\-]+)", query)
    site_domain = site_m.group(1).lower() if site_m else ""

    if not q_toks and not site_domain:
        return items[:max_results]

    # 若查询词较长（>=3 个二元词），至少需命中 >=2 个二元词（或 >=35% 比例）才视为有效相关结果
    min_req = 1 if len(q_toks) <= 2 else max(2, int(len(q_toks) * 0.34))

    scored: List[Tuple[int, Dict[str, str]]] = []
    seen_urls = set()
    for it in items:
        u = it.get("url", "")
        if not u or u in seen_urls:
            continue
        host = (urlsplit(u).hostname or "").lower()
        if site_domain and host != site_domain and not host.endswith("." + site_domain):
            continue
        doc_toks = {t for t in tokenize_cjk_fts(f"{it.get('title', '')} {it.get('snippet', '')}").split() if len(t) >= 2}
        hit_cnt = len(q_toks & doc_toks)
        if q_toks and hit_cnt < min_req:
            continue
        if long_terms and not any(len(term & doc_toks) * 3 >= len(term) * 2 for term in long_terms):
            continue
        seen_urls.add(u)
        scored.append((hit_cnt, it))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:max_results]]


def web_search(query: str, max_results: int = 6, search_api_key: str = "", timeout: int = 8) -> Dict[str, Any]:
    """搜索引擎共享一个时间预算，返回在预算内收集到的相关结果。"""
    q = query.strip()
    if not q:
        return {"ok": False, "engine": "none", "results": [], "error": "搜索词为空"}

    deadline = time.monotonic() + timeout
    if search_api_key.strip():
        def search_tavily():
            ctx = _make_ssl_context()
            payload = json.dumps({
                "api_key": search_api_key.strip(),
                "query": q,
                "max_results": max_results,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=min(3, timeout / 2), context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:280]}
                for r in (data.get("results") or [])[:max_results]
            ]

        task = _SEARCH_WORKERS.submit(search_tavily)
        try:
            res = task.result(timeout=min(3, timeout / 2))
            if res:
                return {"ok": True, "engine": "tavily", "results": res, "error": ""}
        except Exception:
            task.cancel()

    engines = [
        (
            "360so",
            f"https://www.so.com/s?q={quote_plus(q)}",
            {"Referer": "https://www.so.com/"},
            parse_so360_search_html,
        ),
        (
            "sogou",
            f"https://www.sogou.com/web?query={quote_plus(q)}",
            {"Referer": "https://www.sogou.com/"},
            parse_sogou_search_html,
        ),
        (
            "bing",
            f"https://cn.bing.com/search?q={quote_plus(q)}&ensearch=0",
            {"Cookie": "_EDGE_S=mkt=zh-cn; SRCHHPGUSR=SRCHLANG=zh-Hans"},
            parse_bing_search_html,
        ),
        (
            "duckduckgo",
            f"https://html.duckduckgo.com/html/?q={quote_plus(q)}",
            {},
            parse_ddg_search_html,
        ),
    ]

    def search_public(url, headers, parser):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        html_text = decode_bytes_auto(_http_get(url, headers=headers, timeout=remaining))
        return parser(html_text, max_results=max_results + 4)

    pending = {
        _SEARCH_WORKERS.submit(search_public, url, headers, parser): name
        for name, url, headers, parser in engines
    }
    aggregated = []
    used_engines = []
    try:
        for task in as_completed(pending, timeout=max(0, deadline - time.monotonic())):
            try:
                items = _score_and_filter_results(q, task.result(), max_results)
            except Exception:
                continue
            if not items:
                continue
            used_engines.append(pending[task])
            aggregated = _score_and_filter_results(q, aggregated + items, max_results)
            if len(aggregated) >= max_results:
                break
    except FutureTimeout:
        pass
    finally:
        for task in pending:
            task.cancel()

    if aggregated:
        return {
            "ok": True,
            "engine": "+".join(used_engines),
            "results": aggregated[:max_results],
            "error": "",
        }

    return {
        "ok": False,
        "engine": "none",
        "results": [],
        "error": "未检索到与查询词高度匹配的公开结果（建议精简关键词或直接用 web_fetch 访问目标站点主页提取导航链接）",
    }


# ---------------- 2. 网页正文、附件与页面内导航链接提取 ----------------
class _WebpageContentParser(HTMLParser):
    MAIN_RE = re.compile(
        r"(wp_articlecontent|v_news_content|read_content|article[-_]content|entry[-_]content|news_content|markdown-body|post-content|lemmaSummary|J-lemma-content)",
        re.IGNORECASE,
    )
    ATT_EXTS = (".docx", ".doc", ".pdf", ".xlsx", ".xls", ".csv", ".zip", ".rar")

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.in_title = False
        self.title_parts: List[str] = []
        self.skip_depth = 0
        self.depth = 0
        self.main_depth: Optional[int] = None
        self.main_chunks: List[str] = []
        self.body_chunks: List[str] = []
        self.in_a = False
        self.curr_href = ""
        self.curr_a_title = ""
        self.curr_a_txt: List[str] = []
        self.attachments: List[Dict[str, str]] = []
        self.links: List[Dict[str, str]] = []
        self._seen_att = set()
        self._seen_links = set()

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        self.depth += 1
        amap = dict(attrs)
        if t == "title":
            self.in_title = True
        if t in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
        if t in ("div", "article", "section", "main"):
            cid = f"{amap.get('class', '')} {amap.get('id', '')}"
            if self.main_depth is None and self.MAIN_RE.search(cid):
                self.main_depth = self.depth
        if t == "a":
            self.in_a = True
            self.curr_href = (amap.get("href") or "").strip()
            self.curr_a_title = (amap.get("title") or "").strip()
            self.curr_a_txt = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.skip_depth > 0:
            return
        if self.in_a:
            self.curr_a_txt.append(data)
        s = re.sub(r"\s+", " ", data).strip()
        if not s:
            return
        if self.main_depth is not None:
            self.main_chunks.append(s)
        elif len(s) >= 2:
            self.body_chunks.append(s)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self.in_title = False
        if t in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div"):
            target = self.main_chunks if self.main_depth is not None else self.body_chunks
            target.append("\n")
        if t == "a" and self.in_a:
            self.in_a = False
            raw_h = self.curr_href
            if raw_h and not raw_h.startswith(("javascript:", "#", "mailto:", "tel:")):
                furl = urljoin(self.base_url, raw_h)
                inner_txt = re.sub(r"\s+", " ", "".join(self.curr_a_txt)).strip()
                label = self.curr_a_title if len(self.curr_a_title) > len(inner_txt) else inner_txt
                hl = raw_h.lower().split("?")[0]
                if any(hl.endswith(ext) for ext in self.ATT_EXTS):
                    att_name = label or furl.rsplit("/", 1)[-1]
                    if furl not in self._seen_att:
                        self._seen_att.add(furl)
                        self.attachments.append({"name": att_name[:80], "url": furl})
                elif furl.startswith(("http://", "https://")) and len(label) >= 2 and furl not in self._seen_links:
                    self._seen_links.add(furl)
                    self.links.append({"text": label[:100], "url": furl})

        if t in ("script", "style", "noscript", "svg") and self.skip_depth > 0:
            self.skip_depth -= 1
        if self.main_depth is not None and self.depth == self.main_depth:
            self.main_depth = None
        if self.depth > 0:
            self.depth -= 1


def extract_webpage_content(url: str, html_text: str, related_query: str = "") -> Dict[str, Any]:
    p = _WebpageContentParser(url)
    p.feed(html_text)
    raw = " ".join(p.main_chunks if len(p.main_chunks) >= 3 else p.body_chunks)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
    cleaned = "\n".join(ln for ln in lines if ln)[:8000]
    links = p.links
    query_terms = {t for t in tokenize_cjk_fts(related_query).split() if len(t) > 1}
    if query_terms:
        links = sorted(
            p.links,
            key=lambda link: len(query_terms.intersection(tokenize_cjk_fts(link["text"]).split())),
            reverse=True,
        )
    return {
        "ok": bool(cleaned or p.links),
        "url": url,
        "title": re.sub(r"\s+", " ", "".join(p.title_parts)).strip(),
        "content": cleaned,
        "links": links[:40],
        "attachments": p.attachments,
        "error": "",
    }


def web_fetch(url: str, timeout: int = 8, related_query: str = "") -> Dict[str, Any]:
    if not url or not url.strip().startswith("http"):
        return {"ok": False, "url": url, "title": "", "content": "", "links": [], "attachments": [], "error": "无效的 HTTP URL"}
    norm_u = normalize_http_url(url)
    try:
        raw = _http_get(norm_u, timeout=timeout)
        return extract_webpage_content(norm_u, decode_bytes_auto(raw), related_query)
    except Exception as exc:
        return {"ok": False, "url": url, "title": "", "content": "", "links": [], "attachments": [], "error": str(exc)[:140]}


# ---------------- 3. 多源信息流连接器与守护巡检 ----------------
class _ListLinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.in_a = False
        self.in_meta = False
        self.href = ""
        self.title_attr = ""
        self.a_txt: List[str] = []
        self.meta_txt: List[str] = []
        self.seen = set()
        self.items: List[Dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        amap = dict(attrs)
        if t == "a":
            self.in_a = True
            self.href = (amap.get("href") or "").strip()
            self.title_attr = (amap.get("title") or "").strip()
            self.a_txt = []
        elif t == "span" and "meta" in amap.get("class", ""):
            self.in_meta = True
            self.meta_txt = []

    def handle_data(self, data):
        if self.in_a:
            self.a_txt.append(data)
        if self.in_meta:
            self.meta_txt.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "span" and self.in_meta:
            self.in_meta = False
            m = re.search(r"(202\d-\d{2}-\d{2})", "".join(self.meta_txt))
            if m and self.items:
                self.items[-1]["published_at"] = m.group(1)
        elif t == "a" and self.in_a:
            self.in_a = False
            inner = re.sub(r"\s+", " ", "".join(self.a_txt)).strip()
            title = self.title_attr if len(self.title_attr) > len(inner) else inner
            if len(title) < 8 or not self.href or self.href.startswith(("javascript:", "#", "mailto:")):
                return
            is_article = bool(re.search(r"/202\d/|/c\d+a\d+/|page\.htm|info/\d+/\d+|\.html?$", self.href))
            if not is_article and len(title) < 12:
                return
            if title in self.seen:
                return
            self.seen.add(title)
            full_url = urljoin(self.base_url, self.href)
            pub = datetime.now().strftime("%Y-%m-%d")
            md = re.search(r"/(202\d)/(\d{2})(\d{2})/", full_url)
            if md:
                pub = f"{md.group(1)}-{md.group(2)}-{md.group(3)}"
            self.items.append({"title": title[:100], "url": full_url, "published_at": pub})


def parse_rss_or_atom(xml_text: str, source_id: str, source_name: str, limit: int = 6) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_text)
    out: List[Dict[str, Any]] = []
    rss_items = root.findall(".//item")
    if rss_items:
        for it in rss_items[:limit]:
            title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
            link = (it.findtext("link") or "").strip()
            desc = re.sub(r"<[^>]+>", "", (it.findtext("description") or "")).strip()
            if title:
                out.append({
                    "item_id": "rss_" + uuid.uuid5(uuid.NAMESPACE_URL, link or title).hex[:8],
                    "source_id": source_id,
                    "source_name": source_name,
                    "title": title[:100],
                    "url": link,
                    "summary": desc[:180],
                    "published_at": datetime.now().strftime("%Y-%m-%d"),
                })
        return out

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for ent in root.findall(".//atom:entry", ns)[:limit]:
        title = re.sub(r"\s+", " ", (ent.findtext("atom:title", "", ns) or "")).strip()
        summary = re.sub(r"\s+", " ", (ent.findtext("atom:summary", "", ns) or "")).strip()
        link = (ent.findtext("atom:id", "", ns) or "").strip()
        link_el = ent.find("atom:link", ns)
        if not link and link_el is not None:
            link = link_el.attrib.get("href", "")
        pub = (ent.findtext("atom:published", "", ns) or datetime.now().strftime("%Y-%m-%d"))[:10]
        if title:
            out.append({
                "item_id": "atom_" + uuid.uuid5(uuid.NAMESPACE_URL, link or title).hex[:8],
                "source_id": source_id,
                "source_name": source_name,
                "title": title[:100],
                "url": link,
                "summary": summary[:200],
                "full_content": summary,
                "published_at": pub,
            })
    return out


def fetch_source_items(src: Dict[str, Any], search_api_key: str = "", timeout: int = 7) -> List[Dict[str, Any]]:
    sid = src["source_id"]
    sname = src["name"]
    kind = src["kind"]
    target = src["target"]

    if kind == "github":
        url = f"https://api.github.com/search/repositories?q={quote_plus(target)}&sort=stars&order=desc&per_page=5"
        data = json.loads(decode_bytes_auto(_http_get(url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)))
        items = []
        for repo in (data.get("items") or [])[:5]:
            fn = repo.get("full_name", "")
            stars = repo.get("stargazers_count", 0)
            desc = (repo.get("description") or "").strip()
            lang = repo.get("language") or "Code"
            items.append({
                "item_id": "gh_" + uuid.uuid5(uuid.NAMESPACE_URL, repo.get("html_url", fn)).hex[:8],
                "source_id": sid,
                "source_name": f"GitHub · ★{stars}",
                "title": f"{fn} — {desc[:60]}" if desc else fn,
                "url": repo.get("html_url", "https://github.com"),
                "summary": f"{lang} · ★ {stars} · {desc[:140]}",
                "published_at": (repo.get("updated_at") or "2026-11-25")[:10],
            })
        return items

    if kind == "arxiv":
        url = f"https://export.arxiv.org/api/query?search_query={target}&sortBy=submittedDate&sortOrder=descending&max_results=5"
        xml_txt = decode_bytes_auto(_http_get(url, timeout=timeout))
        return parse_rss_or_atom(xml_txt, sid, sname, limit=5)

    if kind == "search_query":
        sr = web_search(target, max_results=5, search_api_key=search_api_key, timeout=timeout)
        return [
            {
                "item_id": "sq_" + uuid.uuid5(uuid.NAMESPACE_URL, r["url"] or r["title"]).hex[:8],
                "source_id": sid,
                "source_name": sname,
                "title": r["title"][:100],
                "url": r["url"],
                "summary": r["snippet"],
                "published_at": datetime.now().strftime("%Y-%m-%d"),
            }
            for r in sr.get("results", [])
        ]

    raw_txt = decode_bytes_auto(_http_get(target, timeout=timeout))
    if kind == "rss" or raw_txt.lstrip().startswith("<?xml") or "<rss" in raw_txt[:300] or "<feed" in raw_txt[:300]:
        return parse_rss_or_atom(raw_txt, sid, sname, limit=6)

    lp = _ListLinkParser(target)
    lp.feed(raw_txt)
    return [
        {
            "item_id": "web_" + uuid.uuid5(uuid.NAMESPACE_URL, it["url"]).hex[:8],
            "source_id": sid,
            "source_name": sname,
            "title": it["title"],
            "url": it["url"],
            "summary": f"来自 {sname}",
            "published_at": it["published_at"],
        }
        for it in lp.items[:6]
    ]


def sync_all_sources(storage: Any, timeout: int = 7) -> Dict[str, Any]:
    sources = [s for s in storage.list_feed_sources() if s.get("enabled")]
    skey = storage.get_llm_config().get("search_api_key", "")
    reports: List[Dict[str, Any]] = []
    collected: List[Dict[str, Any]] = []

    def _one(src: Dict[str, Any]):
        try:
            items = fetch_source_items(src, search_api_key=skey, timeout=timeout)
            storage.mark_source_synced(src["source_id"])
            return {"report": {"source_id": src["source_id"], "name": src["name"], "ok": True, "count": len(items), "error": ""}, "items": items}
        except Exception as exc:
            return {"report": {"source_id": src["source_id"], "name": src["name"], "ok": False, "count": 0, "error": str(exc)[:90]}, "items": []}

    if sources:
        with ThreadPoolExecutor(max_workers=min(6, len(sources))) as pool:
            for r in pool.map(_one, sources):
                reports.append(r["report"])
                collected.extend(r["items"])

    added = storage.upsert_feed_items(collected) if collected else 0
    return {"fetched_count": len(collected), "newly_added": added, "source_reports": reports}


def rank_feed_by_memories(items: List[Dict[str, Any]], memories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """根据用户真实长期记忆库动态计算每条信息的关联分与推荐原因"""
    mem_entries = []
    for m in memories:
        toks = {t for t in tokenize_cjk_fts(m["content"]).split() if len(t) >= 2}
        mem_entries.append((m, toks))

    ranked: List[Dict[str, Any]] = []
    for it in items:
        text = f"{it.get('title', '')} {it.get('source_name', '')} {it.get('summary', '')}"
        it_toks = {t for t in tokenize_cjk_fts(text).split() if len(t) >= 2}
        score = 68 + (6 if it.get("saved") else 0)
        matched_snippets: List[str] = []

        for m, m_toks in mem_entries:
            overlap = it_toks & m_toks
            if overlap:
                weight = 6 if m["category"] in ("goal", "profile") else 4
                score += min(18, len(overlap) * weight)
                matched_snippets.append(m["content"][:22])

        reason = (
            f"关联记忆：{matched_snippets[0]}"
            if matched_snippets
            else (it.get("summary") or f"订阅自 {it.get('source_name', '')}")[:48]
        )
        ranked.append({
            **it,
            "relevance_score": max(60, min(99, score)),
            "relevance_reason": reason,
        })

    ranked.sort(key=lambda x: (1 if x.get("saved") else 0, x["relevance_score"], x.get("published_at", "")), reverse=True)
    return ranked


def start_feed_daemon(storage: Any, interval_sec: int = 1800) -> threading.Thread:
    """后台非阻塞守护线程：启动后延迟2秒首次拉取真实动态，此后定时增量更新"""
    def _loop():
        time.sleep(2.0)
        while True:
            try:
                sync_all_sources(storage, timeout=7)
            except Exception:
                pass
            time.sleep(interval_sec)

    th = threading.Thread(target=_loop, name="ZhixingFeedDaemon", daemon=True)
    th.start()
    return th
