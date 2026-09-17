import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.errors import AssistantError
from assistant.storage import Store, normalize_message


def notification(**changes):
    return {"source": "qq", "conversation": "合成课程群", "sender": "合成助教",
            "message_id": "synthetic-001", "sent_at": "2026-09-16T10:00:00+08:00",
            "text": "请于9月20日18:00前报名，活动于9月23日14:00开始。", **changes}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_import_is_idempotent_and_survives_restart(self):
        record, created = self.store.import_message(notification())
        self.assertTrue(created)
        duplicate, created = self.store.import_message(notification())
        self.assertFalse(created)
        self.assertEqual(record, duplicate)
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(len(self.store.list_messages()), 1)
        self.assertEqual(self.store.get_message(record["id"])["original"]["text"], notification()["text"])

    def test_same_identity_different_content_is_not_overwritten(self):
        record, _ = self.store.import_message(notification())
        with self.assertRaises(AssistantError) as caught:
            self.store.import_message(notification(text="更正内容"))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.get_message(record["id"])["original"]["text"], notification()["text"])

    def test_fallback_identity_distinguishes_occurrences(self):
        first, _ = normalize_message(notification(message_id=""))
        second, _ = normalize_message(notification(message_id="", sent_at="2026-09-17T10:00:00+08:00"))
        self.assertNotEqual(first, second)

    def test_source_text_whitespace_is_preserved(self):
        original_text = "  原通知\n\n"
        record, _ = self.store.import_message(notification(text=original_text))
        self.assertEqual(self.store.get_message(record["id"])["original"]["text"], original_text)

    def test_equivalent_timezones_normalize_to_same_record(self):
        first, _ = normalize_message(notification())
        second, _ = normalize_message(notification(sent_at="2026-09-16T02:00:00Z"))
        self.assertEqual(first, second)

    def test_missing_or_naive_timestamp_is_rejected(self):
        for instant in (None, "2026-09-16T10:00:00", "not-a-date"):
            with self.subTest(instant=instant), self.assertRaises(AssistantError):
                self.store.import_message(notification(sent_at=instant))

    def test_second_writer_is_rejected(self):
        with self.assertRaises(AssistantError) as caught:
            Store(self.root)
        self.assertEqual(caught.exception.status, 409)

    def test_broad_data_directory_is_rejected(self):
        with self.assertRaises(AssistantError):
            Store(Path.cwd())

    def test_directory_and_file_are_private(self):
        record, _ = self.store.import_message(notification())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "messages" / f"{record['id']}.json").stat().st_mode & 0o777, 0o600)

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(AssistantError):
            self.store.get_message("../../.env")

    def test_external_names_never_become_paths(self):
        record, _ = self.store.import_message(notification(conversation="../../other", message_id="../escape"))
        self.assertTrue((self.root / "messages" / f"{record['id']}.json").is_file())
        self.assertEqual(len(list((self.root / "messages").iterdir())), 1)

    def test_interrupted_write_preserves_previous_file(self):
        self.store.update_memory("profile", "原画像")
        path = self.root / "memory" / "profile.md"
        with patch("assistant.storage.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.store._write(path, "新画像")
        self.assertEqual(path.read_text(), "原画像")
        self.assertFalse(list(path.parent.glob(".tmp-*")))

    def test_memory_intent_recovers_after_interruption(self):
        self.store.update_memory("rules", "旧规则")
        original_write = self.store._write

        def interrupted(path, content):
            if path.name == "rules.md":
                raise OSError("synthetic interruption")
            return original_write(path, content)

        with patch.object(self.store, "_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.update_memory("rules", "新规则")
        self.assertTrue((self.root / ".memory-pending.json").exists())
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.memory()["rules"], "新规则")
        self.assertFalse((self.root / ".memory-pending.json").exists())

    def test_memory_snapshots_allow_review_and_revert(self):
        self.store.update_memory("profile", "旧画像")
        self.store.update_memory("profile", "新画像")
        snapshots = [json.loads(path.read_text()) for path in (self.root / "memory" / "history").glob("*.json")]
        snapshot = next(item for item in snapshots if item["after"] == "新画像")
        self.store.update_memory("profile", snapshot["before"])
        self.assertEqual(self.store.memory()["profile"], "旧画像")

    def test_corrupt_file_is_not_treated_as_empty(self):
        record, _ = self.store.import_message(notification())
        path = self.root / "messages" / f"{record['id']}.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(AssistantError) as caught:
            self.store.import_message(notification())
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(path.read_text(), "{broken")

    def test_unknown_schema_is_rejected(self):
        record, _ = self.store.import_message(notification())
        path = self.root / "messages" / f"{record['id']}.json"
        record["schema_version"] = 99
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(AssistantError):
            self.store.get_message(record["id"])

    def test_corrupt_saved_analysis_is_rejected(self):
        record, _ = self.store.import_message(notification())
        path = self.root / "messages" / f"{record['id']}.json"
        record.update(status="success", analysis={"summary": "缺少其他字段"})
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(AssistantError) as caught:
            self.store.get_message(record["id"])
        self.assertEqual(caught.exception.status, 500)

    def test_optional_envelope_fields_have_defaults(self):
        record, _ = self.store.import_message(notification())
        path = self.root / "messages" / f"{record['id']}.json"
        for key in ("error", "analysis", "coverage"):
            record.pop(key)
        path.write_text(json.dumps(record), encoding="utf-8")
        loaded = self.store.get_message(record["id"])
        self.assertEqual(loaded["coverage"], "text_only")
        self.assertIsNone(loaded["analysis"])

    def test_symlink_file_is_rejected(self):
        target = self.root / "outside.md"
        target.write_text("private", encoding="utf-8")
        profile = self.root / "memory" / "profile.md"
        profile.unlink()
        profile.symlink_to(target)
        with self.assertRaises(AssistantError):
            self.store.memory()

    def test_daily_budget_is_persistent_and_failed_attempts_count(self):
        self.store.reserve_call("2026-09-16", 1)
        self.store.close()
        self.store = Store(self.root)
        with self.assertRaises(AssistantError) as caught:
            self.store.reserve_call("2026-09-16", 1)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.store.usage("2026-09-17")["calls"], 0)

    def test_chat_survives_restart(self):
        self.store.add_chat("问题", "回答", [])
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.chat_history()[0]["reply"], "回答")

    def test_closed_store_cannot_write(self):
        self.store.close()
        with self.assertRaises(AssistantError):
            self.store.import_message(notification())
