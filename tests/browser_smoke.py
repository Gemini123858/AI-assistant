"""Optional Chromium UI check; always demo mode, always temporary synthetic data."""

import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from assistant.config import Config
from assistant.web import create_app


def main():
    with tempfile.TemporaryDirectory() as temporary:
        app = create_app(Config(data_dir=Path(temporary) / "data", mode="demo"))
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        screenshots = Path("data/browser-check")
        screenshots.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel="chromium")
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#mode-badge")).to_have_text("演示模式")
                page.get_by_role("button", name="载入合成样例").click()
                expect(page.locator("#message-count")).to_have_text("2")
                page.locator(".message-card").first.get_by_role("button", name="分析通知").click()
                expect(page.locator(".message-card").first.locator(".summary-text")).to_contain_text("[演示]")
                page.locator(".message-card").first.get_by_role("button", name="查看原文").click()
                expect(page.locator("#source-dialog")).to_be_visible()
                expect(page.locator("#source-attachments")).to_contain_text("未读取")
                page.get_by_role("button", name="关闭", exact=True).click()
                page.locator("#chat-input").fill("/规则 不要把报名截止当成活动开始")
                page.locator("#chat-button").click()
                expect(page.locator("#chat-history")).to_contain_text("已记住")
                page.get_by_role("button", name="画像与规则", exact=True).click()
                expect(page.locator("#rules-text")).to_have_value("- 不要把报名截止当成活动开始")
                page.locator("#profile-text").fill("我目前在找实习（合成测试）。")
                page.get_by_role("button", name="保存画像", exact=True).click()
                expect(page.locator("#toast")).to_contain_text("已保存")
                page.get_by_role("button", name="通知工作台", exact=True).click()
                page.locator("#conversation").fill("合成安全测试")
                page.locator("#notification-text").fill('<img src=x onerror="window.__injected=true">')
                page.locator("#import-button").click()
                expect(page.locator("#message-count")).to_have_text("3")
                assert page.evaluate("window.__injected === undefined")
                assert page.locator(".message-card img").count() == 0
                page.screenshot(path=str(screenshots / "desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.locator("#chat-input")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(screenshots / "mobile.png"), full_page=True)
                assert not errors, errors
                browser.close()
            print("Browser smoke passed: import, analysis, source, chat, memory, text rendering, narrow layout.")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.extensions["store"].close()


if __name__ == "__main__":
    main()
