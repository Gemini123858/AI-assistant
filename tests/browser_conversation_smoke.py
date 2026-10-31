"""Offline unified UI flow. Fake LLM, IMAP, browser and SMTP; no external requests."""
import json
import re
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from assistant.config import Config
from assistant.web import create_app
from .test_conversations import FlowModel, fields, finish
from .test_drafts import FakeSMTP
from .test_generic_tasks import result
from .test_mail import FakeIMAP, TOKEN, analysis
from .test_tasks import FixtureBrowser, LIST, call


class Quiet(WSGIRequestHandler):
    def log_request(self, *args):
        pass


def main():
    with tempfile.TemporaryDirectory() as folder:
        imap, smtp, model = FakeIMAP(), FakeSMTP(), FlowModel([])
        app = create_app(Config(data_dir=Path(folder)/"data", mode="api", api_key="synthetic",
            qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic", access_token=TOKEN,
            daily_calls=100), model, FixtureBrowser, lambda: imap, lambda: smtp)
        imap.uids = [10]
        mail = app.extensions["mail"].read_latest()
        mail_ref = {"kind": "mail", "id": mail["id"]}
        def create_after_browser(messages):
            refs = []
            for message in messages:
                if message["role"] == "tool":
                    refs.extend(item["ref"] for item in json.loads(message["content"]).get("records", []))
            return call("create_draft", {**fields(), "source_refs": refs})
        def update_current(messages):
            attached = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "user" and '"question"' in message["content"]))["attached_records"]
            snapshot = next(item for item in attached if item["ref"]["kind"] == "draft")
            return call("update_draft", {"id": snapshot["ref"]["id"], "version": snapshot["ref"]["version"], **fields(body="合成模型修改后的正文")})
        model.outputs = [call("read_record", {"ref": mail_ref}), call("save_mail_analysis", {"id": mail["id"], "analysis": analysis()}),
            call("run_browser", {"goal": "读取公开岗位要求", "start_url": LIST}),
            call("click_element", {"element_id": "stale-synthetic-element"}), call("finish_task", result()),
            create_after_browser, {**finish("合成查询与草稿已完成，尚未发送。"),
                "_usage": {"prompt_tokens": 1234, "completion_tokens": 200, "total_tokens": 1434}},
            update_current, finish("已修改同一份草稿。")]
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        errors = []
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, channel="chromium")
                page = browser.new_page(viewport={"width":1440,"height":1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.locator('input[name="token"]').fill(TOKEN)
                page.get_by_role("button", name="登录", exact=True).click()
                expect(page.locator("#conversation-list button")).to_have_count(1)
                page.locator("#add-records").click()
                page.locator("#record-kind").select_option("mail")
                page.locator("#record-results").get_by_role("button", name="预览").first.click()
                expect(page.locator("#preview-content .analysis-empty")).to_be_visible()
                expect(page.locator("#preview-content")).to_contain_text("本人：我正在找实习")
                assert app.extensions["mail"].get(mail["id"])["analysis"] is None
                page.locator("#close-preview").click()
                page.locator("#record-results input[type=checkbox]").first.check()
                page.locator("#picker-add").click()
                expect(page.locator("#attached-records")).to_contain_text("聊天记录")
                page.locator("#chat-input").fill("分析这封邮件，查官网并起草咨询邮件")
                page.locator("#chat-button").click()
                expect(page.locator("#chat-history")).to_contain_text("合成查询与草稿已完成", timeout=20000)
                expect(page.locator("#context-status")).to_contain_text("1,234 tokens")
                page.locator("#add-records").click()
                page.locator("#record-kind").select_option("mail")
                page.locator("#record-results").get_by_role("button", name="预览").first.click()
                expect(page.locator("#preview-content")).to_contain_text("分析摘要")
                expect(page.locator("#preview-content")).to_contain_text(analysis()["summary"])
                expect(page.locator("#preview-content .analysis-empty")).to_have_count(0)
                page.locator("#close-preview").click()
                page.locator("#record-kind").select_option("task")
                page.locator("#record-results").get_by_role("button", name="预览").first.click()
                expect(page.locator("#preview-content")).to_contain_text(result()["summary"])
                expect(page.locator("#preview-content .source-card")).to_have_count(1)
                expect(page.locator("#preview-content .source-line")).to_have_count(0)
                page.locator("#preview-content").get_by_role("button", name=re.compile("定位原文")).click()
                expect(page.locator("#preview-content .is-cited")).to_contain_text("岗位列表")
                page.locator("#preview-content").get_by_text(re.compile("^浏览动作与诊断")).click()
                expect(page.locator("#preview-content .action-row")).to_have_count(2)
                expect(page.locator("#preview-content .action-row.failed")).to_contain_text("合成元素编号过期")
                expect(page.locator("#preview-content .debug-data")).to_be_hidden()
                output = Path("data/browser-check"); output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output/"record-preview-desktop.png"))
                page.set_viewport_size({"width":390,"height":844})
                assert page.locator("#preview-dialog").evaluate("element => element.scrollWidth <= element.clientWidth")
                page.screenshot(path=str(output/"record-preview-mobile.png"))
                page.set_viewport_size({"width":1440,"height":1000})
                page.locator("#preview-open").click()
                expect(page.locator("#record-content")).to_contain_text(result()["summary"])
                expect(page.locator("#record-content .source-card")).to_have_count(1)
                page.locator("#close-record").click()
                execution = page.locator("details.execution").first
                execution.locator(":scope > summary").click()
                operation = execution.locator(":scope > details").first
                operation.locator(":scope > summary").click()
                expect(operation.locator(".record-link")).to_be_visible()
                expect(operation.locator(".debug-data")).to_be_hidden()
                page.locator(".record-link").filter(has_text="邮件草稿").last.click()
                expect(page.locator("#draft-body")).to_have_value("合成正文")
                expect(page.locator("#record-content")).to_contain_text("草稿来源（2）")
                expect(page.locator("#record-content button").filter(has_text="邮件 · ")).to_have_text("邮件 · " + mail["subject"])
                expect(page.locator("#record-content button").filter(has_text="网页任务 · ")).to_contain_text("读取公开岗位要求")
                page.locator("#draft-body").fill("手动输入，在切换会话前自动保存")
                page.locator("#new-conversation").click()
                expect(page.locator("#conversation-list button")).to_have_count(2)
                expect(page.locator("#record-panel")).to_be_hidden()
                page.locator("#add-records").click()
                page.locator("#record-kind").select_option("draft")
                page.locator("#record-results input[type=checkbox]").first.check()
                page.locator("#picker-add").click()
                page.locator("#chat-input").fill("把这份草稿简短一点")
                page.locator("#chat-button").click()
                expect(page.locator("#chat-history")).to_contain_text("已修改同一份草稿", timeout=15000)
                page.locator(".record-link").filter(has_text="邮件草稿").last.click()
                expect(page.locator("#draft-body")).to_have_value("合成模型修改后的正文")
                expect(page.locator("#draft-status")).to_contain_text("v3")
                expect(page.locator("#draft-revisions")).to_contain_text("v3 · 助手修改")
                expect(page.locator("#draft-revisions")).to_contain_text("v2 · 手工修改")
                page.locator("#draft-copy").click()
                expect(page.locator("#draft-status")).to_contain_text("v1")
                assert len(app.extensions["store"].list_drafts()) == 2
                page.locator("#draft-send").click()
                expect(page.locator("#draft-status")).to_contain_text("SMTP 已接受")
                expect(page.locator("#draft-send")).to_be_disabled()
                assert len(smtp.messages) == 1
                page.locator("#close-record").click()
                page.locator("#conversation-list button").filter(has_text="分析这封邮件").click()
                page.locator(".record-link").filter(has_text="邮件草稿").last.click()
                expect(page.locator("#draft-body")).to_have_value("合成模型修改后的正文")
                expect(page.locator(".record-link").filter(has_text="邮件草稿").filter(has_text="当时 v1").first).to_be_visible()
                page.locator("#close-record").click()
                page.reload()
                expect(page.locator("#conversation-title")).to_contain_text("分析这封邮件")
                page.locator("#chat-input").fill("尚未发送的输入")
                page.locator("#new-conversation").click()
                page.locator("#conversation-list button").filter(has_text="分析这封邮件").click()
                expect(page.locator("#chat-input")).to_have_value("尚未发送的输入")
                output = Path("data/browser-check"); output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output/"unified-desktop.png"))
                page.set_viewport_size({"width":390,"height":844})
                expect(page.locator("#chat-input")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(output/"unified-mobile.png"))
                page.locator("#open-settings").click()
                page.locator("#assistant-name").fill("合成助手")
                page.locator("#user-nickname").fill("合成昵称")
                page.locator("#identity-form button[type=submit]").click()
                expect(page).to_have_title("合成助手")
                expect(page.locator("#toast")).to_contain_text("称呼已保存")
                page.locator("#rules-text").fill("合成纠正规则")
                page.locator('[data-kind="rules"] button[type="submit"]').click()
                expect(page.locator("#toast")).to_contain_text("已保存")
                rules = page.locator('[data-kind="rules"]')
                rules.get_by_role("button", name="查看修改历史").click()
                rules.locator(".memory-history-list summary").first.click()
                page.once("dialog", lambda dialog: dialog.accept())
                rules.get_by_role("button", name="恢复到变更前").first.click()
                expect(page.locator("#rules-text")).to_have_value("")
                page.locator("#close-settings").click()
                page.reload()
                expect(page).to_have_title("合成助手")
                page.locator("#new-conversation").click()
                expect(page.locator(".welcome h2")).to_contain_text("合成昵称")
                page.locator("#open-settings").click()
                expect(page.locator("#assistant-name")).to_have_value("合成助手")
                expect(page.locator("#user-nickname")).to_have_value("合成昵称")
                page.locator("#close-settings").click()
                assert not errors, errors
                browser.close()
            print("Offline unified UI passed: picker → mail → browser → draft; shared edits, autosave, copy/send, refresh, composer recovery, settings and mobile layout.")
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
            app.extensions["conversations"].close(); app.extensions["mail"].close()
            app.extensions["tasks"].close(); app.extensions["store"].close()


if __name__ == "__main__":
    main()
