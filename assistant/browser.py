"""Task-scoped public Chromium actions, with a few audited read API capabilities."""

import base64
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
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
HONOR_SITE = "career.honor.com"
# Audited from the official anonymous recruitment frontend. These are query
# capabilities, not a blanket /wecruit/ grant or a model-chosen action sequence.
HONOR_ASSET_HOSTS = {"as.alipayobjects.com", "at.alicdn.com", "wecruit-cdn.hotjob.cn"}
HONOR_READ_POST = re.compile(
    r"/wecruit/(?:suite/config/home/navigation|config/get|isLogin|"
    r"positionInfo/(?:listSearchTerm(?:/linkage)?|listPosition|listPositionDetail))"
    r"/SU[0-9a-f]{24}/?")


def honor_job_read(url):
    parsed = urlsplit(url)
    if parsed.hostname != HONOR_SITE:
        return None
    match = re.fullmatch(r"/wecruit/positionInfo/(listPosition|listPositionDetail)/SU[0-9a-f]{24}/?", parsed.path)
    return match.group(1) if match else None


def honor_page_read(url):
    parsed = urlsplit(url)
    if parsed.hostname != HONOR_SITE:
        return None
    if re.fullmatch(r"/SU[0-9a-f]{24}/(?:mc|pc)/position/(?:campus|social)/?", parsed.path):
        return "listPosition"
    if re.fullmatch(r"/SU[0-9a-f]{24}/(?:mc|pc)/detail/?", parsed.path):
        return "listPositionDetail"
    return None


READ_API = re.compile(r"/(?:api/v1/(?:common/setting|ip/location|website/agent/config|"
                      r"config/job/filters/[0-9]+|job/posts/[0-9]+)|people-suite/next-sdk/api/notice)/?")

# Fixed, read-only DOM inspection code. Models never supply selectors or JavaScript.
ELEMENT_SELECTOR = 'a[href],button,input,select,textarea,summary,[onclick],[role="button"],[role="tab"],[role="menuitem"],[role="link"],[role="checkbox"],[tabindex],[class*="pagination"] li'
# Read declared React click handlers without executing them or exposing props.
CLICKABLE_INFO = r"""e => !!e.onclick || Object.keys(e).some(k =>
    /^(?:__reactProps\$|__reactEventHandlers\$)/.test(k) &&
    e[k] && typeof e[k].onClick === 'function')"""
ELEMENT_INFO = """e => {
    const visible = !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const parent = e.closest('[role="search"], [class*="search" i]');
    const form = e.closest('form');
    return {connected:e.isConnected, visible, tag:e.tagName.toLowerCase(),
      role:e.getAttribute('role') || (e.tagName === 'A' ? 'link' : e.tagName === 'INPUT' ? 'textbox' : 'button'),
      label:(e.getAttribute('aria-label') || (e.labels && Array.from(e.labels).map(l=>l.innerText).join(' ')) || e.innerText || e.getAttribute('placeholder') || e.title || '').trim().slice(0,200),
      href:e.tagName === 'A' ? e.href : '', type:e.getAttribute('type') || '',
      name:e.getAttribute('name') || '',
      placeholder:e.getAttribute('placeholder') || '', disabled:!!e.disabled || e.getAttribute('aria-disabled') === 'true',
      search:!!parent, pagination:!!e.closest('[class*="pagination" i]'),
      filter:!!e.closest('[class*="filter" i],[aria-label*="筛选"],[aria-label*="filter" i]'),
      form:!!form, form_method:form ? form.method.toUpperCase() : '', form_action:form ? form.action : '',
      options:e.tagName==='SELECT' ? Array.from(e.options).slice(0,100).map(o=>({value:o.value,label:o.label,disabled:o.disabled})) : [],
      dialog:!!e.closest('[role="dialog"],dialog,[class*="modal"],[class*="Modal"]'),
      cookie:!!e.closest('[class*="cookie" i],[id*="cookie" i]')};
}"""


WRITE_LABEL = re.compile(r"投递|登录|登入|注册|上传|发送|提交|验证码|订阅|购买|删除|保存|支付|sign.?in|log.?in|apply now|submit|send|upload|save|delete|purchase|subscribe|checkout", re.I)
WRITE_PATH = re.compile(r"(?:^|[/_.-])(?:login|logout|signin|signup|register|apply|submit|upload|delete|remove|save|send|unsubscribe|checkout|purchase|payment|captcha|verify)(?:$|[/_.-])", re.I)


class BrowserError(AssistantError):
    def __init__(self, message, kind="interaction", retryable=True, status=502):
        super().__init__(message, status)
        self.kind, self.retryable = kind, retryable


def normalize_domain(value):
    try:
        if not isinstance(value, str) or value != value.strip() or len(value) > 253:
            raise ValueError()
        host = value.encode("idna").decode("ascii").lower()
        if ("." not in host or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-') for part in host.split('.'))
                or host.endswith((".local", ".localhost", ".internal", ".lan"))):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return host
        raise ValueError()
    except (ValueError, UnicodeError):
        raise AssistantError("授权域必须为完整的公开域名，不接受 IP、通配符、端口或 URL。", 400) from None


class BrowserScope:
    """Immutable task authority. Page content cannot add hosts or write capabilities."""
    def __init__(self, start_url=ENTRY, domains=None, resource_domains=None, read_post_urls=None):
        if not isinstance(start_url, str):
            raise AssistantError("起始 URL 必须为 HTTPS 文本地址。", 400)
        try:
            host = urlsplit(start_url).hostname
        except ValueError:
            raise AssistantError("起始 URL 格式无效。", 400) from None
        self.domains = frozenset(normalize_domain(item) for item in (domains or [host]))
        resources = set(self.domains)
        if SITE in self.domains:
            resources.update(ASSET_HOSTS)
        if HONOR_SITE in self.domains:
            resources.update(HONOR_ASSET_HOSTS)
        resources.update(normalize_domain(item) for item in (resource_domains or []))
        self.resource_domains = frozenset(resources)
        self.start_url = navigation_url(start_url, self)
        if read_post_urls is not None and (not isinstance(read_post_urls, list) or len(read_post_urls) > 20):
            raise AssistantError("只读 POST 接口必须为不超过20项的完整 HTTPS URL 列表。", 400)
        urls = set()
        for value in read_post_urls or []:
            value = navigation_url(value, self)
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment:
                raise AssistantError("只读 POST 授权填写精确接口地址，不带查询参数或片段。", 400)
            urls.add(value)
        self.read_post_urls = frozenset(urls)

    def as_dict(self):
        result = {"start_url": self.start_url, "domains": sorted(self.domains),
                  "resource_domains": sorted(self.resource_domains)}
        if self.read_post_urls:
            result["read_post_urls"] = sorted(self.read_post_urls)
        return result


def element_actions(info, url, scope=None):
    """Site-level read capabilities, NOT a fixed popup/search workflow."""
    if not info.get("connected") or not info.get("visible") or info.get("disabled"):
        return []
    label = info.get("label", "")
    if WRITE_LABEL.search(label):
        return []
    if info.get("href"):
        try:
            navigation_url(info["href"], scope)
            return ["click"]
        except AssistantError:
            return []
    search = info.get("search") or info.get("type") == "search" or bool(re.search(
        r"搜索|关键词|关键字|search|query|keywords", " ".join(info.get(k, "") for k in ("label", "placeholder", "name")), re.I))
    read_control = search or info.get("filter") or info.get("pagination")
    if info.get("form"):
        try:
            navigation_url(info.get("form_action"), scope)
        except AssistantError:
            return []
        method = info.get("form_method")
        action = urlsplit(info.get("form_action"))
        post_allowed = scope and urlunsplit((action.scheme, action.netloc, action.path, "", "")) in scope.read_post_urls
        if not search or method != "GET" and not (method == "POST" and post_allowed):
            return []
    if info.get("tag") in {"input", "textarea"}:
        if read_control and info.get("type") in {"", "text", "search"}:
            return ["fill", "press"]
        if info.get("filter") and info.get("type") in {"checkbox", "radio"}:
            return ["check"]
        return []
    if info.get("tag") == "select" and read_control:
        return ["select"]
    if read_control or info.get("role") == "tab":
        return ["click"]
    # Navigation/UI buttons are model-directed; the network layer still blocks
    # unapproved POST/writes. No positive popup-label whitelist is needed.
    if info.get("tag") == "button" or info.get("role") in {"button", "menuitem", "link"}:
        if re.search(r"同意|接受|accept|agree", label, re.I) and not info.get("cookie"):
            return []
        return ["click"]
    return []


def navigation_url(value, scope=None):
    try:
        url = urlsplit(value)
        hosts = scope.domains if scope else {SITE}
        if (not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value)
                or url.scheme != "https" or url.hostname not in hosts or url.port not in (None, 443)
                or url.username or url.password or WRITE_PATH.search(unquote(url.path)) or write_query(url.query)):
            raise ValueError("outside read scope")
        # A trailing slash and fragment affect relative URLs and SPA navigation.
        return urlunsplit(("https", url.hostname, url.path or "/", url.query, url.fragment))
    except (ValueError, TypeError, AttributeError):
        raise AssistantError("链接超出任务授权域或涉及潜在业务写操作，未访问；请确认访问范围。", 403) from None


def is_detail(url):
    return (bool(re.fullmatch(r"/experienced/position/[0-9]+/detail", urlsplit(url).path))
            or honor_page_read(url) == "listPositionDetail")


def write_query(query):
    return any(WRITE_PATH.search(key) or key.lower() in {"action", "operation", "op", "cmd"} and WRITE_PATH.search(value)
               for key, value in parse_qsl(query, keep_blank_values=True))


def read_post_body(body):
    if not isinstance(body, str) or len(body) > 65536:
        return False
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        try:
            return bool(parse_qsl(body, strict_parsing=True)) and not write_query(body)
        except ValueError:
            return False
    def writes(value):
        if isinstance(value, dict):
            return any(WRITE_PATH.search(key) or key.lower() == "mutation"
                       or key.lower() in {"operation", "action", "op", "cmd"} and isinstance(item, str) and WRITE_PATH.search(item)
                       or key.lower() == "query" and isinstance(item, str) and re.search(r"^\s*mutation\b", item, re.I)
                       or writes(item) for key, item in value.items())
        return isinstance(value, list) and any(writes(item) for item in value)
    try:
        return isinstance(data, dict) and not writes(data)
    except RecursionError:
        return False


def allowed_request(url, method, resource_type, post_data=None, scope=None):
    """Scoped public GETs, exact POST read grants and obvious-write guards."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password or write_query(parsed.query)):
            return False
        resources = scope.resource_domains if scope else {SITE, *ASSET_HOSTS}
        if (scope and parsed.hostname in resources and method in {"GET", "HEAD"}
                and resource_type in {"xhr", "fetch"} and not WRITE_PATH.search(unquote(parsed.path))):
            return True
        if resource_type == "document":
            navigation_url(url, scope)
            if method == "GET":
                return True
        if scope and parsed.hostname in scope.domains:
            navigation_url(url, scope)
            if (parsed.hostname == HONOR_SITE and method == "POST"
                    and resource_type in {"xhr", "fetch"}
                    and HONOR_READ_POST.fullmatch(parsed.path)):
                # Navigation/config/status queries legitimately have no body.
                return post_data in (None, "") or read_post_body(post_data)
            endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            if (method == "POST" and resource_type in {"xhr", "fetch", "document"}
                    and endpoint in scope.read_post_urls):
                # User-approved exact read endpoint, not a model-added wildcard.
                # Preserve site-generated fields instead of hardcoding its schema.
                return read_post_body(post_data)
        if (parsed.hostname in resources and method in {"GET", "HEAD"} and resource_type in {
                "script", "stylesheet", "image", "font"} and not WRITE_PATH.search(unquote(parsed.path))
                and not parsed.path.startswith("/api/")):
            return True
        if parsed.hostname != SITE or scope and SITE not in scope.domains:
            return False
        if method in {"GET", "HEAD"}:
            return (resource_type in {"script", "stylesheet", "image", "font"}
                    and not parsed.path.startswith("/api/")
                    and bool(re.search(r"\.(?:js|css|png|jpe?g|webp|svg|gif|ico|woff2?|ttf|otf)$", parsed.path))
                    or bool(READ_API.fullmatch(parsed.path)))
        if method != "POST" or resource_type not in {"xhr", "fetch"}:
            return False
        data = json.loads(post_data)
        if not read_post_body(post_data):
            return False
        if parsed.path == CSRF_PATH:
            return (not parsed.query and isinstance(data, dict) and set(data) == {"portal_entrance"}
                    and type(data["portal_entrance"]) is int and 1 <= data["portal_entrance"] <= 100)
        if parsed.path != SEARCH_PATH or not isinstance(data, dict):
            return False
        # This endpoint is an audited public query capability. Site-generated
        # portal/filter fields and signatures are preserved, not a fixed schema.
        # Keep resource bounds and reject explicit write-shaped payloads only.
        return (type(data.get("limit")) is int and 1 <= data["limit"] <= 100
                and type(data.get("offset")) is int and 0 <= data["offset"] <= 1_000_000
                and isinstance(data.get("keyword", ""), str) and len(data.get("keyword", "")) <= 1000)
    except (AssistantError, ValueError, TypeError, AttributeError, RecursionError):
        return False


def resolver_rules(hosts, mode="system", check=lambda: None):
    """Pin approved hosts to public IPs for this fresh browser, avoiding DNS TOCTOU."""
    rules = []
    for host in sorted(hosts):
        check()
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
            raise AssistantError("授权域名解析到非公开地址，已拒绝。请核对 DNS/代理解析；尚不能确定底层原因。", 403)
        # Chromium resolver rules support bracketed IPv6; prefer IPv4 where available.
        chosen = sorted(ips, key=lambda ip: (":" in ip, ip))[0]
        if ":" in chosen:
            chosen = "[" + chosen + "]"
        rules.append(f"MAP {host} {chosen}")
    return ", ".join([*rules, "MAP * ~NOTFOUND"])


class JobBrowser:
    def __init__(self, check, dns_mode="system", scope=None):
        self.check = check
        self.dns_mode = dns_mode
        self.scope = scope or BrowserScope()
        self.playwright = self.browser = self.context = self.page = None
        self.blocked = 0
        self.read_errors = []
        self.response_errors = {}
        self.search_verified = False
        self._elements = {}
        self._observation = 0
        self._element_url = None
        self.network_errors = []
        self.internal_errors = []
        self.policy_blocks = []
        self._document_ok = False
        self._search_seen = False
        self._honor_verified = set()
        self._honor_seen = set()

    def diagnostics(self):
        return {"network_errors": self.network_errors[-12:], "policy_blocks": self.policy_blocks[-12:],
                "internal_errors": self.internal_errors[-5:]}

    @staticmethod
    def target(url):
        parsed = urlsplit(url)
        return (parsed.hostname or "") + parsed.path[:300]

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            try:
                rules = resolver_rules(self.scope.resource_domains, self.dns_mode, self.check)
            except AssistantError as error:
                if error.status == 403:
                    raise
                raise BrowserError(str(error), "network") from None
            except (OSError, ValueError):
                raise BrowserError("公开域名解析或 DNS 连接失败；可重启浏览器或报告停止。", "network") from None
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
            self.page.on("requestfailed", self._network_failure)
            self.page.on("response", self._http_status)
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            # Playwright route handlers do not re-run for HTTP redirect hops.
            # Validate response Location before Chromium follows it, without
            # fetching through an unpinned APIRequestContext.
            self._cdp = self.context.new_cdp_session(self.page)
            self._cdp.on("Fetch.requestPaused", self._redirect_response)
            self._cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
            self.context.on("page", lambda page: page.close() if page != self.page else None)
            return self
        except AssistantError:
            self.__exit__(None, None, None)
            raise
        except Exception:
            self.__exit__(None, None, None)
            raise BrowserError("浏览器未能启动，请检查 Playwright/Chromium 安装；未自动重试。", "internal", False) from None

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
        if allowed_request(request.url, request.method, request.resource_type, request.post_data, self.scope):
            kind = honor_job_read(request.url)
            if kind:
                self._honor_seen.add(kind)
                self._honor_verified.discard(kind)
            route.continue_()
        else:
            self.blocked += 1
            self.policy_blocks.append({"target": self.target(request.url), "type": request.resource_type,
                                       "method": request.method,
                                       "reason": "未授权域/潜在写操作/POST读取接口尚未确认；可确认精确只读接口后重新发起"})
            self.policy_blocks = self.policy_blocks[-50:]
            if urlsplit(request.url).path == SEARCH_PATH:
                self.read_errors.append("岗位检索请求超出已验证范围，未报告为零岗位。")
            if honor_job_read(request.url):
                self.read_errors.append("必要岗位读取 POST 被权限策略阻断；不是零岗位或网络故障。")
            route.abort()

    def _redirect_response(self, event):
        from playwright.sync_api import Error as PlaywrightError

        key = event["requestId"]
        try:
            self._continue_response(event, key)
        except Exception as error:
            if isinstance(error, PlaywrightError) and "Invalid InterceptionId" in str(error):
                # Navigation can cancel a queued response event. Chromium says
                # this interception no longer exists; there is nothing to resume.
                return
            detail = str(error).splitlines()[0][:250]
            self.internal_errors.append("CDP 响应校验失败；可重启匿名浏览器恢复，不是网站网络故障。诊断：" + detail)
            try:
                self._cdp.send("Fetch.failRequest", {"requestId": key, "errorReason": "BlockedByClient"})
            except Exception:
                try:
                    self.page.close()
                except Exception:
                    pass

    def _continue_response(self, event, key):
        location = next((h["value"] for h in event.get("responseHeaders", [])
                         if h["name"].lower() == "location"), None)
        permitted = True
        status = event.get("responseStatusCode") or 0
        if location and 300 <= status < 400:
            target = urljoin(event["request"]["url"], location)
            kind = event["resourceType"].lower()
            method = event["request"].get("method", "GET")
            if status == 303 and method not in {"GET", "HEAD"} or status in {301, 302} and method == "POST":
                method = "GET"
            permitted = allowed_request(target, method, kind, event["request"].get("postData"), self.scope)
            if not permitted:
                self.policy_blocks.append({"target": self.target(target), "type": kind,
                                           "reason": "重定向超出授权或涉及业务写操作"})
                self.blocked += 1
        self._cdp.send("Fetch.continueRequest" if permitted else "Fetch.failRequest",
                       {"requestId": key, **({} if permitted else {"errorReason": "BlockedByClient"})})

    def _network_failure(self, request):
        if allowed_request(request.url, request.method, request.resource_type, request.post_data, self.scope):
            code = request.failure or "unknown"
            if code == "net::ERR_BLOCKED_BY_CLIENT":
                return
            self.network_errors.append({"target": self.target(request.url), "type": request.resource_type,
                                        "code": code[:100]})
            self.network_errors = self.network_errors[-50:]

    def _http_status(self, response):
        request = response.request
        if request.resource_type == "document" and request.frame == self.page.main_frame:
            self._document_ok = response.status < 400
        if (response.status >= 400 and request.resource_type in {"document", "script", "xhr", "fetch"}
                and allowed_request(request.url, request.method, request.resource_type, request.post_data, self.scope)):
            self.network_errors.append({"target": self.target(request.url), "type": request.resource_type,
                                        "code": "HTTP " + str(response.status)})

    def _response(self, response):
        parsed = urlsplit(response.url)
        kind = honor_job_read(response.url)
        if kind:
            self._honor_seen.add(kind)
            self._honor_verified.discard(kind)
            try:
                body = response.json() if response.status == 200 else None
                data = body.get("data") if isinstance(body, dict) else None
                valid = (isinstance(body, dict) and str(body.get("state")) == "200"
                         and isinstance(data, dict))
                if kind == "listPosition":
                    valid = (valid and isinstance(data.get("pageForm"), dict)
                             and isinstance(data["pageForm"].get("pageData"), list))
                else:
                    valid = valid and bool(data.get("postName"))
                if valid:
                    self._honor_verified.add(kind)
                    self.response_errors.pop(parsed.path, None)
                else:
                    self.response_errors[parsed.path] = f"荣耀岗位读取响应未验证（HTTP {response.status}）；不能视为零岗位。"
            except Exception:
                self.response_errors[parsed.path] = "荣耀岗位读取接口响应未能验证；不能视为零岗位。"
            return
        if parsed.hostname != SITE:
            return
        path = parsed.path
        if path == SEARCH_PATH or re.fullmatch(r"/api/v1/job/posts/[0-9]+", path):
            if path == SEARCH_PATH:
                self._search_seen = True
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
        if (honor_job_read(request.url) or parsed.hostname == SITE and
                (parsed.path == SEARCH_PATH or re.fullmatch(r"/api/v1/job/posts/[0-9]+", parsed.path))):
            response = request.response()
            if response is not None:
                self._response(response)

    def read(self, url):
        from playwright.sync_api import Error as PlaywrightError

        url = navigation_url(url, self.scope)
        self.check()
        self.read_errors = []
        self.response_errors = {}
        self.search_verified = False
        self._search_seen = False
        self._honor_verified.clear()
        self._honor_seen.clear()
        self.network_errors = []
        self.policy_blocks = []
        before = urlsplit(self.page.url)
        same_document = (before._replace(fragment="") == urlsplit(url)._replace(fragment="") and self._document_ok)
        self._document_ok = False
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if response is None and not same_document or response is not None and response.status >= 400:
                raise BrowserError("页面返回错误 HTTP 状态，未视为有效内容。", "page_loading")
            self._document_ok = True
            return self.observe(wait_seconds=5)
        except AssistantError:
            raise
        except (PlaywrightError, OSError, ValueError):
            if any(item["type"] == "document" for item in self.policy_blocks) and not self.network_errors:
                raise BrowserError("导航/重定向被任务权限阻断，不是网络不可达。", "permission", False, 403) from None
            if self.internal_errors:
                raise BrowserError(self.internal_errors[-1], "internal") from None
            raise BrowserError("页面导航失败或超时；模型可根据诊断选择重试或停止。", "network") from None

    def _clear_elements(self):
        for handle, _ in self._elements.values():
            try:
                handle.dispose()
            except Exception:
                pass
        self._elements = {}

    def observe(self, wait_seconds=2, text_start=0, text_length=65536):
        """Return useful incomplete UI too, so the model can handle an overlay."""
        from playwright.sync_api import Error as PlaywrightError

        self.check()
        if (type(text_start) is not int or not 0 <= text_start <= 4_000_000
                or type(text_length) is not int or not 1 <= text_length <= 65536):
            raise AssistantError("正文起始位置必须为0～4000000的整数。")
        try:
            if self.page.url == "about:blank":
                return {"url": self.scope.start_url, "title": "", "text": "", "links": [], "elements": [],
                        "kind": "page", "ready": False, "fetched_at": now_iso(), "text_truncated": False,
                        "read_error": "尚未成功导航，可打开授权 URL 或报告停止。", **self.diagnostics()}
            url = navigation_url(self.page.url, self.scope)
            deadline = time.monotonic() + wait_seconds
            previous, stable = "", 0
            text, useful = "", False
            while time.monotonic() < deadline:
                self.check()
                url = navigation_url(self.page.url, self.scope)
                text = self.page.locator("body").inner_text(timeout=2000)
                useful = bool(text.strip() and self._document_ok)
                if urlsplit(url).hostname == SITE and is_detail(url):
                    useful = useful and "职位要求" in text
                if self._search_seen:
                    useful = useful and self.search_verified
                if urlsplit(url).hostname == HONOR_SITE:
                    page_read = honor_page_read(url)
                    required = self._honor_seen | ({page_read} if page_read else set())
                    useful = useful and required <= self._honor_verified
                stable = stable + 1 if useful and text == previous else 0
                if stable >= 2:
                    break
                previous = text
                self.page.wait_for_timeout(300)
            errors = [*self.read_errors, *self.response_errors.values()]
            required = honor_page_read(url)
            if required and required not in self._honor_verified and not errors:
                errors.append("页面框架已打开，但必要岗位读取 POST 尚未验证；不能视为零岗位。")
            # A failed hydration/analytics script need not invalidate already
            # rendered article text. Keep failures visible; ready covers only
            # the current text snapshot, not interactive completeness.
            required_failures = [error for error in self.network_errors if error["type"] == "document"]
            script_warning = "部分脚本加载失败；当前正文可读取，不保证交互组件或全部内容完整。" if any(
                error["type"] == "script" for error in self.network_errors) else None
            ready = bool(useful and not errors and not required_failures and not self.internal_errors)
            if text_start > len(text):
                raise AssistantError("正文偏移超过页面长度，请调整 text_start。")
            window = text[text_start:text_start + text_length]
            ready = ready and bool(window.strip())
            final = navigation_url(self.page.url, self.scope)
            links = self.page.locator("a[href]").evaluate_all(
                "els => els.map(e => ({text:e.innerText.slice(0,160),url:e.href}))")
            safe = {}
            for link in links:
                try:
                    target = navigation_url(link["url"], self.scope)
                    safe.setdefault(target, {"url": target, "text": link["text"]})
                except AssistantError:
                    continue
            heading = self.page.locator("h1").first
            title = heading.inner_text(timeout=2000) if heading.count() else self.page.title()
            self._clear_elements()
            self._observation += 1
            self._element_url = final
            elements = []
            candidates = self.page.evaluate_handle("""selector => {
                const declared = """ + CLICKABLE_INFO + """;
                return Array.from(document.querySelectorAll('*')).filter(e =>
                    e.matches(selector) || declared(e));
            }""", ELEMENT_SELECTOR)
            try:
                handles = list(candidates.get_properties().values())
            finally:
                candidates.dispose()
            for handle in handles:
                if len(elements) >= 200:
                    handle.dispose()
                    continue
                try:
                    info = handle.evaluate(ELEMENT_INFO)
                    info["clickable"] = handle.evaluate(CLICKABLE_INFO)
                except PlaywrightError:
                    try:
                        handle.dispose()
                    except PlaywrightError:
                        pass
                    continue
                if not info["connected"] or not info["visible"]:
                    handle.dispose()
                    continue
                identity = f"o{self._observation}-e{len(elements) + 1}"
                actions = element_actions(info, final, self.scope)
                elements.append({"id": identity, "role": info["role"], "label": info["label"],
                                 "actions": actions, "disabled": info["disabled"],
                                 "href": info["href"] if actions and info["href"] else None,
                                 "options": info["options"],
                                 "context": "dialog" if info["dialog"] else "search" if info["search"] else
                                            "filter" if info["filter"] else "pagination" if info["pagination"] else "page",
                                 "restriction": "未知或可能写入的控件；需核实副作用/单独授权" if not actions else None})
                self._elements[identity] = (handle, info)
            return {"url": final, "title": title[:300], "text": window,
                    "text_start": text_start, "total_characters": len(text),
                    "text_truncated": text_start > 0 or len(text) > text_start + text_length,
                    "links": list(safe.values())[:200],
                    "fetched_at": now_iso(), "kind": "detail" if is_detail(final) else "page",
                    "blocked_requests": self.blocked, "elements": elements, "ready": ready,
                    "body_stable": stable >= 2, "resource_warning": script_warning, **self.diagnostics(),
                    "read_error": None if ready else (errors[0] if errors else
                        "正文/必要资源或读取接口尚未验证；可等待、刷新或换入口，不能判为零结果。")}
        except AssistantError:
            raise
        except (PlaywrightError, OSError, ValueError) as error:
            if self.internal_errors:
                raise BrowserError(self.internal_errors[-1], "internal") from None
            detail = str(error).splitlines()[0][:250]
            raise BrowserError("页面观察失败或超时；可重新观察/刷新。诊断：" + detail, "interaction") from None

    def _element(self, identity, action):
        self.check()
        if not isinstance(identity, str) or identity not in self._elements:
            raise AssistantError("元素编号过期或未观察到，请重新观察页面。", 409)
        handle, expected = self._elements[identity]
        try:
            actual = handle.evaluate(ELEMENT_INFO)
            actual["clickable"] = handle.evaluate(CLICKABLE_INFO)
            critical = ("connected", "tag", "role", "href", "type", "name", "form", "form_method", "form_action")
            if (not actual.get("connected") or any(actual.get(k) != expected.get(k) for k in critical)
                    or (expected.get("clickable") and not actual.get("clickable"))
                    or navigation_url(self.page.url, self.scope) != self._element_url):
                raise AssistantError("元素已变化，请重新观察页面，未执行动作。", 409)
            if action not in element_actions(actual, navigation_url(self.page.url, self.scope), self.scope):
                raise AssistantError("该控件不在已验证的只读操作范围内，未执行。", 403)
            return handle
        except AssistantError:
            raise
        except Exception:
            raise AssistantError("元素已失效，请重新观察页面，未执行动作。", 409) from None

    def hover(self, identity):
        self.check()
        if identity not in self._elements:
            raise AssistantError("元素编号过期，请重新观察。", 409)
        handle, _ = self._elements[identity]
        handle.hover(timeout=5000)
        return self.observe()

    def escape(self):
        self.check()
        self.page.keyboard.press("Escape")
        return self.observe()

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

        if not isinstance(text, str) or len(text) > 1000 or type(submit) is not bool:
            raise AssistantError("公开查询必须为不超过1000字的文本；不能填写私人表单。", 400)
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
        if (not isinstance(keyword, str) or len(keyword) > 1000 or type(page) is not int or page < 1):
            raise AssistantError("公开查询文本过长或页码无效。")
        return navigation_url("https://" + SITE + "/experienced/position?" +
                              urlencode({"keywords": keyword, "current": page, "limit": 10}), self.scope)

    def refresh(self):
        return self.read(self.page.url)

    def back(self):
        # Validate the history target before triggering any network request.
        history = self._cdp.send("Page.getNavigationHistory")
        index = history["currentIndex"] - 1
        if index < 0:
            raise AssistantError("没有可返回的页面。", 409)
        navigation_url(history["entries"][index]["url"], self.scope)
        self.check()
        self.network_errors, self.policy_blocks, self.read_errors, self.response_errors = [], [], [], {}
        self._search_seen, self.search_verified = False, False
        self._honor_verified.clear()
        self._honor_seen.clear()
        response = self.page.go_back(wait_until="domcontentloaded", timeout=20000)
        if response is None or response.status >= 400:
            raise BrowserError("返回页面失败，可观察当前页面或改用 URL。", "page_loading")
        return self.observe(wait_seconds=5)

    def wait(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 30:
            raise AssistantError("单次等待必须为1～30秒。")
        for _ in range(seconds * 5):
            self.check()
            self.page.wait_for_timeout(200)
        return self.observe()

    def press(self, identity, key):
        if key not in {"Enter", "Escape", "ArrowDown", "ArrowUp", "Tab"}:
            raise AssistantError("仅允许当前检索控件的导航/检索按键。", 403)
        element = self._element(identity, "press")
        element.press(key, timeout=5000)
        return self.observe(wait_seconds=5)

    def select(self, identity, value):
        element = self._element(identity, "select")
        options = self._elements[identity][1].get("options", [])
        if not any(item["value"] == value and not item["disabled"] for item in options):
            raise AssistantError("选择项不存在或被禁用，请重新观察。", 409)
        element.select_option(value=value, timeout=5000)
        return self.observe(wait_seconds=5)

    def check_field(self, identity, checked):
        if type(checked) is not bool:
            raise AssistantError("筛选勾选状态必须为布尔值。")
        self._element(identity, "check").set_checked(checked, timeout=5000)
        return self.observe(wait_seconds=5)

    def screenshot(self):
        navigation_url(self.page.url, self.scope)
        raw = self.page.screenshot(type="png", full_page=False, timeout=5000,
                                   mask=[self.page.locator('input,textarea,[contenteditable]')])
        if len(raw) > 2_000_000:
            raise AssistantError("截图过大，未发送模型。", 413)
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
