"""Synthetic draft/SMTP tests: never connect to a real account or model."""

import copy
import json
import smtplib
import tempfile
import unittest
from pathlib import Path

from assistant.config import Config
from assistant.drafts import Drafts
from assistant.errors import AssistantError
from assistant.service import Service
from assistant.storage import Store
from .test_storage import notification


class DraftModel:
    def __init__(self):
        self.calls = []
        self.output = {"to": ["老师 <teacher@example.com>"], "cc": [], "bcc": [],
                       "subject": "关于 delete/create 文档的讨论",
                       "body": "正文保留模型自由生成的内容，包括 password 这个普通词。",
                       "reply": "已生成草稿，请检查收件人。"}

    def complete(self, messages):
        self.calls.append(copy.deepcopy(messages))
        return copy.deepcopy(self.output)


class FakeSMTP:
    def __init__(self, error=None, refused=None):
        self.error, self.refused = error, refused or {}
        self.logins, self.messages, self.closed = [], [], False

    def login(self, user, code):
        self.logins.append((user, code))

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.messages.append((copy.deepcopy(message), from_addr, list(to_addrs)))
        if self.error:
            raise self.error
        return self.refused

    def quit(self):
        self.closed = True


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "data"
        self.config = Config(data_dir=root, mode="api", api_key="synthetic",
                             qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic-code",
                             access_token="synthetic-access-token-long-enough")
        self.config.validate()
        self.store = Store(root)
        self.model = DraftModel()
        self.service = Service(self.store, self.config, self.model)
        self.smtp = FakeSMTP()
        self.drafts = Drafts(self.service, smtp_connector=lambda: self.smtp)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def create(self):
        return self.drafts.create({"to": [], "cc": [], "bcc": [], "subject": "", "body": "",
                                   "source": {"kind": "manual", "keys": []}})

    def test_assistant_populates_editable_fields_without_keyword_filtering(self):
        draft = self.create()
        saved = self.drafts.assist(draft["id"], {"version": draft["version"],
                                                  "instruction": "帮我写邮件"})
        self.assertEqual(saved["to"], ["老师 <teacher@example.com>"])
        self.assertIn("delete/create", saved["subject"])
        self.assertIn("password", saved["body"])
        self.assertEqual(saved["turns"][0]["user"], "帮我写邮件")
        data = json.loads(self.model.calls[0][-1]["content"])
        self.assertEqual(data["source"], {"kind": "manual"})

    def test_manual_edit_uses_optimistic_version_and_rejects_header_injection(self):
        draft = self.create()
        payload = {"version": 1, "to": ["person@example.com"], "cc": [], "bcc": [],
                   "subject": "用户自由填写", "body": "\n正文末尾保留空行\n\n"}
        saved = self.drafts.update(draft["id"], payload)
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["body"], "\n正文末尾保留空行\n\n")
        unchanged = self.drafts.update(draft["id"], {**payload, "version": 2})
        self.assertEqual(unchanged["version"], 2)
        self.assertEqual(len(unchanged["revisions"]), 2)
        with self.assertRaises(AssistantError):
            self.drafts.update(draft["id"], payload)
        payload.update(version=2, subject="主题\nBcc: victim@example.com")
        with self.assertRaises(AssistantError):
            self.drafts.update(draft["id"], payload)

    def test_restore_creates_new_current_revision_without_deleting_history(self):
        draft = self.create()
        first = self.drafts.update(draft["id"], {"version": 1, "to": ["one@example.com"],
            "cc": [], "bcc": [], "subject": "第一版", "body": "第一版正文"})
        second = self.drafts.update(draft["id"], {"version": first["version"], "to": ["two@example.com"],
            "cc": [], "bcc": [], "subject": "第二版", "body": "第二版正文"})
        restored = self.drafts.restore(draft["id"],
            {"version": second["version"], "target_version": first["version"]})
        self.assertEqual(restored["version"], 4)
        self.assertEqual((restored["subject"], restored["body"]), ("第一版", "第一版正文"))
        self.assertEqual([item["version"] for item in restored["revisions"]], [1, 2, 3, 4])
        self.assertEqual(restored["revisions"][-1]["reason"], "restore")
        self.assertEqual(restored["revisions"][-1]["restored_from"], 2)

    def test_sources_merge_version_restore_and_legacy_compatibility(self):
        first, _ = self.store.import_message(notification())
        second, _ = self.store.import_message(notification(message_id="second", text="另一条合成通知"))
        refs = [{"kind": "notification", "id": item["id"]} for item in (first, second)]
        draft = self.drafts.create({"source": {"kind": "notifications", "keys": [first["id"]]}})
        # Older files stored legacy keys but left the new source_refs field empty.
        self.assertEqual(draft["revisions"][0]["source_refs"], refs[:1])
        payload = {name: draft[name] for name in ("to", "cc", "bcc", "subject", "body")}
        saved = self.drafts.update(draft["id"], {**payload, "version": 1, "source_refs": [refs[1], refs[1]]})
        self.assertEqual(saved["source_refs"], refs)
        self.assertEqual(saved["version"], 2)
        unchanged = self.drafts.update(draft["id"], {**payload, "version": 2, "source_refs": refs})
        self.assertEqual(unchanged["version"], 2)
        manual = self.drafts.update(draft["id"], {**payload, "version": 2, "body": "手改正文"})
        self.assertEqual(manual["source_refs"], refs)
        restored = self.drafts.restore(draft["id"], {"version": 3, "target_version": 1})
        self.assertEqual(restored["source_refs"], refs[:1])
        # Existing revisions without source snapshots keep current references on restore.
        legacy = self.store.get_draft(draft["id"])
        legacy["revisions"][1].pop("source_refs")
        self.store._write_json(self.store._draft_path(draft["id"]), legacy)
        restored = self.drafts.restore(draft["id"], {"version": 4, "target_version": 2})
        self.assertEqual(restored["source_refs"], refs[:1])

    def test_delete_requires_current_editable_draft_and_removes_all_history(self):
        draft = self.create()
        updated = self.drafts.update(draft["id"], {"version": 1, "to": ["person@example.com"],
            "cc": [], "bcc": [], "subject": "准备删除", "body": "第二个版本"})
        with self.assertRaises(AssistantError):
            self.drafts.delete(draft["id"], {"version": 1})
        result = self.drafts.delete(draft["id"], {"version": updated["version"]})
        self.assertEqual(result, {"id": draft["id"], "deleted": True})
        with self.assertRaises(AssistantError) as missing:
            self.store.get_draft(draft["id"])
        self.assertEqual(missing.exception.status, 404)

        delivery = self.create()
        delivery = self.drafts.update(delivery["id"], {"version": 1,
            "to": ["person@example.com"], "cc": [], "bcc": [], "subject": "发送记录", "body": "保留"})
        delivery = self.drafts.send(delivery["id"], delivery["version"])
        with self.assertRaises(AssistantError) as protected:
            self.drafts.delete(delivery["id"], {"version": delivery["version"]})
        self.assertEqual(protected.exception.status, 409)
        self.assertEqual(self.store.get_draft(delivery["id"])["status"], "sent")

    def test_legacy_draft_gets_single_compatible_revision_then_upgrades_on_edit(self):
        draft = self.create()
        legacy = copy.deepcopy(draft)
        legacy["schema_version"] = 1
        legacy.pop("revisions")
        self.store._write_json(self.store._draft_path(draft["id"]), legacy)
        loaded = self.store.get_draft(draft["id"])
        self.assertEqual(loaded["revisions"][0]["reason"], "legacy")
        upgraded = self.drafts.update(draft["id"], {"version": 1, "to": [], "cc": [], "bcc": [],
            "subject": "升级后修改", "body": ""})
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertEqual([item["reason"] for item in upgraded["revisions"]], ["legacy", "manual"])

    def test_user_send_uses_current_version_and_cannot_repeat(self):
        draft = self.create()
        draft = self.drafts.update(draft["id"], {"version": 1,
            "to": ["Person <person@example.com>"], "cc": [], "bcc": ["audit@example.com"],
            "subject": "测试主题", "body": "测试正文"})
        sent = self.drafts.send(draft["id"], draft["version"])
        self.assertEqual(sent["status"], "sent")
        message, sender, recipients = self.smtp.messages[0]
        self.assertEqual(sender, "owner@qq.com")
        self.assertEqual(recipients, ["person@example.com", "audit@example.com"])
        self.assertIsNone(message.get("Bcc"))
        self.assertEqual(message.get_content().strip(), "测试正文")
        with self.assertRaises(AssistantError):
            self.drafts.send(draft["id"], draft["version"])
        self.assertEqual(len(self.smtp.messages), 1)

    def test_disconnect_during_submission_is_unknown_and_not_retried(self):
        self.smtp.error = smtplib.SMTPServerDisconnected("synthetic disconnect")
        draft = self.create()
        draft = self.drafts.update(draft["id"], {"version": 1,
            "to": ["person@example.com"], "cc": [], "bcc": [], "subject": "主题", "body": "正文"})
        result = self.drafts.send(draft["id"], draft["version"])
        self.assertEqual(result["status"], "unknown")
        with self.assertRaises(AssistantError):
            self.drafts.send(draft["id"], draft["version"])

    def test_send_requires_recipient_but_empty_local_draft_is_allowed(self):
        draft = self.create()
        self.assertEqual(draft["to"], [])
        with self.assertRaises(AssistantError):
            self.drafts.send(draft["id"], draft["version"])
        self.assertEqual(self.smtp.messages, [])


if __name__ == "__main__":
    unittest.main()
