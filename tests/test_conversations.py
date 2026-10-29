"""Unified conversation flows, with synthetic model, mailbox, browser and SMTP only."""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from assistant.config import Config
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
        self.assertIsNone(self.app.extensions["service"].followups()["task"])
        self.assertEqual(self.smtp.messages, [])

    def test_tool_error_is_saved_and_assistant_can_continue(self):
        key = self.new()
        self.model.outputs = [call("update_draft", {"id": "0"*32, "version": 1, **fields()}), finish("没有找到草稿，请重新选择。")]
        record = self.turn(key, "修改不存在的草稿")
        self.assertEqual(record["turns"][-1]["status"], "completed")
        self.assertTrue(any(event["type"] == "tool_result" and event["output"].get("failed") for event in record["events"]))

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
