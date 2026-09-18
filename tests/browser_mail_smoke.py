"""Local synthetic IMAP + Chromium UI, no model API or real account."""
import threading
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from assistant.config import Config
from assistant.web import create_app
from .test_mail import FakeIMAP, TOKEN, analysis
from .test_tasks import ScriptModel, call


def main():
    with tempfile.TemporaryDirectory() as temporary:
        imap = FakeIMAP()
        model = ScriptModel([call("save_mail_analysis", analysis())])
        app = create_app(Config(data_dir=Path(temporary)/"data", mode="api", api_key="synthetic",
                         qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic", access_token=TOKEN),
                         model, mail_connector=lambda: imap)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel="chromium")
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    page.locator('input[name="token"]').fill(TOKEN)
                    page.get_by_role("button", name="登录", exact=True).click()
                    page.locator('[data-tab="mail"]').click()
                    with page.expect_response(lambda r: r.url.endswith("/api/mail/start") and r.status == 200):
                        page.locator("#mail-start").click()
                    imap.uids = [10, 11]
                    with page.expect_response(lambda r: r.url.endswith("/api/mail/sync") and r.status == 200):
                        page.locator("#mail-sync").click()
                    page.locator("#mail-list").get_by_role("heading", name="聊天记录").wait_for()
                    page.get_by_role("button", name="细读分析（调用模型）", exact=True).click()
                    page.locator("#mail-events td").filter(has_text="提交申请").first.wait_for()
                    assert "截止" in page.locator("#mail-events").inner_text()
                    assert "2026-09-25" in page.locator("#mail-events").inner_text()
                    page.get_by_role("button", name="只读查看正文").click()
                    page.locator("#source-dialog[open]").wait_for()
                    assert "我正在找实习" in page.locator("#source-body").inner_text()
                    page.locator("#close-dialog").click()
                    page.locator("#mail-list .message-card").first.evaluate("""e => {
                        const link = document.createElement('p'); link.className = 'caption';
                        link.textContent = 'https://public.example/' + 'a'.repeat(200); e.append(link);
                    }""")
                    output = Path("data/browser-check")
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output/"v03-mail-desktop.png"))
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output/"v03-mail-mobile.png"))
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.once("dialog", lambda dialog: dialog.accept())
                    with page.expect_response(lambda r: r.url.endswith("/api/mail/latest") and r.status == 200):
                        page.locator("#mail-latest").click()
                    page.locator("#logout-button").click()
                    page.wait_for_url("**/login")
                    assert page.request.get(f"http://127.0.0.1:{server.server_port}/api/mail").status == 401
                    print("Offline mail UI passed: login, baseline, new header, read-only body, event table, narrow layout and logout.")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            worker.join(timeout=5)
            app.extensions["mail"].close()
            app.extensions["tasks"].close()
            app.extensions["store"].close()


if __name__ == "__main__":
    main()
