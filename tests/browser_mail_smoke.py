"""Local synthetic IMAP + Chromium UI, no model API or real account."""
import re
import threading
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from assistant.config import Config
from assistant.web import create_app
from .test_drafts import FakeSMTP
from .test_mail import FakeIMAP, TOKEN, analysis
from .test_tasks import ScriptModel, call


def main():
    with tempfile.TemporaryDirectory() as temporary:
        imap = FakeIMAP()
        smtp = FakeSMTP()
        model = ScriptModel([call("save_mail_analysis", analysis())])
        app = create_app(Config(data_dir=Path(temporary)/"data", mode="api", api_key="synthetic",
                         qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic", access_token=TOKEN),
                         model, mail_connector=lambda: imap, smtp_connector=lambda: smtp)
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
                    page.get_by_role("heading", name="继续追问").wait_for()
                    assert "截止" in page.locator("#mail-events").inner_text()
                    assert "2026-09-25" in page.locator("#mail-events").inner_text()
                    page.get_by_role("button", name="只读查看正文").click()
                    page.locator("#source-dialog[open]").wait_for()
                    assert "我正在找实习" in page.locator("#source-body").inner_text()
                    page.locator("#close-dialog").click()
                    with page.expect_response(lambda r: r.url.endswith("/api/drafts") and r.status == 201):
                        page.get_by_role("button", name="根据此邮件写邮件", exact=True).click()
                    expect(page.locator("#draft-from")).to_have_value("owner@qq.com")
                    assert page.locator("#draft-from").input_value() == "owner@qq.com"
                    assert "邮件分析" in page.locator("#draft-source").inner_text()
                    page.locator("#draft-to").fill("recipient@example.com")
                    page.locator("#draft-subject").fill("合成草稿")
                    page.locator("#draft-body").fill("仅保存，不发送。")
                    with page.expect_response(lambda r: "/api/drafts/" in r.url and r.request.method == "PUT" and r.status == 200):
                        page.locator("#draft-save").click()
                    expect(page.locator("#draft-status")).to_contain_text("版本 2")
                    with page.expect_response(lambda r: "/api/drafts/" in r.url and r.request.method == "PUT" and r.status == 200):
                        page.locator("#draft-save").click()
                    expect(page.locator("#draft-status")).to_contain_text("版本 2")
                    page.locator("#draft-new").click()
                    expect(page.locator("#draft-list button")).to_have_count(2)
                    first_draft = page.locator("#draft-list button").filter(has_text="合成草稿")
                    first_draft.click()
                    expect(first_draft).to_have_class(re.compile(r"\bselected\b"))
                    expect(page.locator("#draft-subject")).to_have_value("合成草稿")
                    with page.expect_response(lambda r: r.url.endswith("/send") and r.status == 200):
                        page.locator("#draft-send").click()
                    expect(page.locator("#delivery-list")).to_contain_text("合成草稿")
                    assert "合成草稿" not in page.locator("#draft-list").inner_text()
                    page.once("dialog", lambda dialog: dialog.accept())
                    with page.expect_response(lambda r: "/api/drafts/" in r.url and r.request.method == "DELETE" and r.status == 200):
                        page.locator("#draft-delete").click()
                    expect(page.locator("#draft-list button")).to_have_count(0)
                    expect(page.locator("#draft-status")).to_contain_text("尚未创建草稿")
                    page.locator("#mail-list .message-card").first.evaluate("""e => {
                        const link = document.createElement('p'); link.className = 'caption';
                        link.textContent = 'https://public.example/' + 'a'.repeat(200); e.append(link);
                    }""")
                    output = Path("data/browser-check")
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output/"v04-mail-desktop.png"))
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output/"v04-mail-mobile.png"))
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    page.once("dialog", lambda dialog: dialog.accept())
                    with page.expect_response(lambda r: r.url.endswith("/api/mail/latest") and r.status == 200):
                        page.locator("#mail-latest").click()
                    page.locator("#logout-button").click()
                    page.wait_for_url("**/login")
                    assert page.request.get(f"http://127.0.0.1:{server.server_port}/api/mail").status == 401
                    print("Offline mail UI passed: contextual drafts, stable versions, deletion, selected state, separate delivery records, narrow layout and logout.")
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
