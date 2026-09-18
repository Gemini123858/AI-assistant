"""Small synthetic IMAP/model regression: never use an account or cloud API."""
import copy
import re
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.mail import Mail, parse_text
from assistant.service import Service
from assistant.storage import Store
from assistant.web import create_app
from .test_tasks import ScriptModel, call


TOKEN = "synthetic-access-token-not-a-secret-123"


def raw_mail(text="本人：我正在找实习。\n2026年9月25日18:00前提交申请。", sender="owner@qq.com", subject="聊天记录"):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, "owner@qq.com", subject
    message["Date"] = "Fri, 18 Sep 2026 10:00:00 +0800"
    message.set_content(text)
    return message.as_bytes()


class FakeIMAP:
    def __init__(self):
        self.validity, self.next_uid = 123, 11
        self.uids, self.commands, self.mail = [], [], raw_mail()

    def login(self, user, code):
        return "OK", []

    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"10"]

    def response(self, label):
        return label, [str(self.validity if label == "UIDVALIDITY" else self.next_uid).encode()]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(n).encode() for n in self.uids)]
        if "HEADER.FIELDS" in args[-1]:
            data = self.mail.split(b"\n\n")[0] + b"\n\n"
        else:
            data = self.mail
        return "OK", [(b"11 (BODY[] {100})", data), b")"]

    def logout(self):
        self.commands.append(("logout",))
        return "BYE", []


def analysis():
    return {"summary": "申请截止通知，需确认是否参加。", "events": [{"title": "提交申请", "relevance": "可能与实习有关，需确认。",
        "importance": "review", "time_kind": "deadline", "time_text": "2026年9月25日18:00", "normalized": "2026-09-25T18:00:00+08:00",
        "action": "确认后准备申请", "evidence": "2026年9月25日18:00前提交申请。", "uncertainties": ["未提供申请目标"]}],
        "uncertainties": ["聊天记录身份需核对"], "link_requests": []}


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name)/"data", mode="api", api_key="synthetic",
                             qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic", access_token=TOKEN)
        self.config.validate()
        self.store = Store(self.config.data_dir)
        self.model = ScriptModel([])
        self.service = Service(self.store, self.config, self.model)
        self.imap = FakeIMAP()
        self.mail = Mail(self.service, lambda: self.imap)

    def tearDown(self):
        self.mail.close()
        self.store.close()
        self.temp.cleanup()

    def new_mail(self):
        self.mail.sync()
        self.imap.uids = [10, 11]
        self.mail.sync()
        return self.mail.records()[0]["id"]

    def test_baseline_skips_history_new_headers_deduplicate_and_body_is_peek(self):
        self.assertTrue(self.mail.sync()["baseline_created"])
        self.assertFalse(any(c[0] == "FETCH" for c in self.imap.commands))
        self.imap.uids = [10, 11]
        self.assertEqual(self.mail.sync()["added"], 1)
        self.assertEqual(self.mail.sync()["added"], 0)
        record = self.mail.records()[0]
        self.assertIsNone(record["text"])
        self.assertIn("我正在找实习", self.mail.read_body(record["id"])["text"])
        self.assertTrue(all(c[2] for c in self.imap.commands if c[0] == "select"))
        self.assertTrue(all("BODY.PEEK" in c[-1] for c in self.imap.commands if c[0] == "FETCH"))
        self.assertEqual(self.model.calls, [])

    def test_changed_uidvalidity_stops_without_rebaselining(self):
        self.mail.sync()
        self.imap.validity += 1
        with self.assertRaises(AssistantError):
            self.mail.sync()
        self.assertEqual(self.mail.state()["uidvalidity"], 123)
        self.assertEqual(self.mail.records(), [])

    def test_explicit_latest_import_does_not_read_other_bodies_or_move_baseline(self):
        self.mail.sync()
        baseline = self.mail.state()
        self.imap.uids = [3, 7, 10]
        record = self.mail.read_latest()
        self.assertEqual(record["uid"], 10)
        self.assertIn("我正在找实习", record["text"])
        self.assertEqual(self.mail.state(), baseline)
        self.assertEqual(len(self.mail.records()), 1)
        fetched = [c for c in self.imap.commands if c[0] == "FETCH"]
        self.assertTrue(all(c[1] == "10" and "BODY.PEEK" in c[-1] for c in fetched))
        self.assertEqual(self.model.calls, [])

    def test_header_triage_neither_reads_body_nor_exports_credentials(self):
        import json
        key = self.new_mail()
        self.config.qq_imap_auth_code = "SYNTHETIC_MAIL_SECRET_SENTINEL"
        inputs = []
        def classify(messages):
            inputs.append(json.dumps(messages, ensure_ascii=False))
            return {"decisions": [{"id": key, "decision": "review", "reason": "需阅读文字确认"}]}
        self.model.complete = classify
        self.assertEqual(self.mail.triage()["reviewed"], 1)
        self.assertIsNone(self.mail.get(key)["text"])
        self.assertNotIn(self.config.qq_imap_auth_code, inputs[0])
        self.assertEqual(self.mail.get(key)["triage"]["decision"], "review")

    def test_remaining_daily_budget_and_numbered_event_quote(self):
        key = self.new_mail()
        self.config.daily_calls = 2
        self.store.reserve_call(self.service.day(), 2)
        output = analysis()
        output["events"][0].pop("evidence")
        output["events"][0].update(line_start=2, line_end=2)
        self.model.outputs = [call("save_mail_analysis", output)]
        saved = self.mail.analyze(key)
        self.assertEqual(saved["analysis"]["events"][0]["evidence"], "2026年9月25日18:00前提交申请。")
        self.assertEqual([t["function"]["name"] for t in self.model.tools[0]], ["save_mail_analysis"])
        self.assertEqual(self.store.usage(self.service.day())["calls"], 2)

    def test_model_event_table_and_identity_guard(self):
        key = self.new_mail()
        update = {"text": "正在找实习", "evidence": "本人：我正在找实习。", "speaker": "本人", "kind": "fact"}
        self.model.outputs = [call("update_profile", update), call("save_mail_analysis", analysis())]
        saved = self.mail.analyze(key)
        self.assertEqual(saved["analysis"]["events"][0]["time_kind"], "deadline")
        self.assertFalse(saved["profile_updates"][0]["applied"])
        self.assertEqual(self.store.memory()["profile"], "")
        self.mail.confirm_profile(key, 0)
        self.assertIn("用户确认", self.store.memory()["profile"])
        self.assertEqual(self.store.usage(self.service.day())["calls"], 2)

    def test_configured_speaker_auto_update_is_traced_and_idempotent(self):
        self.config.chat_self_names = "本人"
        key = self.new_mail()
        record = self.mail.read_body(key)
        update = {"text": "正在找实习", "evidence": "本人：我正在找实习。", "speaker": "本人", "kind": "fact"}
        self.assertTrue(self.mail.update_profile(record, update)["applied"])
        self.assertTrue(self.mail.update_profile(record, update)["duplicate"])
        self.assertEqual(len(list((self.store.root/"memory/history").glob("*.json"))), 1)
        self.assertIn(key, self.store.memory()["profile"])

    def test_fabricated_event_and_naive_time_rejected(self):
        original = "本人：我正在找实习。\r\n2026年9月25日18:00前提交申请。\r\n"
        quote = {"line_start": 1, "line_end": 2}
        self.assertEqual(Mail.quote({"text": original}, quote), original[:-2])
        record = {"text": "2026年9月25日18:00前提交申请。"}
        output = analysis()
        output["events"][0]["evidence"] = "不存在的通知"
        with self.assertRaises(AssistantError):
            Mail.validate_analysis(record, output)
        output = analysis()
        output["events"][0]["normalized"] = "2026-09-25T18:00:00"
        with self.assertRaises(AssistantError):
            Mail.validate_analysis(record, output)

    def test_html_stays_plain_and_keeps_links_without_loading_images(self):
        message = EmailMessage()
        message.set_content('<p>通知<a href="https://docs.python.org/">详情</a><img src="https://tracker.example/a"></p><script>秘密</script>', subtype="html")
        text, attachments, truncated = parse_text(message.as_bytes())
        self.assertIn("https://docs.python.org/", text)
        self.assertNotIn("tracker.example", text)
        self.assertNotIn("秘密", text)
        self.assertEqual((attachments, truncated), ([], False))

    def test_account_secrets_hidden_and_auth_required(self):
        self.assertNotIn("qq_imap_auth_code=", repr(self.config))
        invalid = copy.copy(self.config)
        invalid.access_token = ""
        with self.assertRaises(AssistantError):
            invalid.validate()


class MailAuthTests(unittest.TestCase):
    def test_private_apis_require_login_and_csrf_and_logout_revokes_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Config(data_dir=Path(temporary)/"data", access_token=TOKEN))
            try:
                client = app.test_client()
                self.assertEqual(client.get("/api/memory").status_code, 401)
                self.assertEqual(client.get("/").status_code, 302)
                page = client.get("/login").get_data(as_text=True)
                csrf = re.search('name="csrf_token" value="([^"]+)"', page).group(1)
                self.assertNotIn(TOKEN, page)
                self.assertEqual(client.post("/login", data={"token": TOKEN}).status_code, 403)
                self.assertEqual(client.post("/login", data={"token": TOKEN, "csrf_token": csrf}).status_code, 302)
                self.assertEqual(client.post("/api/mail/latest", json={}, headers={"X-CSRF-Token": csrf}).status_code, 400)
                self.assertEqual(client.get("/api/mail").status_code, 200)
                client.post("/api/logout", json={}, headers={"X-CSRF-Token": csrf})
                self.assertEqual(client.get("/api/memory").status_code, 401)
            finally:
                app.extensions["mail"].close()
                app.extensions["tasks"].close()
                app.extensions["store"].close()
