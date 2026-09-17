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
# The public search input emits these empty filters along with current/limit.
# Nonempty filters are not yet audited; this is not arbitrary query permission.
EMPTY_FILTERS = {"category", "location", "project", "type", "job_hot_flag", "functionCategory", "tag"}
NAVIGATION = re.compile(r"/experienced(?:/|/position/?|/position/[0-9]+/detail/?)?")
READ_API = re.compile(r"/(?:api/v1/(?:common/setting|ip/location|website/agent/config|"
                      r"config/job/filters/[0-9]+|job/posts/[0-9]+)|people-suite/next-sdk/api/notice)/?")

# Fixed, read-only DOM inspection code. Models never supply selectors or JavaScript.
ELEMENT_SELECTOR = 'a[href],button,input,[role="button"],[role="tab"],[class*="pagination"] li'
ELEMENT_INFO = """e => {
    const visible = !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const parent = e.closest('[class*="search"], [class*="Search"]');
    return {connected:e.isConnected, visible, tag:e.tagName.toLowerCase(),
      role:e.getAttribute('role') || (e.tagName === 'A' ? 'link' : e.tagName === 'INPUT' ? 'textbox' : 'button'),
      label:(e.getAttribute('aria-label') || e.innerText || e.getAttribute('placeholder') || e.title || '').trim().slice(0,200),
      href:e.tagName === 'A' ? e.href : '', type:e.getAttribute('type') || '',
      placeholder:e.getAttribute('placeholder') || '', disabled:!!e.disabled || e.getAttribute('aria-disabled') === 'true',
      search:!!parent, pagination:!!e.closest('[class*="pagination"], [class*="Pagination"]'),
      dialog:!!e.closest('[role="dialog"],dialog,[class*="modal"],[class*="Modal"]')};
}"""


def element_actions(info, url):
    """Site-level read capabilities, NOT a fixed popup/search workflow."""
    if not info.get("connected") or not info.get("visible") or info.get("disabled"):
        return []
    label = info.get("label", "")
    if re.search(r"投递|申请|登录|登入|注册|上传|发送|提交|验证码|立即体验|订阅|购买|确认", label):
        return []
    if info.get("href"):
        try:
            navigation_url(info["href"])
            return ["click"]
        except AssistantError:
            return []
    listing = urlsplit(url).path.rstrip("/") == "/experienced/position"
    if (listing and info.get("tag") == "input" and info.get("type") in {"", "text", "search"}
            and (info.get("search") or re.search(r"搜索.*职位|职位.*搜索|关键词", info.get("placeholder", "")))):
        return ["fill"]
    if info.get("tag") == "input":
        return []
    if listing and info.get("pagination"):
        current = int(dict(parse_qsl(urlsplit(url).query)).get("current", "1"))
        if ((label.isdigit() and 1 <= int(label) <= 20)
                or label == "上一页" and current > 1 or label == "下一页" and current < 20):
            return ["click"]
        return []
    if listing and info.get("search") and label in {"", "搜索", "查询", "搜索职位", "清除", "清空", "重置"}:
        return ["click"]
    if info.get("dialog") and re.search(r"关闭|取消|稍后|暂不|以后|不，谢谢", label):
        return ["click"]
    return []


def navigation_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != SITE or url.port not in (None, 443)
                or url.username or url.password or not NAVIGATION.fullmatch(url.path)):
            raise ValueError("outside read scope")
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True) if url.query else []
        if len(pairs) > len(EMPTY_FILTERS) + 4 or len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate query")
        if pairs and url.path.rstrip("/") != "/experienced/position":
            raise ValueError("query outside listing")
        for key, item in pairs:
            if key == "keywords" and item in KEYWORDS:
                continue
            if key in {"page", "current"} and item.isdigit() and 1 <= int(item) <= 20:
                continue
            if key == "limit" and item == "10" or key in EMPTY_FILTERS and item == "":
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
        self._elements = {}
        self._observation = 0
        self._element_url = None

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
        self._clear_elements()
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
            return self.observe(wait_seconds=20)
        except AssistantError:
            raise
        except (PlaywrightError, OSError, ValueError):
            raise AssistantError("浏览器读取失败或超时，未自动重试。", 502) from None

    def _clear_elements(self):
        for handle, _ in self._elements.values():
            try:
                handle.dispose()
            except Exception:
                pass
        self._elements = {}

    def observe(self, wait_seconds=2):
        """Return useful incomplete UI too, so the model can handle an overlay."""
        from playwright.sync_api import Error as PlaywrightError

        self.check()
        try:
            url = navigation_url(self.page.url)
            deadline = time.monotonic() + wait_seconds
            previous, stable = "", 0
            text, useful = "", False
            while time.monotonic() < deadline:
                self.check()
                url = navigation_url(self.page.url)
                text = self.page.locator("body").inner_text(timeout=2000)
                useful = ("职位要求" in text if is_detail(url) else
                          ("开启新的工作" in text and self.search_verified if "/position" in url else
                           self.page.locator('a[href*="/experienced/position"]').count() > 0))
                stable = stable + 1 if useful and text == previous else 0
                if stable >= 2:
                    break
                previous = text
                self.page.wait_for_timeout(300)
            errors = [*self.read_errors, *self.response_errors.values()]
            ready = bool(useful and stable >= 2 and not errors)
            if not text.strip():
                raise AssistantError(errors[0] if errors else "页面正文未加载；未报告为零岗位。", 502)
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
            self._clear_elements()
            self._observation += 1
            self._element_url = final
            elements = []
            for handle in self.page.query_selector_all(ELEMENT_SELECTOR):
                if len(elements) >= 80:
                    handle.dispose()
                    continue
                info = handle.evaluate(ELEMENT_INFO)
                if not info["connected"] or not info["visible"]:
                    handle.dispose()
                    continue
                identity = f"o{self._observation}-e{len(elements) + 1}"
                actions = element_actions(info, final)
                elements.append({"id": identity, "role": info["role"], "label": info["label"],
                                 "actions": actions, "disabled": info["disabled"],
                                 "href": info["href"] if actions and info["href"] else None,
                                 "context": "dialog" if info["dialog"] else "search" if info["search"] else
                                            "pagination" if info["pagination"] else "page"})
                self._elements[identity] = (handle, info)
            return {"url": final, "title": title[:300], "text": text[:16000],
                    "text_truncated": len(text) > 16000, "links": list(safe.values())[:80],
                    "fetched_at": now_iso(), "kind": "detail" if is_detail(final) else "listing",
                    "blocked_requests": self.blocked, "elements": elements, "ready": ready,
                    "read_error": None if ready else (errors[0] if errors else
                        "页面或岗位检索尚未完整验证；可以观察或操作已授权控件，不能判为零岗位。")}
        except AssistantError:
            raise
        except (PlaywrightError, OSError, ValueError):
            raise AssistantError("页面观察失败或超时，未自动重试。", 502) from None

    def _element(self, identity, action):
        self.check()
        if not isinstance(identity, str) or identity not in self._elements:
            raise AssistantError("元素编号过期或未观察到，请重新观察页面。", 409)
        handle, expected = self._elements[identity]
        try:
            actual = handle.evaluate(ELEMENT_INFO)
            if actual != expected or navigation_url(self.page.url) != self._element_url:
                raise AssistantError("元素已变化，请重新观察页面，未执行动作。", 409)
            if action not in element_actions(actual, navigation_url(self.page.url)):
                raise AssistantError("该控件不在已验证的只读操作范围内，未执行。", 403)
            return handle
        except AssistantError:
            raise
        except Exception:
            raise AssistantError("元素已失效，请重新观察页面，未执行动作。", 409) from None

    def click(self, identity):
        from playwright.sync_api import Error as PlaywrightError

        element = self._element(identity, "click")
        # Invalidate the previous search proof before a search/pagination action.
        info = self._elements[identity][1]
        if info["search"] or info["pagination"]:
            self.search_verified = False
            self.response_errors = {}
        try:
            element.click(timeout=5000)  # No force=True: do not click through overlays.
            return self.observe(wait_seconds=5)
        except PlaywrightError:
            raise AssistantError("元素点击失败或被遮挡；未强制点击，可重新观察页面。", 409) from None

    def fill(self, identity, text, submit):
        from playwright.sync_api import Error as PlaywrightError

        if not isinstance(text, str) or text not in KEYWORDS or type(submit) is not bool:
            raise AssistantError("仅允许填写安全职位关键词，不向网站发送画像或任意表单内容。", 403)
        element = self._element(identity, "fill")
        try:
            element.fill(text, timeout=5000)
            if submit:
                self.check()
                self.search_verified = False
                self.response_errors = {}
                element.press("Enter", timeout=5000)
            return self.observe(wait_seconds=5 if submit else 2)
        except PlaywrightError:
            raise AssistantError("搜索框填写失败或被遮挡；未强制操作。", 409) from None

    def scroll(self, direction):
        if not isinstance(direction, str) or direction not in {"up", "down"}:
            raise AssistantError("滚动方向无效。")
        self.check()
        self.page.mouse.wheel(0, 700 if direction == "down" else -700)
        return self.observe()

    def search_url(self, keyword, page):
        if (not isinstance(keyword, str) or keyword not in KEYWORDS or type(page) is not int
                or not 1 <= page <= 20):
            raise AssistantError("查询词不在本版安全职位词表中，或页码不合法。")
        return navigation_url("https://" + SITE + "/experienced/position?" +
                              urlencode({"keywords": keyword, "current": page, "limit": 10}))
