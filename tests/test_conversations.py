"""Unified conversation flows, with synthetic model, mailbox, browser and SMTP only."""
import hashlib
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.config import Config
from assistant.conversations import SYSTEM, TOOLS
from assistant.llm import APIModel
from assistant.web import create_app
from .test_drafts import FakeSMTP
from .test_generic_tasks import result
from .test_mail import FakeIMAP, TOKEN, analysis
from .test_tasks import FixtureBrowser, LIST, ScriptModel, call
from .test_storage import notification
from .test_web import analysis_output


def fields(**changes):
    return {"to": ["recipient@example.com"], "cc": [], "bcc": [], "subject": "合成咨询", "body": "合成正文", **changes}


def finish(reply="已完成", refs=None):
    return call("finish_turn", {"reply": reply, "record_refs": refs or []})


def main_background(messages):
    return json.loads(next(line for line in messages[0]["content"].splitlines() if line.startswith("{")))


class FlowModel(ScriptModel):
    def complete_tools(self, messages, tools, timeout=None):
        output = self.outputs[0]
        if callable(output):
            self.outputs[0] = output(messages)
        return super().complete_tools(messages, tools, timeout)


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name)/"data", mode="api", api_key="synthetic",
            qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic", access_token=TOKEN,
            daily_calls=100, mail_poll_seconds=3600)
        self.model, self.imap, self.smtp = FlowModel([]), FakeIMAP(), FakeSMTP()
        self.app = self.open_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True
        self.headers = {"X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', self.client.get("/").text).group(1)}
        self.store = self.app.extensions["store"]
        self.controller = self.app.extensions["conversations"]

    def open_app(self):
        return create_app(self.config, self.model, FixtureBrowser, lambda: self.imap, lambda: self.smtp)

    def close_app(self):
        self.app.extensions["conversations"].close()
        self.app.extensions["mail"].close()
        self.app.extensions["tasks"].close()
        self.app.extensions["store"].close()

    def tearDown(self):
        self.close_app()
        self.temp.cleanup()

    def new(self):
        response = self.client.post("/api/conversations", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        return response.json["conversation"]["id"]

    def turn(self, key, message, refs=None):
        response = self.client.post(f"/api/conversations/{key}/turns", json={"message": message, "records": refs or []}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        self.controller.wait(key, response.json["turn_id"])
        record = self.store.get_conversation(key)
        self.assertNotEqual(record["turns"][-1]["status"], "running")
        return record

    def test_full_history_and_separate_conversations(self):
        first, second = self.new(), self.new()
        self.model.outputs = [finish(f"回答{i}") for i in range(9)]
        for index in range(8):
            record = self.turn(first, f"问题{index}")
            self.assertEqual(record["turns"][-1]["status"], "completed")
        self.turn(second, "独立问题")
        serialized = json.dumps(self.model.calls[-2], ensure_ascii=False)
        self.assertIn("问题0", serialized)
        self.assertIn("回答0", serialized)
        self.assertNotIn("问题0", json.dumps(self.model.calls[-1], ensure_ascii=False))
        self.assertEqual(len(self.controller.list()), 2)

    def test_system_prompt_reaches_serialized_api_requests_after_tool_execution(self):
        api = APIModel(self.config)
        self.app.extensions["service"].model = api
        self.config.turn_calls = 2
        outputs = [call("create_draft", {**fields(to=["owner@qq.com"]), "source_refs": []}), finish()]
        responses = [io.BytesIO(json.dumps({"choices": [{"finish_reason": "tool_calls", "message": item}]}).encode())
                     for item in outputs]
        with patch.object(api.opener, "open", side_effect=responses) as opened:
            record = self.turn(self.new(), "给自己起草一封合成测试邮件，先不要发送")
        self.assertEqual(record["turns"][-1]["status"], "completed")
        self.assertEqual(opened.call_count, 2)
        for request, expected_tools in zip(opened.call_args_list, (TOOLS, [TOOLS[-1]])):
            body = json.loads(request.args[0].data)
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertTrue(body["messages"][0]["content"].startswith(SYSTEM + "\n"))
            self.assertEqual(main_background(body["messages"])["mail_account"]["address"], "owner@qq.com")
            self.assertEqual(body["tools"], expected_tools)
            self.assertEqual(body["tool_choice"], "required")
        versions = [event["system_template_sha256"] for event in record["events"] if event["type"] == "model"]
        self.assertEqual(versions, [hashlib.sha256(SYSTEM.encode()).hexdigest()] * 2)
        self.assertEqual(self.smtp.messages, [])

    def test_shared_draft_manual_edit_snapshot_send_and_conversation_delete(self):
        first, second = self.new(), self.new()
        self.model.outputs = [call("create_draft", {**fields(), "source_refs": []}), finish()]
        record = self.turn(first, "写一封邮件")
        created = next(event["output"]["records"][0] for event in record["events"] if event["type"] == "tool_result" and event["output"].get("records"))
        draft_id = created["ref"]["id"]
        ref = {"kind": "draft", "id": draft_id}
        self.model.outputs = [finish()]
        self.turn(second, "看看这封草稿", [ref])
        manual_headers = {**self.headers, "X-Conversation-ID": second}
        response = self.client.put(f"/api/drafts/{draft_id}", json={"version": 1, **fields(body="手工修改 v2")}, headers=manual_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["draft"]["revisions"][-1]["reason"], "manual")
        self.model.outputs = [call("read_record", {"ref": ref}), finish()]
        record = self.turn(first, "再看一下最新内容")
        self.assertEqual(created["data"]["body"], "合成正文")
        self.assertEqual(created["ref"]["version"], 1)
        self.assertTrue(any(event["type"] == "record_update" and event["records"][0]["data"]["body"] == "手工修改 v2" for event in record["events"]))
        self.assertTrue(any(event["type"] == "user_action" for event in self.store.get_conversation(second)["events"]))
        sent = self.client.post(f"/api/drafts/{draft_id}/send", json={"version": 2}, headers=manual_headers)
        self.assertEqual(sent.json["draft"]["status"], "sent")
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertEqual(self.client.delete(f"/api/conversations/{first}", headers=self.headers).status_code, 200)
        self.assertEqual(self.store.get_draft(draft_id)["status"], "sent")

    def test_mail_browser_draft_chain_and_private_context_boundary(self):
        self.imap.uids = [10]
        mail = self.app.extensions["mail"].read_latest()
        mail_ref = {"kind": "mail", "id": mail["id"]}
        key = self.new()
        def draft_after_browser(messages):
            snapshots = []
            for message in messages:
                if message["role"] == "tool":
                    snapshots.extend(json.loads(message["content"]).get("records", []))
            refs = [item["ref"] for item in snapshots if item["ref"]["kind"] in {"task", "mail"}]
            return call("create_draft", {**fields(), "source_refs": refs})
        self.model.outputs = [call("read_record", {"ref": mail_ref}),
            call("save_mail_analysis", {"id": mail["id"], "analysis": analysis()}),
            call("run_browser", {"goal": "查询公开项目要求", "start_url": LIST}),
            call("finish_task", result()), draft_after_browser, finish("官网已查阅，草稿已保存。")]
        record = self.turn(key, "PRIVATE_USER_PROMPT：分析邮件、查官网并起草邮件", [mail_ref])
        self.assertEqual(record["turns"][-1]["status"], "completed", record["turns"][-1])
        task = self.store.list_tasks()[0]
        draft = self.store.list_drafts()[0]
        self.assertEqual(task["origin"]["conversation_id"], key)
        self.assertEqual({ref["kind"] for ref in draft["source_refs"]}, {"mail", "task"})
        self.assertEqual(record["turns"][-1]["calls"], 6)
        browser_context = json.dumps(self.model.calls[3], ensure_ascii=False)
        self.assertNotIn("PRIVATE_USER_PROMPT", browser_context)
        self.assertNotIn("本人：我正在找实习", browser_context)
        self.assertNotIn("mail_account", browser_context)
        self.assertNotIn(self.config.qq_imap_user, browser_context)
        for index in (0, 1, 2, 4, 5):
            background = main_background(self.model.calls[index])
            self.assertEqual(background["mail_account"], {"address": "owner@qq.com"})
            self.assertNotIn(self.config.qq_imap_auth_code, json.dumps(background))
        self.assertIsNone(self.app.extensions["service"].followups()["task"])
        self.assertEqual(self.smtp.messages, [])

    def test_draft_to_self_uses_account_background_and_missing_account_is_omitted(self):
        def draft_to_self(messages):
            background = main_background(messages)
            return call("create_draft", {**fields(to=[background["mail_account"]["address"]]), "source_refs": []})
        self.model.outputs = [draft_to_self, finish()]
        key = self.new()
        record = self.turn(key, "给我自己起草一封测试邮件，先不要发送")
        self.assertEqual(record["turns"][-1]["status"], "completed")
        self.assertEqual(self.store.list_drafts()[0]["to"], ["owner@qq.com"])
        self.assertEqual(self.smtp.messages, [])
        self.config.qq_imap_user = ""
        background = main_background(self.controller.context(key))
        self.assertNotIn("mail_account", background)

    def test_assistant_updates_draft_and_adds_used_sources(self):
        original, _ = self.store.import_message(notification())
        source = {"kind": "notification", "id": original["id"]}
        draft = self.app.extensions["drafts"].create(fields())
        self.model.outputs = [call("update_draft", {"id": draft["id"], "version": 1,
            **fields(body="根据通知补充正文"), "source_refs": [source]}), finish()]
        record = self.turn(self.new(), "参考这条通知修改草稿", [source, {"kind": "draft", "id": draft["id"]}])
        self.assertEqual(record["turns"][-1]["status"], "completed")
        saved = self.store.get_draft(draft["id"])
        self.assertEqual(saved["source_refs"], [source])
        self.assertEqual(saved["revisions"][-1]["reason"], "assistant")
        self.assertEqual(saved["body"], "根据通知补充正文")

    def test_tool_error_is_saved_and_assistant_can_continue(self):
        key = self.new()
        self.model.outputs = [call("update_draft", {"id": "0"*32, "version": 1, **fields()}), finish("没有找到草稿，请重新选择。")]
        record = self.turn(key, "修改不存在的草稿")
        self.assertEqual(record["turns"][-1]["status"], "completed")
        self.assertTrue(any(event["type"] == "tool_result" and event["output"].get("failed") for event in record["events"]))

    def test_missing_mail_quote_can_be_corrected_using_numbered_lines(self):
        self.imap.uids = [10]
        mail = self.app.extensions["mail"].read_latest()
        ref = {"kind": "mail", "id": mail["id"]}
        incomplete = analysis()
        incomplete["events"][0].pop("evidence")

        def correct_quote(messages):
            error = json.loads(messages[-1]["content"])
            self.assertTrue(error["failed"])
            self.assertIn("缺少来源引用", error["error"])
            self.assertIn("line_start", error["error"])
            self.assertIsNone(self.app.extensions["mail"].get(mail["id"])["analysis"])
            read_output = next(json.loads(message["content"]) for message in messages
                if message["role"] == "tool" and json.loads(message["content"]).get("records"))
            lines = read_output["records"][0]["data"]["numbered_lines"]
            self.assertEqual(lines[1], {"line": 2, "text": "2026年9月25日18:00前提交申请。"})
            corrected = analysis()
            corrected["events"][0].pop("evidence")
            corrected["events"][0].update(line_start=lines[1]["line"], line_end=lines[1]["line"])
            return call("save_mail_analysis", {"id": mail["id"], "analysis": corrected})

        self.model.outputs = [call("read_record", {"ref": ref}),
            call("save_mail_analysis", {"id": mail["id"], "analysis": incomplete}), correct_quote, finish()]
        record = self.turn(self.new(), "分析这封邮件", [ref])
        self.assertEqual(record["turns"][-1]["status"], "completed")
        self.assertEqual(sum(bool(e.get("output", {}).get("failed")) for e in record["events"]), 1)
        saved = self.app.extensions["mail"].get(mail["id"])["analysis"]
        self.assertEqual(saved["events"][0]["evidence"], "2026年9月25日18:00前提交申请。")

    def test_notification_search_analysis_and_explicit_memory(self):
        key = self.new()
        original = notification()
        def analyze_saved(messages):
            output = json.loads(messages[-1]["content"])
            return call("save_notification_analysis", {"id": output["records"][0]["ref"]["id"], "analysis": analysis_output()})
        self.model.outputs = [call("search_records", {"kind": "notification", "query": "报名"}),
            call("save_notification", {name: original[name] for name in ("text", "conversation", "sent_at", "sender")}),
            analyze_saved, call("update_memory", {"kind": "public_profile", "content": "正在找实习", "append": True,
                "source_quote": "记住我正在找实习"}), finish()]
        record = self.turn(key, "保存并分析这条通知；记住我正在找实习")
        self.assertEqual(record["turns"][-1]["status"], "completed")
        self.assertEqual(self.store.list_messages()[0]["analysis"]["summary"], analysis_output()["summary"])
        self.assertIn("正在找实习", self.store.memory()["public_profile"])
        history = list((self.store.root/"memory"/"history").glob("*.json"))
        self.assertEqual(json.loads(history[-1].read_text())["source"]["conversation_id"], key)
        revisions = self.client.get("/api/memory/public_profile/history").json["revisions"]
        self.assertEqual(revisions[0]["before"], "")
        self.assertEqual(self.client.put("/api/memory/public_profile", json={"content": revisions[0]["before"]},
            headers={**self.headers, "X-Conversation-ID": key}).status_code, 200)
        self.assertEqual(self.store.memory()["public_profile"], "")

    def test_header_triage_can_be_saved_without_reading_body(self):
        self.imap.uids = [10]
        mail = self.app.extensions["mail"].read_latest()
        with self.app.extensions["mail"].lock:
            mail["text"] = None
            self.app.extensions["mail"].save(mail)
        self.model.outputs = [call("save_mail_triage", {"decisions": [{"id": mail["id"], "decision": "review", "reason": "需细读"}]}), finish()]
        self.turn(self.new(), "只看邮件标题做初筛")
        saved = self.app.extensions["mail"].get(mail["id"])
        self.assertIsNone(saved["text"])
        self.assertEqual(saved["triage"]["decision"], "review")
        summary = self.client.get(f'/api/records/mail/{mail["id"]}?summary=1')
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(set(summary.json["record"]), {"ref", "title", "status", "updated_at"})
        self.assertIsNone(self.app.extensions["mail"].get(mail["id"])["text"])

    def test_cancel_closes_pending_tool_call_without_writing(self):
        key = self.new()
        entered, release = threading.Event(), threading.Event()
        def slow(messages):
            entered.set(); release.wait(5)
            return call("create_draft", {**fields(), "source_refs": []})
        self.model.outputs = [slow]
        response = self.client.post(f"/api/conversations/{key}/turns", json={"message": "写邮件"}, headers=self.headers)
        turn_id = response.json["turn_id"]
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.client.delete(f"/api/conversations/{key}", headers=self.headers).status_code, 409)
        self.client.post(f"/api/conversations/{key}/turns/{turn_id}/cancel", json={}, headers=self.headers)
        release.set(); self.controller.wait(key, turn_id)
        record = self.store.get_conversation(key)
        self.assertEqual(record["turns"][-1]["status"], "cancelled")
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(record["events"][-2]["type"], "tool_result")

    def test_restart_marks_interrupted_and_preserves_history(self):
        key = self.new()
        def interrupted(record):
            record["turns"].append({"id": "synthetic-turn", "status": "running", "cancel_requested": False})
            record["events"].append({"seq": 1, "type": "user", "text": "保存的要求", "records": []})
        self.store.change_conversation(key, interrupted)
        self.close_app(); self.app = self.open_app(); self.store = self.app.extensions["store"]
        record = self.store.get_conversation(key)
        self.assertEqual(record["turns"][-1]["status"], "interrupted")
        self.assertEqual(record["events"][0]["text"], "保存的要求")

    def test_legacy_chat_import_once_and_original_kept(self):
        self.store.add_chat("旧问题", "旧回答", [])
        self.close_app(); self.app = self.open_app(); self.store = self.app.extensions["store"]
        imported = self.store.list_conversations()[0]
        self.assertEqual(imported["title"], "历史对话")
        self.assertTrue((self.config.data_dir / "chat.json").exists())
        self.store.delete_conversation(imported["id"])
        self.close_app(); self.app = self.open_app(); self.store = self.app.extensions["store"]
        self.assertEqual(self.store.list_conversations(), [])

    def test_context_limit_keeps_user_message_and_does_not_call_api(self):
        key = self.new(); self.config.context_chars = 10
        record = self.turn(key, "需要完整保存的问题")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(record["events"][0]["text"], "需要完整保存的问题")
        self.assertIn("未裁剪历史", record["turns"][-1]["note"])
