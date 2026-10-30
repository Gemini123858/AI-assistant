"""Notification cards and sources, using only temporary synthetic records."""
import tempfile
import unittest
from pathlib import Path

from assistant.config import Config
from assistant.browser import BrowserScope
from assistant.errors import AssistantError
from assistant.notices import Notices
from assistant.records import Records
from assistant.service import Service
from assistant.storage import Store
from .test_storage import notification
from .test_web import analysis_output
from .test_tasks import FixtureBrowser, LIST


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data")
        self.service = Service(self.store, Config(data_dir=self.store.root))
        self.records = Records(self.service, None)
        self.notices = Notices(self.records)
        self.text, _ = self.store.import_message(notification())
        self.ref = {"kind": "text", "id": self.text["id"]}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_analysis_conversion_deduplication_and_source_survives_edits(self):
        analysis = analysis_output()
        self.store.save_result(self.text["id"], analysis=analysis)
        fields = self.notices.from_analysis(self.ref)
        first, created = self.notices.create(fields)
        self.assertTrue(created)
        edited = self.notices.update(first["id"], {"version": 1, "changes": {"deadline": "2027-01-02", "title": "用户修正"}})
        self.assertEqual(edited["content"]["deadline"], "2027-01-02")
        again, created = self.notices.create(fields)
        self.assertFalse(created)
        self.assertEqual(again["content"]["title"], "用户修正")
        self.assertEqual(self.store.get_message(self.text["id"])["analysis"], analysis)
        with self.assertRaises(AssistantError) as caught:
            self.notices.update(first["id"], {"version": 1, "changes": {"title": "过期修改"}})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.records.read({"kind": "notification", "id": self.text["id"]})["ref"], self.ref)

    def test_delete_restore_search_snapshots_and_restart(self):
        record, _ = self.notices.create({"title": "合成活动", "source_refs": [self.ref], "deadline": "2027-02-03"})
        ref = {"kind": "notice", "id": record["id"]}
        snapshot = self.records.read(ref)
        deleted = self.notices.set_deleted(record["id"], {"version": 1}, True)
        self.assertEqual(self.records.search(kind="notice")["total"], 0)
        self.assertEqual(self.records.search(kind="notice", include_deleted=True)["total"], 1)
        self.assertEqual(self.records.read(ref)["status"], "deleted")
        self.assertEqual(snapshot["status"], "active")
        with self.assertRaises(AssistantError):
            self.notices.update(record["id"], {"version": 2, "changes": {"summary": "不能改已删除卡片"}})
        duplicate, created = self.notices.create({"title": "合成活动", "source_refs": [self.ref], "deadline": "2027-02-03"})
        self.assertFalse(created)
        self.assertEqual(duplicate["deleted_at"], deleted["deleted_at"])
        restored = self.notices.set_deleted(record["id"], {"version": 2}, False)
        self.assertEqual(restored["version"], 3)
        self.assertIsNone(restored["deleted_at"])
        self.assertEqual([r["action"] for r in restored["revisions"]], ["create", "delete", "restore"])
        self.store.close(); self.store = Store(self.store.root)
        self.assertEqual(self.store.get_notice(record["id"])["content"]["deadline"], "2027-02-03")
        self.assertEqual(self.store.get_message(self.text["id"])["original"], self.text["original"])

    def test_date_precision_unknown_time_and_validation(self):
        record, _ = self.notices.create({"title": "日期精度", "start_at": "2027-01-02", "deadline": "2027-01-01T18:00:00+08:00"})
        self.assertEqual(record["content"]["start_at"], "2027-01-02")
        self.assertEqual(record["content"]["end_at"], "")
        unchanged = self.notices.update(record["id"], {"version": 1, "changes": {}})
        self.assertEqual(unchanged["version"], 1)
        for stamp in ("2027-02-31", "2027-01-01T18:00:00", "明天下午"):
            with self.subTest(stamp=stamp), self.assertRaises(AssistantError):
                self.notices.update(record["id"], {"version": 1, "changes": {"deadline": stamp}})
        with self.assertRaises(AssistantError):
            self.notices.create({"title": "无效来源", "source_refs": [{**self.ref, "quote": "原文没有这句话"}]})
        self.assertEqual(self.store.get_notice(record["id"])["version"], 1)

    def test_time_filters_and_web_source_quote(self):
        self.notices.create({"title": "过去", "deadline": "2000-01-01"})
        self.notices.create({"title": "未知"})
        self.assertEqual([r["content"]["title"] for r in self.notices.list("expired")], ["过去"])
        self.assertEqual([r["content"]["title"] for r in self.notices.list("undated")], ["未知"])
        self.assertEqual(self.notices.list(query="未知")[0]["content"]["deadline"], "")
        task = self.store.create_task("合成网页查询", "public_web", "api", self.store.memory(),
            {"calls": 2, "seconds": 60, "steps": 2, "pages": 2, "details": 2},
            BrowserScope(LIST, public_web=True).as_dict(), "合成公开查询")
        page = {**FixtureBrowser(lambda: None).read(LIST), "id": "p1"}
        self.store.update_task(task["id"], status="completed", pages=[page], result={"findings": [
            {"page_id": "p1", "text": "合成结论", "quote": page["text"], "uncertainties": []}]})
        fields = self.notices.from_analysis({"kind": "task", "id": task["id"]}, 0, "findings")
        card, _ = self.notices.create(fields)
        source = card["content"]["source_refs"][0]
        self.assertEqual(source["url"], LIST)
        self.assertEqual(source["quote"], page["text"])
