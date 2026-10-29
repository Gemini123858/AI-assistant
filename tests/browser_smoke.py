"""Current demo UI smoke; temporary data, no model or remote service."""
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from assistant.config import Config
from assistant.web import create_app


def main():
    with tempfile.TemporaryDirectory() as temporary:
        app = create_app(Config(data_dir=Path(temporary)/"data"))
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        errors = []
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel="chromium")
                page = browser.new_page(viewport={"width":1280,"height":900})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                expect(page.locator("#model-status")).to_contain_text("演示模式")
                expect(page.locator("#conversation-list button")).to_have_count(1)
                page.locator("#add-records").click(); page.locator("#open-notification-import").click()
                page.locator("#notification-text").fill('<img src=x onerror="window.__injected=true">合成通知')
                page.locator("#import-form button").click()
                expect(page.locator("#attached-records")).to_contain_text("合成通知")
                page.locator("#chat-input").fill("帮我看看这条通知")
                page.locator("#chat-button").click()
                expect(page.locator("#chat-history")).to_contain_text("未调用真实模型", timeout=10000)
                assert page.evaluate("window.__injected === undefined")
                assert page.locator("#chat-history img").count() == 0
                page.locator("#open-settings").click()
                page.locator("#rules-text").fill("合成规则")
                page.locator('[data-kind="rules"] button[type="submit"]').click()
                expect(page.locator("#toast")).to_contain_text("已保存")
                page.locator("#close-settings").click()
                page.reload()
                expect(page.locator("#chat-history")).to_contain_text("未调用真实模型")
                page.set_viewport_size({"width":390,"height":844})
                expect(page.locator("#chat-input")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors, errors
                browser.close()
            print("Demo UI passed: notification import, attachment, persistent conversation, settings, safe text rendering and mobile layout.")
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
            app.extensions["conversations"].close(); app.extensions["mail"].close()
            app.extensions["tasks"].close(); app.extensions["store"].close()


if __name__ == "__main__":
    main()
