"""Real Chromium, locally fulfilled public sites; no cloud/model/account access."""

from urllib.parse import urlsplit
from unittest.mock import patch

from assistant.browser import BrowserScope, JobBrowser, allowed_request
from assistant.errors import AssistantError


START = "https://catalog.example/topics"
DETAIL = "https://catalog.example/articles/graph"
HTML = """<!doctype html><html lang="zh"><h1>公开资料目录</h1>
<form role="search" action="/topics" method="get"><input type="search" name="q" placeholder="搜索资料"><button>查找</button></form>
<div class="filters"><label>城市<select aria-label="城市"><option value="all">全部</option><option value="sh">上海</option></select></label>
<label><input type="checkbox">仅开放资料</label></div>
<a href="/articles/graph">图结构资料</a><form method="post" action="/api/save"><input placeholder="姓名"><button>保存</button></form>
<button type="button" onclick="fetch('/api/save',{method:'POST',body:'{}'})">发送</button>
<div id="react-card">React 无角色资料卡片</div><div onclick="location.href='/articles/graph'">原生无角色卡片</div>
<script>fetch('https://127.0.0.1/private');fetch('/api/save',{method:'POST',body:'{}'});
const card=document.querySelector('#react-card');
card['__reactProps$synthetic']={onClick:()=>location.href='/articles/graph'};
card.addEventListener('click',card['__reactProps$synthetic'].onClick);
</script></html>"""


class LocalBrowser(JobBrowser):
    def _route(self, route):
        request = route.request
        if not allowed_request(request.url, request.method, request.resource_type, request.post_data, self.scope):
            return super()._route(route)
        path = urlsplit(request.url).path
        if path == "/redirect":
            route.fulfill(status=302, headers={"Location": "https://catalog.example/delete"}, body="")
        elif path == "/foreign":
            route.fulfill(status=302, headers={"Location": "https://foreign.example/private"}, body="")
        elif path == "/blank":
            route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>")
        elif path == "/broken.js":
            route.abort("failed")
        elif path == "/static-article":
            route.fulfill(status=200, content_type="text/html; charset=utf-8",
                          body='<h1>静态文章</h1><p>已渲染公开正文</p><script src="/broken.js"></script>')
        elif path == "/api/catalog":
            route.fulfill(status=200, content_type="application/json", body='{"title":"公开 SPA 资料已加载"}')
        elif path == "/spa":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body="""<h1>公开目录</h1>
<div id="data"></div><p id="clock"></p><script>
fetch('/api/catalog').then(r=>r.json()).then(d=>document.querySelector('#data').textContent=d.title);
setInterval(()=>document.querySelector('#clock').textContent=String(Date.now()),100);
</script>""")
        elif path == "/articles/graph":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<h1>图结构资料</h1><p>公开方法说明</p>")
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML)


def element(page, label):
    return next(e for e in page["elements"] if e["label"] == label)


class GenericResponseBrowser(JobBrowser):
    def _route(self, route):
        request = route.request
        if not allowed_request(request.url, request.method, request.resource_type, request.post_data, self.scope):
            return super()._route(route)
        if request.method == "POST":
            route.fulfill(status=self.response_status, content_type="application/json", body=self.response_body)
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body="""<h1>职位列表框架</h1>
<script>fetch('/api/unknown-data',
{method:'POST',body:'{}'}).then(r=>r.json());</script>""")


def main():
    scope = BrowserScope(START)
    with patch("assistant.browser.resolver_rules", return_value="MAP * ~NOTFOUND"):
        with LocalBrowser(lambda: None, scope=scope) as browser:
            first = browser.read(START)
            assert first["ready"] and first["kind"] == "page"
            assert browser.blocked >= 2
            assert not first["network_errors"], "policy blocks must not become network failures"
            assert browser.click(element(first, "React 无角色资料卡片")["id"])["ready"]
            first = browser.read(START)
            assert browser.click(element(first, "原生无角色卡片")["id"])["url"] == DETAIL
            first = browser.read(START)
            search_id = element(first, "搜索资料")["id"]
            browser.page.locator('input[type="search"]').evaluate("e=>e.placeholder='搜索资料（已更新）'")
            first = browser.fill(search_id, "公开查询", False)
            assert element(first, "姓名")["actions"] == []
            assert element(first, "发送")["actions"] == []
            for label in ("保存", "发送"):
                try:
                    browser.click(element(first, label)["id"])
                    raise AssertionError("Unauthorized write control executed")
                except AssistantError as error:
                    assert error.status == 403
            search = browser.fill(element(first, "搜索资料（已更新）")["id"], "KG alignment 图结构 上海", False)
            search = browser.press(element(search, "搜索资料（已更新）")["id"], "Enter")
            assert "q=" in search["url"] and search["ready"]
            selected = browser.select(element(search, "城市")["id"], "sh")
            checked = browser.check_field(element(selected, "仅开放资料")["id"], True)
            browser.scroll("down")
            assert browser.read(DETAIL)["ready"]
            assert "/topics" in browser.back()["url"]
            assert browser.refresh()["ready"]
            assert browser.wait(1)["ready"]
            image = browser.screenshot()
            assert image.startswith("data:image/png;base64,")
            partial = browser.observe(text_start=5)
            assert partial["text_start"] == 5 and partial["text_truncated"] and partial["ready"]
            window = browser.observe(text_start=5, text_length=12)
            assert window["text"] == partial["text"][:12] and window["ready"]
            fragment = browser.read(browser.page.url + "#资料")
            assert fragment["ready"] and "#" in fragment["url"]
            blank = browser.read("https://catalog.example/blank")
            assert not blank["ready"] and blank["text"] == "" and "read_error" in blank
            spa = browser.read("https://catalog.example/spa")
            assert spa["ready"] and not spa["body_stable"] and "公开 SPA 资料已加载" in spa["text"]
            article = browser.read("https://catalog.example/static-article")
            assert article["ready"] and "已渲染公开正文" in article["text"]
            assert article["resource_warning"] and any(e["type"] == "script" for e in article["network_errors"])
            browser.hover(element(browser.read(START), "图结构资料")["id"])
            browser.escape()
            for path in ("/redirect", "/foreign"):
                try:
                    browser.read("https://catalog.example" + path)
                except AssistantError as error:
                    assert getattr(error, "kind", None) == "permission"
                assert any("重定向" in p["reason"] for p in browser.policy_blocks), "redirect response was not guarded"
                assert not any(e["code"] == "net::ERR_BLOCKED_BY_CLIENT" for e in browser.network_errors)
        start = "https://catalog.example/public-list"
        with GenericResponseBrowser(lambda: None, scope=BrowserScope(start)) as browser:
            browser.response_status, browser.response_body = 503, '{}'
            failed = browser.read(start)
            assert failed["ready"] and failed["data_errors"] and "不能视为零结果" in failed["read_warning"]
            browser.response_status, browser.response_body = 200, '[]'
            recovered = browser.refresh()
            assert recovered["ready"] and not recovered["data_errors"]
            assert recovered["content_note"], "Transport success must not promise business completeness"
    print("Offline generic Chromium passed: second-site DOM, free search/Enter, select/check, back/refresh/wait, screenshot, blank observation, redirect and write boundaries.")


if __name__ == "__main__":
    main()
