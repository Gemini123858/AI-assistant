"""Destructive cleanup flows use temporary synthetic data only."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.cleanup import cleanup_plan, main
from assistant.config import Config
from assistant.notices import Notices
from assistant.records import Records
from assistant.service import Service
from assistant.storage import Store
from assistant.web import create_app
from .test_storage import notification


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        store = Store(self.root)
        text, _ = store.import_message(notification())
        self.ref = {"kind": "text", "id": text["id"]}
        self.text_path = store._message_path(text["id"])
        self.notice, _ = Notices(Records(Service(store, Config()), None)).create({"title": "合成通知", "source_refs": [self.ref]})
        store.update_memory("profile", "合成私人画像")
        store.update_memory("public_profile", "合成公开画像")
        store.update_memory("rules", "合成规则")
        store.update_identity({"assistant_name": "合成助手", "user_nickname": "合成昵称"})
        store.close()
        self.write("chat.json", {"schema_version": 1, "turns": []})
        self.write("conversation-migration.json", {"schema_version": 1, "imported_id": None})
        self.write("conversations/synthetic.json", {"records": [self.ref], "snapshot": "合成原文副本"})
        self.write("drafts/synthetic.json", {"source_refs": [self.ref]})
        self.write("tasks/synthetic.json", {"memory": {"profile": "合成私人画像"}})
        self.write("screenshots/synthetic.json", {"image": "合成截图"})
        self.write("mail/synthetic.json", {"text": "合成邮件"})
        self.write("mail/sync.json", {"last_uid": 10})
        self.write("usage.json", {"calls": 1})
        self.write("logs/synthetic.log", {"text": "合成日志"})
        self.write("browser-cache/synthetic.bin", {"browser": "binary fixture"})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def run_cleanup(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--data-dir", str(self.root), *args])
        return result, output.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_preview_and_cancel_do_not_mutate_data(self):
        before = self.snapshot()
        code, output = self.run_cleanup("--all")
        self.assertEqual(code, 0)
        self.assertIn("仅预览", output)
        self.assertNotIn("合成原文副本", output)
        self.assertEqual(self.snapshot(), before)
        with patch("builtins.input", return_value="no"):
            code, _ = self.run_cleanup("--all", "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(self.snapshot(), before)

    def test_selected_categories_order_and_no_cascade(self):
        plan = cleanup_plan(self.root, ["texts", "notices", "drafts"])
        self.assertEqual([paths[0].name for _, paths in plan], ["drafts", "notices", "messages"])
        before = (self.root / "conversations/synthetic.json").read_bytes()
        code, output = self.run_cleanup("--only", "texts", "--apply", "--yes")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.text_path.exists())
        self.assertTrue((self.root / "drafts/synthetic.json").exists())
        self.assertEqual((self.root / "conversations/synthetic.json").read_bytes(), before)
        store = Store(self.root)
        try:
            record = store.get_notice(self.notice["id"])
            self.assertEqual(record["content"]["source_refs"], [self.ref])
            # A removed source stays a historical reference; the card itself remains readable.
            self.assertEqual(Records(Service(store, Config()), None).read({"kind": "notice", "id": self.notice["id"]})["title"], "合成通知")
        finally:
            store.close()

    def test_profile_removes_its_history_and_pending_but_keeps_rules(self):
        histories = list((self.root / "memory/history").glob("*.json"))
        profile_history = next(p for p in histories if json.loads(p.read_text())["kind"] == "profile")
        rules_history = next(p for p in histories if json.loads(p.read_text())["kind"] == "rules")
        self.write(".memory-pending.json", {"schema_version": 1, "revision": profile_history.stem})
        code, output = self.run_cleanup("--only", "profile", "--apply", "--yes")
        self.assertEqual(code, 0, output)
        self.assertFalse(profile_history.exists())
        self.assertFalse((self.root / ".memory-pending.json").exists())
        self.assertTrue(rules_history.exists())
        store = Store(self.root)
        try:
            self.assertEqual(store.memory(), {"profile": "", "public_profile": "", "rules": "合成规则"})
        finally:
            store.close()

    def test_mail_cleanup_includes_sync_state_and_conversation_cleanup_legacy(self):
        code, output = self.run_cleanup("--only", "mail", "conversations", "--apply", "--yes")
        self.assertEqual(code, 0, output)
        for name in ("mail", "conversations", "chat.json", "conversation-migration.json"):
            self.assertFalse((self.root / name).exists())
        self.assertTrue(self.text_path.exists())

    def test_all_preserves_cache_and_optional_identity_and_app_restarts(self):
        identity = (self.root / "identity.json").read_bytes()
        code, output = self.run_cleanup("--all", "--keep-identity", "--apply", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual({p.name for p in self.root.iterdir()}, {".writer.lock", "browser-cache", "identity.json"})
        self.assertEqual((self.root / "identity.json").read_bytes(), identity)
        app = create_app(Config(data_dir=self.root))
        try:
            self.assertEqual(app.extensions["conversations"].list(), [])
            self.assertEqual(app.extensions["store"].memory()["profile"], "")
            self.assertEqual(app.extensions["store"].identity()["assistant_name"], "合成助手")
        finally:
            for name in ("conversations", "mail", "tasks", "store"):
                app.extensions[name].close()
        code, output = self.run_cleanup("--all", "--apply", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual({p.name for p in self.root.iterdir()}, {".writer.lock", "browser-cache"})

    def test_active_writer_wrong_root_and_symlinks(self):
        before = self.snapshot()
        store = Store(self.root)
        try:
            code, output = self.run_cleanup("--all", "--apply", "--yes")
            self.assertEqual(code, 2)
            self.assertIn("先停止", output)
        finally:
            store.close()
        self.assertEqual(self.snapshot(), before)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", self.temp.name, "--all", "--apply", "--yes"]), 2)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        protected = outside / "keep.txt"
        protected.write_text("keep")
        (self.root / "external").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_cleanup("--all", "--apply", "--yes")[0], 0)
        self.assertEqual(protected.read_text(), "keep")

    def test_bad_history_stops_plan_before_any_deletion(self):
        self.write("memory/history/bad.json", {"missing": "kind"})
        before = self.snapshot()
        code, _ = self.run_cleanup("--only", "drafts", "profile", "--apply", "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(self.snapshot(), before)
        # A full reset can still remove damaged records without parsing them.
        code, output = self.run_cleanup("--all", "--apply", "--yes")
        self.assertEqual(code, 0, output)
