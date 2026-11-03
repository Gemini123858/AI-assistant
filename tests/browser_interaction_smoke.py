"""Real Chromium with synthetic public pages; no external network or model API."""
import json

from assistant.browser import BrowserScope, JobBrowser, interaction_error

START = "https://catalog.example/article"
ARTICLE = """<html><head><title>原始文章</title></head><body><h1>原始文章</h1>
<a href="/background" target="_blank">打开搜索页</a><p>目标文章正文</p>
<div id="delegated" style="cursor:pointer">事件委托卡片<span>装饰文字</span></div>
<script>document.querySelector('#delegated').addEventListener('click',()=>document.body.dataset.clicked='yes')</script>
</body></html>"""
BACKGROUND = """<html><head><title>后台搜索页</title></head><body><h1>后台搜索页</h1>
<input type="search" placeholder="后台搜索框"><script>
setInterval(()=>fetch('/background-data').catch(()=>{}),30);
</script></body></html>"""
CONFIG = """<html><head><title>配置页面</title><style>
.choice{position:relative;width:320px;height:100px;margin:20px}
input{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0}
label{position:absolute;inset:0;display:block;background:#ddd;cursor:pointer;padding:20px}
</style></head><body><h1>公开配置</h1>
<div class="choice"><input type="radio" name="size" value="small" id="small"><label for="small"><span>小尺寸</span><span> 公开说明</span></label></div>
<div class="choice"><input type="radio" name="size" value="large" id="large"><label for="large">大尺寸</label></div>
<div id="overlay" style="position:fixed;inset:0;z-index:100;background:white;display:none">普通覆盖层</div>
</body></html>"""


class SyntheticBrowser(JobBrowser):
    def _route_request(self, route, *, page=None):
        if not self.permitted(route.request):
            return super()._route_request(route, page=page)
        path = route.request.url.split("catalog.example", 1)[-1]
        if path == "/background-data":
            route.fulfill(status=503, content_type="application/json", body='{"unavailable":true}')
        else:
            html = CONFIG if path == "/config" else BACKGROUND if path == "/background" else ARTICLE
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)


def main():
    with SyntheticBrowser(lambda: None, scope=BrowserScope(START, public_web=True)) as browser:
        page = browser.read(START)
        card = next(e for e in page["elements"] if "事件委托卡片" in e["label"])
        assert len([e for e in page["elements"] if "装饰文字" in e["label"]]) == 1
        browser.click(card["id"])
        assert browser.page.get_attribute("body", "data-clicked") == "yes"
        page = browser.observe(include_all_elements=True)
        assert len(page["elements"]) > len(browser.observe()["elements"])
        page = browser.observe()
        popup = next(e for e in page["elements"] if e["label"] == "打开搜索页")
        browser.click(popup["id"])
        assert len(browser.list_tabs()["tabs"]) == 2
        browser.switch_tab("t2")
        for _ in range(3):
            browser.switch_tab("t1")
            page = browser.read(START)
            assert page["active_tab"] == page["tab_id"] == "t1"
            assert page["title"] == "原始文章" and "目标文章正文" in page["text"]
            assert not any(e["label"] == "后台搜索框" for e in page["elements"])
            assert not page["network_errors"]
        browser.switch_tab("t2")
        assert browser.network_errors, "Background errors must remain in their own tab."
        browser.close_tab("t2")
        page = browser.read("https://catalog.example/config")
        radios = [e for e in page["elements"] if e["role"] == "radio"]
        assert len(radios) == 2 and all(e["checked"] is False for e in radios)
        assert not any(e["role"] == "button" and "小尺寸" in e["label"] for e in page["elements"])
        page = browser.check_field(radios[0]["id"], True)
        assert next(e for e in page["elements"] if e["role"] == "radio" and e["value"] == "small")["checked"]
        large = next(e for e in page["elements"] if e["value"] == "large")
        browser.page.locator('#overlay').evaluate("e=>e.style.display='block'")
        try:
            browser.check_field(large["id"], True)
            raise AssertionError("A normal overlay must not be bypassed.")
        except Exception as error:
            assert interaction_error(error, "check_field").details["code"] == "pointer_intercepted", str(error)
        assert not browser.page.locator('#large').is_checked()
    print("Chromium passed: tab/diagnostic isolation, delegated card, broad candidates, radio label selection, overlay feedback.")


if __name__ == "__main__":
    main()
