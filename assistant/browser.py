"""Task-scoped anonymous Chromium with a site-independent negative policy."""

import base64
import hashlib
import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from .errors import AssistantError
from .storage import now_iso
from .browser_network import PublicHTTPSProxy


SITE = "jobs.bytedance.com"
ENTRY = "https://jobs.bytedance.com/experienced/"
STATIC_RESOURCE_TYPES = frozenset({"script", "stylesheet", "image", "font", "media"})
# Fixed, read-only DOM inspection code. Models never supply selectors or JavaScript.
ELEMENT_SELECTOR = 'a[href],button,input,select,textarea,summary,[onclick],[role="button"],[role="tab"],[role="menuitem"],[role="link"],[role="checkbox"],[tabindex],[class*="pagination"] li'
# Read declared React click handlers without executing them or exposing props.
CLICKABLE_INFO = r"""e => !!e.onclick || Object.keys(e).some(k =>
    /^(?:__reactProps\$|__reactEventHandlers\$)/.test(k) &&
    e[k] && typeof e[k].onClick === 'function')"""
# Discovery is deliberately inclusive: event delegation need not leave an
# onclick property or a framework marker. Priority only orders the results;
# it never removes a visible node. Include open shadow roots as well.
ELEMENT_CANDIDATES = """selector => {
    const declared = """ + CLICKABLE_INFO + """;
    const preferred = [], remaining = [];
    function visit(root) {
        for (const e of root.querySelectorAll('*')) {
            const style = getComputedStyle(e);
            if (e.getClientRects().length && style.visibility !== 'hidden') {
                (e.matches(selector) || declared(e) || style.cursor === 'pointer'
                    ? preferred : remaining).push(e);
            }
            if (e.shadowRoot) visit(e.shadowRoot);
        }
    }
    visit(document);
    return preferred.concat(remaining);
}"""
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


WRITE_PATH = re.compile(r"(?:^|[/_.\s-])(?:login|logout|signin|signup|register|apply|deliver|submit|upload|delete|remove|save|send|create|update|insert|edit|collect|favorite|follow|unfollow|subscribe|unsubscribe|checkout|purchase|payment|captcha|verify)(?:$|[/_.\s-])", re.I)
CREDENTIAL_FIELD = re.compile(r"(?:^|[/_.\s-])(?:password|passwd|authorization|access_token|refresh_token|auth_code|credit_card)(?:$|[/_.\s-])", re.I)
# Whole operation segments are still ambiguous, but article slugs are not
# operations merely because they mention create/delete/edit.
NAV_ACTION_PATH = re.compile(r"(?:^|/)(?:login|logout|signin|signup|register|apply|deliver|submit|upload|delete|remove|save|send|collect|favorite|follow|unfollow|subscribe|unsubscribe|checkout|purchase|payment|captcha|verify)(?:/|$)", re.I)


def risky_name(value):
    # Check operation tokens, not substrings of the user's search text. Treat
    # camelCase writes like sendMail consistently with send_mail.
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", unquote(value))
    # Generic boolean/status queries do not authenticate an account.
    normalized = re.sub(r"(?:^|/)(?:is_login|login_status|login_state)(?=/|$)", "/status", normalized, flags=re.I)
    return bool(WRITE_PATH.search(normalized))


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
    """Immutable reading authority; host restrictions are optional, writes are not."""
    def __init__(self, start_url=ENTRY, domains=None, resource_domains=None, read_post_urls=None, blocked_urls=None, read_exceptions=None, public_web=None):
        if public_web is not None and not isinstance(public_web, bool):
            raise AssistantError("public_web 必须为布尔值，只控制公开阅读范围，不授予写入权限。", 400)
        # Missing in historical records: keep their exact-host authority and
        # serialized shape. New tasks explicitly opt into public-web reading.
        self.public_web = public_web is True
        self._public_web_explicit = public_web is not None
        if not isinstance(start_url, str):
            raise AssistantError("起始 URL 必须为 HTTPS 文本地址。", 400)
        try:
            host = urlsplit(start_url).hostname
        except ValueError:
            raise AssistantError("起始 URL 格式无效。", 400) from None
        self.domains = frozenset(normalize_domain(item) for item in (domains or [host]))
        resources = set(self.domains)
        resources.update(normalize_domain(item) for item in (resource_domains or []))
        self.resource_domains = frozenset(resources)
        # Check authority before risk heuristics: a confirmed exception must be
        # available when validating a start URL that was previously blocked.
        self.start_url = request_url(start_url, None if self.public_web else self.domains)
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
        # Legacy read_post_urls survive round-trip for old records, but no
        # longer grant permissions or override the negative policy.
        if blocked_urls is not None and (not isinstance(blocked_urls, list) or len(blocked_urls) > 20):
            raise AssistantError("禁止访问 URL 必须为不超过20项的精确 HTTPS 地址列表。", 400)
        blocked = set()
        for value in blocked_urls or []:
            try:
                parsed = urlsplit(value)
                if (not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value)
                        or parsed.scheme != "https" or not parsed.hostname
                        or (not self.public_web and parsed.hostname not in self.resource_domains)
                        or parsed.port not in (None, 443) or parsed.username or parsed.password
                        or parsed.query or parsed.fragment):
                    raise ValueError()
                normalize_domain(parsed.hostname)
                blocked.add(urlunsplit(("https", parsed.hostname, parsed.path or "/", "", "")))
            except (ValueError, TypeError, AttributeError):
                raise AssistantError("禁止访问 URL 必须在任务域内，不含查询参数、片段或凭据；按整个接口路径阻断。", 400) from None
        self.blocked_urls = frozenset(blocked)
        self._blocked_endpoints = frozenset((urlsplit(value).hostname, unquote(urlsplit(value).path))
                                           for value in blocked)
        if read_exceptions is not None and (not isinstance(read_exceptions, list) or len(read_exceptions) > 20):
            raise AssistantError("阅读豁免必须为不超过20项的请求列表。", 400)
        self.read_exceptions = []
        for entry in read_exceptions or []:
            if not isinstance(entry, dict):
                raise AssistantError("阅读豁免必须填写请求对象。", 400)
            method, kind = entry.get("method"), entry.get("resource_type")
            keys = {"url", "method", "resource_type"} | ({"body_sha256", "content_type"} if method == "POST" else set())
            if (set(entry) != keys or not isinstance(method, str) or not isinstance(kind, str)
                    or method not in {"GET", "HEAD", "POST"}
                    or kind not in {"document", "xhr", "fetch"}):
                raise AssistantError("阅读豁免只接受精确 GET/HEAD/POST 文档或数据请求。", 400)
            hosts = self.domains if method == "POST" or kind == "document" else self.resource_domains
            url = request_url(entry["url"], None if self.public_web else hosts)
            # Mirror the per-surface predicate used when the binding is granted.
            query_risk = (operation_query if method == "POST" or kind in {"xhr", "fetch"}
                          else write_query)(urlsplit(url).query)
            if urlsplit(url).fragment or query_risk or blocked_url(url, self):
                raise AssistantError("阅读豁免不能覆盖查询操作、凭据或用户显式禁止路径，也不能带片段。", 400)
            normalized = {"url": url, "method": method, "resource_type": kind}
            if method == "POST":
                digest, content_type = entry["body_sha256"], entry["content_type"]
                if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or not isinstance(content_type, str) or len(content_type) > 200
                        or any(ord(c) < 32 for c in content_type)):
                    raise AssistantError("POST豁免必须包含正文 SHA256 与 Content-Type。", 400)
                normalized.update(body_sha256=digest, content_type=content_type.lower().strip())
            self.read_exceptions.append(normalized)
        navigation_url(self.start_url, self)

    def as_dict(self):
        result = {"start_url": self.start_url, "domains": sorted(self.domains),
                  "resource_domains": sorted(self.resource_domains)}
        if self._public_web_explicit:
            result["public_web"] = self.public_web
        if self.read_post_urls:
            result["read_post_urls"] = sorted(self.read_post_urls)
        if self.blocked_urls:
            result["blocked_urls"] = sorted(self.blocked_urls)
        if self.read_exceptions:
            result["read_exceptions"] = [dict(entry) for entry in self.read_exceptions]
        return result


def element_actions(info, url, scope=None):
    """All visible enabled nodes may be clicked; filling remains a separate capability."""
    if not info.get("connected") or not info.get("visible") or info.get("disabled"):
        return []
    actions = ["click"]
    search = info.get("search") or info.get("type") == "search" or bool(re.search(
        r"搜索|关键词|关键字|search|query|keywords", " ".join(info.get(k, "") for k in ("label", "placeholder", "name")), re.I))
    read_control = search or info.get("filter") or info.get("pagination")
    if info.get("form") and search:
        try:
            navigation_url(info.get("form_action"), scope)
        except AssistantError:
            return actions
        method = info.get("form_method")
        post_allowed = allowed_request(info.get("form_action"), "POST", "document", "", scope)
        if not search or method != "GET" and not (method == "POST" and post_allowed):
            return actions
    if info.get("tag") in {"input", "textarea"}:
        if read_control and info.get("type") in {"", "text", "search"}:
            return actions + ["fill", "press"]
        if info.get("filter") and info.get("type") in {"checkbox", "radio"}:
            return actions + ["check"]
        return actions
    if info.get("tag") == "select" and read_control:
        return actions + ["select"]
    return actions


def request_url(value, hosts):
    """HTTPS/host shape; None means public-web scope, not unchecked networking."""
    try:
        if not isinstance(value, str):
            raise ValueError()
        url = urlsplit(value)
        if (len(value) > 2000 or any(ord(c) < 32 for c in value)
                or url.scheme != "https" or not url.hostname
                or (hosts is not None and url.hostname not in hosts) or url.port not in (None, 443)
                or url.username or url.password):
            raise ValueError("outside read scope")
        normalize_domain(url.hostname)
        # A trailing slash and fragment affect relative URLs and SPA navigation.
        return urlunsplit(("https", url.hostname, url.path or "/", url.query, url.fragment))
    except (AssistantError, ValueError, TypeError, AttributeError):
        reason = "链接必须为不含凭据的公开域名 HTTPS 地址（443端口）；不接受本机/内网域名或IP地址。"
        if hosts is not None:
            reason += "限定网站模式下还须在任务域内。"
        raise AssistantError(reason, 403) from None


def navigation_url(value, scope=None):
    denial = request_denial(value, "GET", "document", scope=scope)
    if denial:
        error = AssistantError(denial["reason"], 403)
        error.policy = denial
        raise error
    return request_url(value, None if scope and scope.public_web else scope.domains if scope else {SITE})


def is_detail(url):
    # An optional presentation hint, not a navigation/permission constraint.
    return bool(re.search(r"(?:^|/)(?:detail|details)(?:/|$)", urlsplit(url).path, re.I))


def blocked_url(value, scope):
    parsed = urlsplit(value)
    endpoint = (parsed.hostname, unquote(parsed.path or "/"))
    return endpoint in getattr(scope, "_blocked_endpoints", ())


def graphql_mutation(value):
    return bool(re.search(r"(?:^|\n)\s*mutation\b[\s\S]*\{", value, re.I))


def operation_query(query):
    """Credentials, RPC selectors and GraphQL mutations; plain keys are data."""
    return any(CREDENTIAL_FIELD.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key))
               or key.lower() in {"action", "operation", "op", "cmd"} and risky_name(value)
               or key.lower() in {"query", "mutation"} and graphql_mutation(value)
               for key, value in parse_qsl(query, keep_blank_values=True))


def write_query(query):
    return (any(risky_name(key) for key, _ in parse_qsl(query, keep_blank_values=True))
            or operation_query(query))


def terminal_verb_path(path):
    """Only the last path segment can be the operation; earlier ones are namespaces."""
    terminal = unquote(path).rstrip("/").rsplit("/", 1)[-1]
    return risky_name("/" + terminal)


def read_post_body(body, content_type=""):
    if body in (None, ""):
        return True
    if not isinstance(body, str) or len(body.encode("utf-8")) > 65536:
        return False
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        try:
            return bool(parse_qsl(body, strict_parsing=True)) and not operation_query(body)
        except ValueError:
            return (any(kind in content_type.lower() for kind in ("text/plain", "application/graphql"))
                    and not graphql_mutation(body)
                    and not re.search(r"Content-Disposition:|filename\s*=", body, re.I))
    def writes(value):
        # Key names are the site's schema, not operations: createTime/updateTime/
        # applyId are ordinary read fields. Values of operation selectors and
        # credential/mutation keys are the actual risk.
        if isinstance(value, dict):
            return any(CREDENTIAL_FIELD.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key))
                       or key.lower() == "mutation"
                       or key.lower() in {"operation", "action", "op", "cmd"} and isinstance(item, str) and risky_name(item)
                       or key.lower() == "query" and isinstance(item, str) and graphql_mutation(item)
                       or writes(item) for key, item in value.items())
        return isinstance(value, list) and any(writes(item) for item in value)
    try:
        return isinstance(data, (dict, list)) and not writes(data)
    except RecursionError:
        return False


def request_denial(url, method, resource_type, post_data=None, scope=None, content_type=""):
    """One policy for navigation, requests and redirects; only path risk is overridable."""
    def denied(rule, reason, overridable=False):
        result = {"rule": rule, "reason": reason, "overridable": overridable}
        if overridable:
            result["read_exception"] = request_binding(url, method, resource_type, post_data, content_type)
        return result
    try:
        parsed = urlsplit(url)
        domains = scope.domains if scope else {SITE}
        resources = scope.resource_domains if scope else {SITE}
        hosts = domains if resource_type == "document" or method == "POST" else resources
        if scope and scope.public_web:
            hosts = None
        if method in {"GET", "HEAD"} and resource_type in STATIC_RESOURCE_TYPES:
            # Browser-reported subresources, not a URL-extension or CDN whitelist.
            # The transport separately validates and pins every host's public IPs.
            hosts = {normalize_domain(parsed.hostname)}
        url = request_url(url, hosts)
        if blocked_url(url, scope):
            return denied("explicit_deny", "命中用户显式禁止路径；需用户移除禁令，阅读豁免无效。")
        # Who generates the request decides how much of the URL is policy surface.
        # Documents are model-chosen; subresources and site-generated data
        # requests are not something the model can craft.
        if method in {"GET", "HEAD"} and resource_type in {"document"} | STATIC_RESOURCE_TYPES:
            query_risk = write_query(parsed.query)
        elif method == "POST" or resource_type in {"xhr", "fetch"}:
            query_risk = operation_query(parsed.query)
        else:
            query_risk = False
        if query_risk:
            return denied("query_operation", "查询参数包含账号/操作或凭据风险；阅读豁免不能覆盖。")
        if method not in {"GET", "HEAD", "POST"}:
            return denied("method", "请求方法不属于公开读取能力。")
        if resource_type not in {"document", "xhr", "fetch"} | STATIC_RESOURCE_TYPES:
            return denied("resource_type", "资源类型不属于公开读取能力。")
        if method == "POST" and (resource_type not in {"document", "xhr", "fetch"}
                or any(kind in content_type.lower() for kind in ("multipart/", "application/octet-stream"))
                or not read_post_body(post_data, content_type)):
            return denied("body", "POST正文含写入/凭据/mutation、文件类型、无效格式或超出大小限制；不可豁免。")
        path_risk = False
        if method in {"GET", "HEAD"} and resource_type == "document":
            path_risk = bool(NAV_ACTION_PATH.search(unquote(parsed.path)))
        elif method == "POST" and resource_type in {"document", "xhr", "fetch"}:
            # /api/apply/jobs is a namespace; only /jobs/apply is an operation.
            path_risk = terminal_verb_path(parsed.path)
        if path_risk:
            binding = request_binding(url, method, resource_type, post_data, content_type)
            if binding not in getattr(scope, "read_exceptions", ()):
                return denied("path_keyword", "路径疑似账号/业务操作（仅启发式，可能误伤）；可核实后添加本任务精确阅读豁免并重新发起。", True)
        return None
    except (AssistantError, ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
        reason = "链接格式或主机不符合公开 HTTPS 阅读要求。"
        if not (scope and scope.public_web):
            reason += "当前为限定网站模式，目标也须在任务域内；新建任务可选择公网阅读。"
        return denied("authority", reason)


def request_binding(url, method, resource_type, post_data=None, content_type=""):
    parsed = urlsplit(url)
    result = {"url": urlunsplit(("https", parsed.hostname, parsed.path or "/", parsed.query, "")),
              "method": method, "resource_type": resource_type}
    if method == "POST":
        result.update(body_sha256=hashlib.sha256((post_data or "").encode("utf-8")).hexdigest(),
                      content_type=content_type.lower().strip())
    return result


def allowed_request(url, method, resource_type, post_data=None, scope=None, content_type=""):
    return request_denial(url, method, resource_type, post_data, scope, content_type) is None


def public_addresses(host, mode="system"):
    """System DNS only. Legacy mode arguments never invoke external DoH."""
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = {address[4][0] for address in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AssistantError("域名解析到非公开地址，已拒绝。请核对 DNS/代理解析；尚不能确定底层原因。", 403)
    return sorted(ips, key=lambda ip: (":" in ip, ip))


def resolver_rules(hosts, mode="system", check=lambda: None):
    """Legacy fixed-host helper; runtime uses dynamic public-IP HTTPS tunnels."""
    rules = []
    for host in sorted(hosts):
        check()
        ips = public_addresses(host, mode)
        # Chromium resolver rules support bracketed IPv6; prefer IPv4 where available.
        chosen = sorted(ips, key=lambda ip: (":" in ip, ip))[0]
        if ":" in chosen:
            chosen = "[" + chosen + "]"
        rules.append(f"MAP {host} {chosen}")
    return ", ".join([*rules, "MAP * ~NOTFOUND"])


class JobBrowser:
    # Only transport is shared. Background tabs must not overwrite the current
    # tab's readiness, pending requests or failure diagnosis.
    TAB_STATE_FIELDS = ("read_errors", "data_errors", "_pending_data", "data_responses", "network_errors",
                        "internal_errors", "policy_blocks", "_document_ok", "_proxy_auth_failed")

    def __init__(self, check, dns_mode="system", scope=None):
        self.check = check
        self.dns_mode = "system"
        self.scope = scope or BrowserScope()
        self.playwright = self.browser = self.context = self.page = None
        self.blocked = 0
        self.read_errors = []
        self.data_errors = {}
        self._pending_data = {}
        self.data_responses = []
        self._elements = {}
        self._observation = 0
        self._element_url = None
        self.network_errors = []
        self.internal_errors = []
        self.policy_blocks = []
        self._document_ok = False
        self._transport = None
        self._proxy_auth_failed = False
        self._tabs, self._page_ids, self._tab_states, self._tab_cdps = {}, {}, {}, {}
        self._tab_titles = {}
        self._tab_sequence = 0
        self._observed_text = ""

    def _save_tab_state(self):
        if self.page is not None:
            self._tab_states[self.page] = {key: getattr(self, key) for key in self.TAB_STATE_FIELDS}

    def _activate_page(self, page):
        self._save_tab_state()
        self.page = page
        self._cdp = self._tab_cdps.get(page)
        state = self._tab_states.get(page, {})
        for key in self.TAB_STATE_FIELDS:
            default = False if key in {"_document_ok", "_proxy_auth_failed"} else {} if key in {"data_errors", "_pending_data"} else []
            setattr(self, key, state.get(key, default))

    def _page_event(self, page, callback, *args):
        # Playwright's synchronous callbacks share one thread. Restore the
        # selected tab even when a background callback raises.
        selected = self.page
        if page is not selected:
            self._activate_page(page)
        try:
            return callback(*args)
        finally:
            self._save_tab_state()
            if page is not selected:
                self._activate_page(selected)

    def _attach_page(self, page):
        if page in self._page_ids or page.is_closed() is True:
            return
        self._tab_sequence += 1
        key = f"t{self._tab_sequence}"
        self._page_ids[page], self._tabs[key] = key, page
        self._tab_states[page] = {}
        for event, callback in (("request", self._request_started), ("requestfinished", self._finished),
                                ("requestfailed", self._network_failure), ("response", self._http_status)):
            page.on(event, lambda value, callback=callback: self._page_event(page, callback, value))
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("close", lambda: self._tabs.pop(key, None))
        cdp = self.context.new_cdp_session(page)
        self._tab_cdps[page] = cdp
        cdp.on("Fetch.requestPaused", lambda event: self._page_event(page, self._redirect_response, event))
        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})

    def list_tabs(self):
        self.check()
        tabs = []
        for key, page in list(self._tabs.items()):
            if page.is_closed():
                continue
            # Never wait for a background page's execution context just to
            # enumerate tabs. Its title is filled when that tab is observed.
            title = self._tab_titles.get(page, "")
            tabs.append({"tab_id": key, "url": page.url, "title": title, "active": page is self.page})
        return {"tabs": tabs, "active_tab": self._page_ids.get(self.page)}

    def _ensure_page(self):
        if self.page is None or self.page.is_closed() is True:
            page = next((page for page in self._tabs.values() if not page.is_closed()), None)
            if page is None:
                page = self.context.new_page()
                self._attach_page(page)
            self._activate_page(page)
            self._clear_elements()

    def switch_tab(self, tab_id):
        self.check()
        page = self._tabs.get(tab_id)
        if page is None or page.is_closed():
            raise BrowserError("标签页不存在或已关闭，请先列出标签页。", "interaction", False, 409)
        self._activate_page(page)
        self._clear_elements()
        return self.observe(wait_seconds=5)

    def close_tab(self, tab_id):
        self.check()
        page = self._tabs.get(tab_id)
        if page is None or page.is_closed():
            raise BrowserError("标签页不存在或已关闭。", "interaction", False, 409)
        selected = page is self.page
        page.close()
        self._tabs.pop(tab_id, None)
        if selected:
            self._activate_page(next(iter(self._tabs.values()), None))
            self._clear_elements()
        return self.list_tabs()

    def diagnostics(self):
        connection_errors = self._transport.failures() if self._transport else []
        return {"network_errors": self.network_errors[-12:], "policy_blocks": self.policy_blocks[-12:],
                "dns_mode": self.dns_mode,
                "connection_errors": connection_errors,
                "internal_errors": self.internal_errors[-5:],
                "pending_data_requests": list(self._pending_data.values())[-12:],
                "data_responses": self.data_responses[-12:], "data_errors": list(self.data_errors.values())[-12:]}

    def permitted(self, request):
        try:
            content_type = request.headers.get("content-type", "")
            if any(kind in content_type.lower() for kind in ("multipart/", "application/octet-stream")):
                return False
            return allowed_request(request.url, request.method, request.resource_type, request.post_data,
                                   self.scope, content_type)
        except Exception:
            # Binary/undecodable bodies are not public query text. Fail closed
            # here instead of breaking Playwright's request callback.
            return False

    def _request_started(self, request):
        if request.resource_type in {"xhr", "fetch"} and self.permitted(request):
            target = self.target(request.url)
            self._pending_data[id(request)] = {"target": target, "method": request.method}
            self.data_errors.pop(target, None)

    @staticmethod
    def target(url):
        parsed = urlsplit(url)
        return (parsed.hostname or "") + parsed.path[:300]

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            self.check()
            self._transport = PublicHTTPSProxy(lambda host: public_addresses(host, self.dns_mode), normalize_domain)
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=True, channel="chromium",
                proxy=self._transport.settings,
                # Playwright already disables background networking/component
                # updates. The transport also rejects unregistered connections.
                args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            self.context = self.browser.new_context(service_workers="block", accept_downloads=False,
                                                    locale="zh-CN", permissions=[])
            self.context.route("**/*", self._route)
            self.context.route_web_socket("**/*", lambda ws: ws.close())
            self.page = self.context.new_page()
            self._attach_page(self.page)
            self._cdp = self._tab_cdps[self.page]
            self.context.on("page", self._attach_page)
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
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _route(self, route):
        from playwright.sync_api import Error as PlaywrightError
        request = route.request
        # Wire popup diagnostics and redirect checks before continuing its
        # first navigation; context routing covers initial popup requests too.
        try:
            page = request.frame.page
        except PlaywrightError:
            # Some initial navigations precede the frame object. Still apply
            # the same request policy; page events attach the new tab as soon
            # as Playwright exposes it, without closing or stalling it.
            for page in self.context.pages:
                self._attach_page(page)
            return self._route_request(route)
        self._attach_page(page)
        return self._page_event(page, self._route_request, route)

    def _route_request(self, route):
        request = route.request
        if self.permitted(request):
            if self._transport:
                self._transport.allow_host(urlsplit(request.url).hostname)
            route.continue_()
        else:
            self.blocked += 1
            try:
                denial = request_denial(request.url, request.method, request.resource_type, request.post_data,
                                         self.scope, request.headers.get("content-type", ""))
            except Exception:
                denial = None
            self.policy_blocks.append({"target": self.target(request.url), "type": request.resource_type,
                                       "method": request.method,
                                       **(denial or {"rule": "body", "overridable": False,
                                                     "reason": "文件类型或无法解码的正文不可放行。"})})
            self.policy_blocks = self.policy_blocks[-50:]
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
            content_type = next((v for k, v in event["request"].get("headers", {}).items()
                                 if k.lower() == "content-type"), "")
            denial = request_denial(target, method, kind, event["request"].get("postData"), self.scope, content_type)
            permitted = denial is None
            if permitted and self._transport:
                # Redirect hops bypass Playwright's route handler.
                self._transport.allow_host(urlsplit(target).hostname)
            if not permitted:
                self.policy_blocks.append({"target": self.target(target), "type": kind,
                                           "method": method, **denial,
                                           "reason": "重定向：" + denial["reason"]})
                self.blocked += 1
        self._cdp.send("Fetch.continueRequest" if permitted else "Fetch.failRequest",
                       {"requestId": key, **({} if permitted else {"errorReason": "BlockedByClient"})})

    def _network_failure(self, request):
        pending = self._pending_data.pop(id(request), None)
        if self.permitted(request):
            code = request.failure or "unknown"
            if code == "net::ERR_BLOCKED_BY_CLIENT":
                return
            if code in {"net::ERR_INVALID_AUTH_CREDENTIALS", "net::ERR_PROXY_AUTH_UNSUPPORTED",
                        "net::ERR_PROXY_AUTH_REQUESTED"} and self._transport:
                self._proxy_auth_failed = True
                self.internal_errors.append("浏览器连接认证异常：" + code +
                    "；内部隧道不要求密码，请检查运行版本/代理配置；不是普通网络超时，勿原样循环重试。")
                return
            if self._transport and any(item["kind"] == "permission" and
                    item["target"] == urlsplit(request.url).hostname for item in self._transport.failures()):
                return  # Public-IP enforcement is a policy denial, not a network outage.
            self.network_errors.append({"target": self.target(request.url), "type": request.resource_type,
                                        "code": code[:100]})
            self.network_errors = self.network_errors[-50:]
            if pending:
                self.data_errors[pending["target"]] = {**pending, "code": code[:100]}

    def _http_status(self, response):
        request = response.request
        if request.resource_type == "document" and request.frame == self.page.main_frame:
            self._document_ok = response.status < 400
        if response.status == 407 and self._transport:
            self._proxy_auth_failed = True
            self.internal_errors.append("代理认证异常（HTTP 407）；当前内部隧道不要求密码，" +
                "请检查运行版本/代理配置，不要将它当作网站登录要求或普通网络超时。")
            return
        if (response.status >= 400 and request.resource_type in {"document", "script", "xhr", "fetch"}
                and self.permitted(request)):
            self.network_errors.append({"target": self.target(request.url), "type": request.resource_type,
                                        "code": "HTTP " + str(response.status)})

    def _response(self, response):
        pending = self._pending_data.pop(id(response.request), None)
        if not pending:
            return  # Old navigation or out-of-scope response cannot validate this page.
        record = {**pending, "status": response.status}
        self.data_responses.append(record)
        self.data_responses = self.data_responses[-50:]
        if response.status >= 400:
            self.data_errors[pending["target"]] = {**record, "code": "HTTP " + str(response.status)}
        else:
            self.data_errors.pop(pending["target"], None)

    def _finished(self, request):
        if id(request) in self._pending_data:
            response = request.response()
            if response is not None:
                self._response(response)

    def read(self, url):
        from playwright.sync_api import Error as PlaywrightError

        url = navigation_url(url, self.scope)
        self.check()
        self._ensure_page()
        self.read_errors = []
        self.data_errors.clear()
        self._pending_data.clear()
        self.data_responses.clear()
        self.network_errors = []
        self.policy_blocks = []
        if self._transport:
            self._transport.clear_failures()
        before = urlsplit(self.page.url)
        same_document = (before._replace(fragment="") == urlsplit(url)._replace(fragment="") and self._document_ok)
        self._document_ok = False
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if response is not None and response.status == 407 and self._transport:
                raise BrowserError("代理认证异常（HTTP 407）；当前内部隧道不要求密码，" +
                    "请检查运行版本/代理配置；不是普通网络超时。", "internal", False)
            if response is None and not same_document or response is not None and response.status >= 400:
                raise BrowserError("页面返回错误 HTTP 状态，未视为有效内容。", "page_loading")
            self._document_ok = True
            return self.observe(wait_seconds=5)
        except AssistantError:
            raise
        except (PlaywrightError, OSError, ValueError) as error:
            if self._transport and any(item["kind"] == "permission" and
                    item["target"] == urlsplit(url).hostname for item in self._transport.failures()):
                raise BrowserError("域名的公网地址验证被拒绝；不是网站网络故障，详情见连接诊断。", "permission", False, 403) from None
            if any(item["type"] == "document" for item in self.policy_blocks) and not self.network_errors:
                raise BrowserError("导航/重定向被任务权限阻断，不是网络不可达。", "permission", False, 403) from None
            if self.internal_errors:
                raise BrowserError(self.internal_errors[-1], "internal", not self._proxy_auth_failed) from None
            # Preserve the actual browser error without copying the raw URL,
            # query tokens, or credentials from Playwright's exception text.
            code = re.search(r"net::ERR_[A-Z0-9_]+", str(error))
            if code and code.group(0) in {"net::ERR_INVALID_AUTH_CREDENTIALS", "net::ERR_PROXY_AUTH_UNSUPPORTED",
                                         "net::ERR_PROXY_AUTH_REQUESTED"} and self._transport:
                raise BrowserError("浏览器连接认证异常：" + code.group(0) +
                    "；内部隧道不要求密码，请检查运行版本/代理配置，不要原样循环重试。", "internal", False) from None
            detail = code.group(0) if code else "导航等待超时" if isinstance(error, PlaywrightError) and "Timeout" in str(error) else "连接或导航失败"
            raise BrowserError("页面导航失败：" + detail + "；结合连接诊断选择恢复或停止，根因未确定。", "network") from None

    def _clear_elements(self):
        for handle, _ in self._elements.values():
            try:
                handle.dispose()
            except Exception:
                pass
        self._elements = {}
        self._observed_text = ""

    def observe(self, wait_seconds=2, text_start=0, text_length=65536,
                element_start=0, element_limit=200, link_start=0):
        """Return useful incomplete UI too, so the model can handle an overlay."""
        from playwright.sync_api import Error as PlaywrightError

        self.check()
        if (type(text_start) is not int or not 0 <= text_start <= 4_000_000
                or type(text_length) is not int or not 1 <= text_length <= 65536):
            raise AssistantError("正文起始位置必须为0～4000000的整数。")
        if (type(element_start) is not int or element_start < 0 or type(link_start) is not int or link_start < 0
                or type(element_limit) is not int or not 1 <= element_limit <= 200):
            raise AssistantError("控件/链接偏移须为非负整数，每次返回1～200个控件。")
        self._ensure_page()
        try:
            if self.page.url == "about:blank":
                return {"url": self.scope.start_url, "title": "", "text": "", "links": [], "elements": [],
                        "kind": "page", "ready": False, "fetched_at": now_iso(), "text_truncated": False,
                        "read_error": "尚未成功导航，可打开授权 URL 或报告停止。", **self.list_tabs(), **self.diagnostics()}
            if self.page.url.startswith("chrome-error:"):
                # Chromium replaced the page with its own error view (blocked
                # redirect, failed navigation). Report the real cause from
                # policy/transport diagnostics instead of rejecting the
                # chrome-error URL itself.
                blocks = "; ".join(item["target"] + " · " + item.get("rule", "") + " · " + item["reason"]
                                   for item in self.policy_blocks[-3:])
                failures = "; ".join(item["target"] + " · " + item["reason"]
                                     for item in (self._transport.failures() if self._transport else [])[-3:])
                detail = blocks or failures or "见 network_errors。"
                return {"url": self.scope.start_url, "title": "", "text": "", "links": [], "elements": [],
                        "kind": "page", "ready": False, "fetched_at": now_iso(), "text_truncated": False,
                        "read_error": "导航未成功（可能被权限拦截或连接失败）：" + detail +
                        "若是重定向目标越域，可换范围内入口或由用户扩大范围。", **self.list_tabs(), **self.diagnostics()}
            url = navigation_url(self.page.url, self.scope)
            deadline = time.monotonic() + wait_seconds
            previous, stable = "", 0
            text, useful = "", False
            while time.monotonic() < deadline:
                self.check()
                url = navigation_url(self.page.url, self.scope)
                text = self.page.locator("body").inner_text(timeout=2000)
                useful = bool(text.strip() and self._document_ok)
                stable = stable + 1 if useful and text == previous else 0
                if stable >= 2 and not self._pending_data:
                    break
                previous = text
                self.page.wait_for_timeout(300)
            errors = list(self.read_errors)
            if self.data_errors:
                errors.append("公开数据请求失败；请结合实际正文和诊断判断覆盖，不能视为零结果。")
            elif self._pending_data:
                errors.append("公开数据请求尚未完成；可等待或观察，不能仅凭页面框架判断零结果。")
            if risky_name(urlsplit(url).path):
                errors.append("路径含动作词，仅作为阅读风险提示；未据此证明业务写入或只读，勿执行账号/提交操作。")
            # A failed hydration/analytics script need not invalidate already
            # rendered article text. Keep failures visible; ready covers only
            # the current text snapshot, not interactive completeness.
            required_failures = [error for error in self.network_errors if error["type"] == "document"]
            script_warning = "部分脚本加载失败；当前正文可读取，不保证交互组件或全部内容完整。" if any(
                error["type"] == "script" for error in self.network_errors) else None
            ready = bool(useful and not required_failures and not self.internal_errors)
            if text_start > len(text):
                raise AssistantError("正文偏移超过页面长度，请调整 text_start。")
            window = text[text_start:text_start + text_length]
            ready = ready and bool(window.strip())
            final = navigation_url(self.page.url, self.scope)
            observed_links = {}
            for frame in self.page.frames:
                try:
                    links = frame.locator("a[href]").evaluate_all(
                        "els => els.map(e => ({text:e.innerText.slice(0,160),url:e.href}))")
                    for link in links:
                        # Observation is not authorization. Do not hide links
                        # because of their label/path/scheme or task host list.
                        observed_links.setdefault(link["url"], link)
                except PlaywrightError:
                    continue  # A frame may detach during this snapshot.
            heading = self.page.locator("h1").first
            title = heading.inner_text(timeout=2000) if heading.count() else self.page.title()
            self._tab_titles[self.page] = title[:300]
            self._clear_elements()
            self._observation += 1
            self._element_url = final
            elements = []
            handles, element_total = [], 0
            for frame in self.page.frames:
                candidates = selected = None
                try:
                    candidates = frame.evaluate_handle(ELEMENT_CANDIDATES, ELEMENT_SELECTOR)
                    count = candidates.evaluate("els => els.length")
                    start = max(0, element_start - element_total)
                    left = element_limit - len(handles)
                    element_total += count
                    if start < count and left > 0:
                        selected = candidates.evaluate_handle("(els, range) => els.slice(range[0], range[1])", [start, start + left])
                        handles.extend(selected.get_properties().values())
                except PlaywrightError:
                    continue
                finally:
                    for array in (selected, candidates):
                        if array is not None:
                            array.dispose()
            for handle in handles:
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
                                 "href": info["href"] or None,
                                 "options": info["options"],
                                 "context": "dialog" if info["dialog"] else "search" if info["search"] else
                                            "filter" if info["filter"] else "pagination" if info["pagination"] else "page",
                                 "restriction": "控件已禁用，不能点击" if info["disabled"] else None})
                self._elements[identity] = (handle, info)
            self._observed_text = text + "\n" + "\n".join(e["label"] for e in elements)
            blocked_loads = [item for item in self.policy_blocks if item["type"] in
                             STATIC_RESOURCE_TYPES | {"document", "xhr", "fetch"}]
            connection_errors = self._transport.failures() if self._transport else []
            if not ready and blocked_loads:
                errors.insert(0, "正文尚未取得；程序拦截：" + "; ".join(
                    item["target"] + " · " + item.get("rule", "") + " · " + item["reason"]
                    for item in blocked_loads[-3:]) +
                    "。原样刷新/等待不能解除权限拦截；path_keyword 可在核实只读后请用户加精确阅读豁免，其他规则请换范围内入口。")
            elif not ready and connection_errors:
                errors.insert(0, "正文尚未取得；连接诊断：" + "; ".join(
                    item["target"] + " · " + item["reason"] for item in connection_errors[-3:]))
            elif not ready and not text.strip() and self._document_ok:
                errors.insert(0, "主文档已返回，但可见正文为空；结合脚本/数据请求诊断判断，不等于零结果。")
            return {"url": final, "title": title[:300], "text": window,
                    "text_start": text_start, "total_characters": len(text),
                    "text_truncated": text_start > 0 or len(text) > text_start + text_length,
                    "links": list(observed_links.values())[link_start:link_start + 200],
                    "link_total": len(observed_links), "link_start": link_start,
                    "next_link_start": link_start + 200 if link_start + 200 < len(observed_links) else None,
                    "element_total": element_total, "element_start": element_start,
                    "next_element_start": element_start + element_limit if element_start + element_limit < element_total else None,
                    "tab_id": self._page_ids.get(self.page), **self.list_tabs(),
                    "fetched_at": now_iso(), "kind": "detail" if is_detail(final) else "page",
                    "blocked_requests": self.blocked, "elements": elements, "ready": ready,
                    "body_stable": stable >= 2, "resource_warning": script_warning, **self.diagnostics(),
                    "content_note": "ready仅表示当前正文快照可读，数据传输另行报告；HTTP成功不证明业务内容完整，须核对任务所需正文。",
                    "read_warning": errors[0] if ready and errors else None,
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
                raise AssistantError("当前控件不可执行此动作：可能不可见、已禁用或未开放填写。", 403)
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
        element = self._element(identity, "click")
        return self._click(element)

    def click_text(self, text, match=0):
        """Fixed browser locator, not model-provided JS/selectors or a label policy."""
        self.check()
        if not isinstance(text, str) or not text.strip() or len(text) > 200 or type(match) is not int or match < 0:
            raise AssistantError("点击文字须为1～200字符，match为从0开始的非负匹配序号。", 400)
        if text not in self._observed_text:
            raise BrowserError("文字未出现在当前观察中，请先观察页面。", "interaction", True, 409)
        targets = []
        for frame in self.page.frames:
            locator = frame.get_by_text(text, exact=True)
            for index in range(locator.count()):
                target = locator.nth(index)
                if target.is_visible():
                    targets.append(target)
        if match >= len(targets):
            raise BrowserError(f"当前有{len(targets)}个可见精确文字匹配；请使用完整文字和有效match，或点击元素编号。", "interaction", True, 409)
        return self._click(targets[match])

    def _click(self, element):
        from playwright.sync_api import Error as PlaywrightError
        try:
            element.click(timeout=5000)  # No force=True: do not click through overlays.
            # Pump popup events without selecting or closing the new tab.
            self.page.wait_for_timeout(200)
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

    def refresh(self):
        if self.page.url == "about:blank" or self.page.url.startswith("chrome-error:"):
            raise BrowserError("尚无可刷新的成功页面；请重新打开任务URL，而不是刷新空白/错误页。", "interaction", False, 409)
        return self.read(self.page.url)

    def back(self):
        # Validate the history target before triggering any network request.
        history = self._cdp.send("Page.getNavigationHistory")
        index = history["currentIndex"] - 1
        if index < 0:
            raise AssistantError("没有可返回的页面。", 409)
        navigation_url(history["entries"][index]["url"], self.scope)
        self.check()
        self.network_errors, self.policy_blocks, self.read_errors = [], [], []
        if self._transport:
            self._transport.clear_failures()
        self.data_errors.clear()
        self._pending_data.clear()
        self.data_responses.clear()
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
