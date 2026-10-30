"""Offline notification cards, source conversion and text import in Chromium."""
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from assistant.config import Config
from assistant.web import create_app
from .test_storage import notification
from .test_web import analysis_output
from .browser_conversation_smoke import Quiet


def main():
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Config(data_dir=Path(folder) / "data"))
        store = app.extensions["store"]
        source, _ = store.import_message(notification())
        store.save_result(source["id"], analysis=analysis_output())
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        errors = []
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel="chromium")
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                expect(page.locator("#conversation-list button")).to_have_count(1)
                page.locator("#import-text").click()
                page.locator("#text-file").set_input_files({"name": "synthetic.txt", "mimeType": "text/plain", "buffer": "合成文件内容，不自动分析。".encode()})
                expect(page.locator("#notification-text")).to_have_value("合成文件内容，不自动分析。")
                page.locator("#import-form button[type=submit]").click()
                expect(page.locator("#attached-records")).to_contain_text("合成文件内容")
                assert len(store.list_messages()) == 2
                page.locator("#add-records").click()
                page.locator("#record-kind").select_option("text")
                page.locator("#record-query").fill(analysis_output()["summary"])
                page.locator("#record-search button").click()
                page.locator("#record-results").get_by_role("button", name="预览").first.click()
                page.locator("#preview-content").get_by_role("button", name="保存为通知").click()
                expect(page.locator("#record-meta")).to_contain_text("通知")
                assert len(store.list_notices()) == 1
                page.locator("#record-content").get_by_role("button", name="编辑", exact=True).click()
                page.locator("#notice-title").fill("合成通知卡片")
                day = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).date().isoformat()
                page.locator("#notice-deadline-mode").select_option("date")
                page.locator("#notice-deadline").fill(day)
                page.locator("#notice-form button[type=submit]").click()
                expect(page.locator("#record-title")).to_have_text("合成通知卡片")
                assert store.list_notices()[0]["content"]["deadline"] == day
                page.locator("#chat-input").fill("保留这段未发送文字")
                page.locator("#open-notices").click()
                card = page.locator(".notice-card").filter(has_text="合成通知卡片")
                expect(card).to_contain_text(day)
                card.get_by_role("button", name="加入对话").click()
                expect(page.locator("#chat-input")).to_have_value("保留这段未发送文字")
                expect(page.locator("#attached-records")).to_contain_text("通知 · 合成通知卡片")
                page.locator("#open-notices").click()
                card.get_by_role("button", name="删除", exact=True).click()
                expect(page.locator("#notice-cards .notice-card")).to_have_count(0)
                page.locator("#notice-view").select_option("deleted")
                expect(page.locator("#notice-cards")).to_contain_text("已删除")
                page.locator("#notice-cards").get_by_role("button", name="恢复通知").click()
                expect(page.locator("#notice-cards .notice-card")).to_have_count(0)
                page.locator("#notice-view").select_option("all")
                page.locator("#new-notice").click()
                page.locator("#notice-title").fill("无来源的手工通知")
                page.locator("#notice-form button[type=submit]").click()
                expect(page.locator("#notice-cards .notice-card")).to_have_count(2)
                page.locator("#notice-view").select_option("undated")
                expect(page.locator("#notice-cards .notice-card")).to_have_count(1)
                expect(page.locator("#notice-cards")).to_contain_text("无来源的手工通知")
                page.locator("#notice-view").select_option("upcoming")
                expect(page.locator("#notice-cards .notice-card")).to_have_count(1)
                page.locator("#notice-view").select_option("all")
                output = Path("data/browser-check"); output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / "notices-desktop.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.locator("#notices-dialog").evaluate("element => element.scrollWidth <= element.clientWidth")
                page.screenshot(path=str(output / "notices-mobile.png"))
                page.locator("#close-notices").click()
                page.reload()
                page.locator("#open-notices").click()
                expect(page.locator("#notice-cards .notice-card")).to_have_count(2)
                assert store.get_message(source["id"])["analysis"] == analysis_output()
                assert not errors, errors
                browser.close()
            print("Notification UI passed: text file import, analysis conversion, date edit, shared reference, delete/restore, filters, reload and mobile.")
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
            app.extensions["conversations"].close(); app.extensions["mail"].close()
            app.extensions["tasks"].close(); store.close()


if __name__ == "__main__":
    main()
