"""One audited public recruitment site, not an arbitrary browser/Shell tool."""

import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import build_opener, Request

from .errors import AssistantError
from .storage import now_iso
from .llm import NoRedirect


SITE = "jobs.bytedance.com"
ENTRY = "https://jobs.bytedance.com/experienced/"
# Exact observed asset hosts; no wildcard domains and no telemetry/captcha hosts.
ASSET_HOSTS = {
    "lf-package-cn.feishucdn.com", "lf3-atsx-byte.feishucdn.com",
    "lf3-cdn-tos.bytescm.com", "lf3-static.bytednsdoc.com",
    "sf-atsx-tob.larksuite.com", "starling.zijieapi.com",
}
SEARCH_PATH = "/api/v1/search/job/posts"
CSRF_PATH = "/api/v1/csrf/token"
KEYWORDS = ("", "算法", "机器学习", "深度学习", "大模型", "人工智能", "知识图谱", "自然语言处理",
            "计算机视觉", "推荐", "搜索", "Python", "Java", "C++", "后端", "前端", "研发",
            "实习", "测试", "安全", "数据分析", "数据开发", "基础架构", "产品")
SEARCH_FIELDS = {"keyword", "limit", "offset", "portal_type", "portal_entrance",
                 "job_category_id_list", "tag_id_list", "location_code_list", "subject_id_list",
                 "recruitment_id_list", "job_function_id_list", "storefront_id_list"}
NAVIGATION = re.compile(r"/experienced(?:/|/position/?|/position/[0-9]+/detail/?)?")
READ_API = re.compile(r"/(?:api/v1/(?:common/setting|ip/location|website/agent/config|"
                      r"config/job/filters/[0-9]+|job/posts/[0-9]+)|people-suite/next-sdk/api/notice)/?")


def navigation_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != SITE or url.port not in (None, 443)
                or url.username or url.password or not NAVIGATION.fullmatch(url.path)):
            raise ValueError("outside read scope")
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True) if url.query else []
        if len(pairs) > 3 or len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate query")
        for key, item in pairs:
            if key == "keywords" and len(item) <= 80:
                continue
            if key == "page" and item.isdigit() and 1 <= int(item) <= 20:
                continue
            raise ValueError("unapproved query")
        return urlunsplit(("https", SITE, url.path.rstrip("/") or "/", urlencode(pairs), ""))
    except (ValueError, TypeError, AttributeError):
        raise AssistantError("链接不在已验证的公开招聘读取范围内，未访问。", 403) from None


def is_detail(url):
    return bool(re.fullmatch(r"/experienced/position/[0-9]+/detail", urlsplit(url).path))


def allowed_request(url, method, resource_type, post_data=None):
    """Validate semantics of the known read endpoint, not merely the HTTP method."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password):
            return False
        if resource_type == "document":
            navigation_url(url)
            return method == "GET"
        if parsed.hostname in ASSET_HOSTS:
            return method in {"GET", "HEAD"} and resource_type in {
                "script", "stylesheet", "image", "font", "fetch", "xhr"}
        if parsed.hostname != SITE:
            return False
        if method in {"GET", "HEAD"}:
            return (resource_type in {"script", "stylesheet", "image", "font"}
                    and not parsed.path.startswith("/api/")
                    and bool(re.search(r"\.(?:js|css|png|jpe?g|webp|svg|gif|ico|woff2?|ttf|otf)$", parsed.path))
                    or bool(READ_API.fullmatch(parsed.path)))
        if method != "POST" or resource_type not in {"xhr", "fetch"}:
            return False
        data = json.loads(post_data)
        if parsed.path == CSRF_PATH:
            # Anonymous read-session bootstrap, not account authentication or a
            # business write. Only the site's fixed public portal identifier.
            return (not parsed.query and isinstance(data, dict) and set(data) == {"portal_entrance"}
                    and type(data["portal_entrance"]) is int and data["portal_entrance"] == 1)
        if parsed.path != SEARCH_PATH:
            return False
        if not isinstance(data, dict) or set(data) - SEARCH_FIELDS:
            return False
        if (type(data.get("limit")) is not int or not 1 <= data["limit"] <= 20
                or type(data.get("offset")) is not int or not 0 <= data["offset"] <= 190
                or type(data.get("portal_type")) is not int or data["portal_type"] != 2
                or type(data.get("portal_entrance")) is not int or data["portal_entrance"] != 1
                or not isinstance(data.get("keyword"), str) or data["keyword"] not in KEYWORDS):
            return False
        for key in SEARCH_FIELDS - {"keyword", "limit", "offset", "portal_type", "portal_entrance"}:
            items = data.get(key, [])
            if (not isinstance(items, list) or len(items) > 20
                    or any(not isinstance(item, str) or len(item) > 100 for item in items)):
                return False
        # The site's own browser SDK mirrors pagination fields and signs this read
        # request. Preserve its signature; never generate one or bypass challenges.
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else []
        if len(pairs) > len(SEARCH_FIELDS) + 1 or len({key for key, _ in pairs}) != len(pairs):
            return False
        for key, value in pairs:
            if key == "_signature":
                if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", value):
                    return False
            else:
                if key not in SEARCH_FIELDS:
                    return False
                expected = data.get(key, [])
                expected = ",".join(expected) if isinstance(expected, list) else str(expected)
                if value != expected:
                    return False
        return True
    except (AssistantError, ValueError, TypeError, AttributeError):
        return False


def resolver_rules(hosts, mode="system"):
    """Pin approved hosts to public IPs for this fresh browser, avoiding DNS TOCTOU."""
    rules = []
    for host in sorted(hosts):
        if mode == "google":
            # Explicit opt-in, fixed DNS endpoint. No user-provided resolver/host.
            request = Request("https://dns.google/resolve?" + urlencode({
                              "name": host, "type": "A", "edns_client_subnet": "0.0.0.0/0"}),
                              headers={"Accept": "application/dns-json"})
            with build_opener(NoRedirect()).open(request, timeout=5) as response:
                raw = response.read(64001)
            if len(raw) > 64000:
                raise AssistantError("DNS 响应过大，已拒绝访问。", 502)
            answer = json.loads(raw)
            if answer.get("Status") != 0:
                raise AssistantError("公开 DNS 解析失败。", 502)
            ips = {item["data"] for item in answer.get("Answer", []) if item.get("type") == 1}
        else:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ips = {address[4][0] for address in addresses}
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise AssistantError("招聘域名解析到非公开地址，已拒绝。请检查代理 Fake-IP，或选择可选的公开 DNS 模式。", 403)
        # Chromium resolver rules support bracketed IPv6; prefer IPv4 where available.
        chosen = sorted(ips, key=lambda ip: (":" in ip, ip))[0]
        if ":" in chosen:
            chosen = "[" + chosen + "]"
        rules.append(f"MAP {host} {chosen}")
    return ", ".join([*rules, "MAP * ~NOTFOUND"])


class JobBrowser:
    def __init__(self, check, dns_mode="system"):
        self.check = check
        self.dns_mode = dns_mode
        self.playwright = self.browser = self.context = self.page = None
        self.blocked = 0
        self.read_errors = []
        self.response_errors = {}
        self.search_verified = False

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            rules = resolver_rules({SITE, *ASSET_HOSTS}, self.dns_mode)
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=True, channel="chromium",
                args=["--host-resolver-rules=" + rules, "--disable-quic",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--no-proxy-server"])
            self.context = self.browser.new_context(service_workers="block", accept_downloads=False,
                                                    locale="zh-CN", permissions=[])
            self.context.route("**/*", self._route)
            self.context.route_web_socket("**/*", lambda ws: ws.close())
            self.page = self.context.new_page()
            # Read bodies only after transfer completion. Reading response.json()
            # on the headers event can block the synchronous cancellation loop.
            self.page.on("requestfinished", self._finished)
            self.context.on("page", lambda page: page.close() if page != self.page else None)
            return self
        except AssistantError:
            self.__exit__(None, None, None)
            raise
        except Exception:
            self.__exit__(None, None, None)
            raise AssistantError("浏览器未能启动，请检查 Playwright、Chromium 安装及网络；未自动重试。", 502) from None

    def __exit__(self, *_):
        for resource in (self.context, self.browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass

    def _route(self, route):
        request = route.request
        if allowed_request(request.url, request.method, request.resource_type, request.post_data):
            route.continue_()
        else:
            self.blocked += 1
            if urlsplit(request.url).path == SEARCH_PATH:
                self.read_errors.append("岗位检索请求超出已验证范围，未报告为零岗位。")
            route.abort()

    def _response(self, response):
        parsed = urlsplit(response.url)
        if parsed.hostname != SITE:
            return
        path = parsed.path
        if path == SEARCH_PATH or re.fullmatch(r"/api/v1/job/posts/[0-9]+", path):
            if path == SEARCH_PATH:
                self.search_verified = False
            try:
                if response.status != 200:
                    self.response_errors[path] = f"岗位读取接口返回 HTTP {response.status}，不能视为没有岗位。"
                    return
                body = response.json()
                if body.get("code") != 0:
                    self.response_errors[path] = "岗位读取接口返回错误，不能视为没有岗位。"
                elif path == SEARCH_PATH:
                    if not isinstance(body.get("data", {}).get("job_post_list"), list):
                        self.response_errors[path] = "岗位检索响应结构未能验证，未报告为零岗位。"
                    else:
                        self.search_verified = True
                        self.response_errors.pop(path, None)
                else:
                    self.response_errors.pop(path, None)
            except Exception:
                self.response_errors[path] = "岗位读取接口响应未能验证。"

    def _finished(self, request):
        parsed = urlsplit(request.url)
        if (parsed.hostname == SITE and
                (parsed.path == SEARCH_PATH or re.fullmatch(r"/api/v1/job/posts/[0-9]+", parsed.path))):
            response = request.response()
            if response is not None:
                self._response(response)

    def read(self, url):
        from playwright.sync_api import Error as PlaywrightError

        url = navigation_url(url)
        self.check()
        self.read_errors = []
        self.response_errors = {}
        self.search_verified = False
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if response is None or response.status >= 400:
                raise AssistantError("招聘页面返回错误状态，未视为有效内容。", 502)
            # Wait for useful, stable content; avoid networkidle on a telemetry-heavy SPA.
            deadline = time.monotonic() + 20
            previous, stable = "", 0
            while time.monotonic() < deadline:
                self.check()
                text = self.page.locator("body").inner_text(timeout=2000)
                useful = ("职位要求" in text if is_detail(url) else
                          ("开启新的工作" in text and self.search_verified if "/position" in url else
                           self.page.locator('a[href*="/experienced/position"]').count() > 0))
                stable = stable + 1 if useful and text == previous else 0
                if stable >= 2:
                    break
                previous = text
                self.page.wait_for_timeout(300)
            else:
                errors = [*self.read_errors, *self.response_errors.values()]
                if errors:
                    raise AssistantError(errors[0], 502)
                raise AssistantError("页面正文或岗位列表未完整加载；可能受限，未报告为零岗位。", 502)
            errors = [*self.read_errors, *self.response_errors.values()]
            if errors:
                raise AssistantError(errors[0], 502)
            if "/position" in url and not is_detail(url) and not self.search_verified:
                raise AssistantError("岗位检索响应未验证，未报告为零岗位。", 502)
            final = navigation_url(self.page.url)
            links = self.page.locator("a[href]").evaluate_all(
                "els => els.map(e => ({text:e.innerText.slice(0,160),url:e.href}))")
            safe = {}
            for link in links:
                try:
                    target = navigation_url(link["url"])
                    safe.setdefault(target, {"url": target, "text": link["text"]})
                except AssistantError:
                    continue
            heading = self.page.locator("h1").first
            title = heading.inner_text(timeout=2000) if heading.count() else self.page.title()
            return {"url": final, "title": title[:300], "text": text[:16000],
                    "text_truncated": len(text) > 16000, "links": list(safe.values())[:80],
                    "fetched_at": now_iso(), "kind": "detail" if is_detail(final) else "listing",
                    "blocked_requests": self.blocked}
        except AssistantError:
            raise
        except (PlaywrightError, OSError, ValueError):
            raise AssistantError("浏览器读取失败或超时，未自动重试。", 502) from None

    def search_url(self, keyword, page):
        if (not isinstance(keyword, str) or keyword not in KEYWORDS or type(page) is not int
                or not 1 <= page <= 20):
            raise AssistantError("查询词不在本版安全职位词表中，或页码不合法。")
        return navigation_url("https://" + SITE + "/experienced/position?" +
                              urlencode({"keywords": keyword, "page": page}))
