"""Offline Markdown rendering, inert HTML, links, history and narrow layouts."""

import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from assistant.config import Config
from assistant.web import create_app
from .browser_conversation_smoke import Quiet
from .test_conversations import FlowModel, finish


MARKDOWN = '''## 合成 Markdown 回答

这是 **重点**、*强调*、~~删除线~~ 和 `inline_code`。

- 第一项
  - 子项
- 第二项

1. 检查资料
2. 整理结果

> 合成引用内容

| 名称 | 数量 |
| :--- | ---: |
| 合成项目 | 42 |

```python
print("<script>这只是代码</script>")
'''+ 'print("' + 'long_code_' * 35 + '")\n' + '''```

[合成链接](https://example.invalid/jobs)
https://example.invalid/plain
[邮件链接](mailto:synthetic@example.invalid)
![合成图片](https://example.invalid/remote.png)

<script>window.markdownExecuted = true</script>
<img src="https://example.invalid/html.png" onerror="window.markdownExecuted = true">
[危险链接](javascript:alert(1))
[编码危险链接](jav&#x61;script:alert(1))
[内嵌数据](data:text/html,test)
[本地文件](file:///etc/passwd)
[凭据链接](https://user:password@example.invalid/)
'''


def main():
    with tempfile.TemporaryDirectory() as folder:
        model = FlowModel([finish(MARKDOWN)])
        app = create_app(Config(data_dir=Path(folder) / "data", mode="api", api_key="synthetic"), model)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        errors, remote = [], []
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel="chromium")
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                base = f"http://127.0.0.1:{server.server_port}"
                page.on("request", lambda request: remote.append(request.url) if not request.url.startswith(base + "/") else None)
                page.goto(base + "/")
                page.locator("#chat-input").fill("用户原文 **保持原样**")
                page.locator("#chat-button").click()
                answer = page.locator(".answer-text")
                expect(answer.locator("h2")).to_have_text("合成 Markdown 回答")
                expect(answer.locator("strong")).to_have_text("重点")
                expect(answer.locator("em")).to_have_text("强调")
                expect(answer.locator("s")).to_have_text("删除线")
                expect(answer.locator("ul ul li")).to_have_text("子项")
                expect(answer.locator("ol li")).to_have_count(2)
                expect(answer.locator("blockquote")).to_contain_text("合成引用内容")
                expect(answer.locator("table tbody td")).to_have_count(2)
                expect(answer.locator("td.markdown-align-right")).to_have_text("42")
                expect(answer.locator("pre code.language-python")).to_contain_text("<script>这只是代码</script>")
                expect(answer.locator("a").filter(has_text="合成链接")).to_have_attribute("target", "_blank")
                expect(answer.locator("a").filter(has_text="合成链接")).to_have_attribute("rel", "noopener noreferrer")
                expect(answer.locator("a[href='mailto:synthetic@example.invalid']")).to_have_count(1)
                expect(answer.locator("a[href='https://example.invalid/remote.png']")).to_contain_text("图片")
                expect(answer.locator("script,img,iframe,[onerror],[onclick],[style]")).to_have_count(0)
                self_links = answer.locator("a").evaluate_all("links => links.map(link => link.getAttribute('href'))")
                assert all(url.startswith(("https://", "http://", "mailto:")) and "password" not in url for url in self_links)
                assert page.evaluate("window.markdownExecuted === undefined")
                expect(page.locator(".chat-entry.user")).to_contain_text("**保持原样**")
                expect(page.locator(".chat-entry.user strong")).to_have_count(0)
                page.reload()
                expect(page.locator(".answer-text h2")).to_have_text("合成 Markdown 回答")
                store = app.extensions["store"]
                saved = next(e["text"] for e in store.list_conversations()[0]["events"] if e["type"] == "answer")
                assert saved == MARKDOWN.strip()
                output = Path("data/browser-check"); output.mkdir(parents=True, exist_ok=True)
                page.locator("#chat-history").evaluate("element => element.scrollTop = 0")
                page.screenshot(path=str(output / "markdown-desktop.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert answer.locator("pre").evaluate("element => element.scrollWidth > element.clientWidth")
                page.screenshot(path=str(output / "markdown-mobile.png"))
                assert not errors, errors
                assert not remote, remote
                browser.close()
            print("Markdown UI passed: formatting, tables, code, links, inert HTML, no remote images, unchanged source, reload and mobile.")
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
            for name in ("conversations", "mail", "tasks", "store"):
                app.extensions[name].close()


if __name__ == "__main__":
    main()
