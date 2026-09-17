import re
import tempfile
import unittest
from pathlib import Path

from assistant.config import Config
from assistant.web import create_app

from .test_tasks import FixtureBrowser, ScriptModel, PROFILE, call, final_result


def outputs():
    return [call("open_link", {"link_id": "p1-l1"}), call("open_link", {"link_id": "p2-l1"}),
            call("finish_task", final_result())]


class TaskWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.model = ScriptModel(outputs())
        FixtureBrowser.fail_detail = False
        self.app = create_app(Config(data_dir=Path(self.temporary.name) / "data", mode="api",
                                     api_key="synthetic-never-sent"), self.model, FixtureBrowser)
        self.client = self.app.test_client()
        page = self.client.get("/").get_data(as_text=True)
        token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        self.headers = {"X-CSRF-Token": token}
        self.app.extensions["store"].update_memory("profile", PROFILE)

    def tearDown(self):
        self.app.extensions["tasks"].close()
        self.app.extensions["store"].close()
        self.temporary.cleanup()

    def start(self):
        response = self.client.post("/api/tasks", json={"request": "合成算法岗位查询"}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.app.extensions["tasks"].wait()
        return response.json["task"]["id"]

    def test_reads_do_not_start_model_or_browser_and_secrets_are_absent(self):
        self.assertEqual(self.client.get("/api/tasks").json["tasks"], [])
        status = self.client.get("/api/status")
        self.assertEqual(status.json["version"], "0.2.0")
        self.assertNotIn("synthetic-never-sent", status.get_data(as_text=True))
        self.assertEqual(self.model.calls, [])

    def test_task_api_exposes_progress_results_and_originals_but_not_memory_snapshot(self):
        key = self.start()
        task = self.client.get(f"/api/tasks/{key}").json["task"]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["pages"]), 3)
        self.assertIn("text", task["pages"][0])
        self.assertNotIn("memory", task)
        short = self.client.get("/api/tasks").json["tasks"][0]
        self.assertNotIn("text", short["pages"][0])
        self.assertFalse(self.client.get("/api/status").json["capabilities"]["send_email"])

    def test_task_mutations_require_csrf_and_exact_scope(self):
        self.assertEqual(self.client.post("/api/tasks", json={"request": "query"}).status_code, 403)
        for payload in ({"request": "query", "url": "https://evil.invalid"}, {"request": "query", "send": True}):
            response = self.client.post("/api/tasks", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.model.calls, [])

    def test_restart_is_explicit_new_task_and_preserves_old_evidence(self):
        old = self.start()
        self.model.outputs = outputs()
        response = self.client.post(f"/api/tasks/{old}/restart", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.app.extensions["tasks"].wait()
        self.assertNotEqual(old, response.json["task"]["id"])
        self.assertEqual(len(self.client.get("/api/tasks").json["tasks"]), 2)
        self.assertEqual(self.client.get(f"/api/tasks/{old}").json["task"]["status"], "completed")

    def test_completed_cancel_is_idempotent_and_invalid_ids_rejected(self):
        key = self.start()
        response = self.client.post(f"/api/tasks/{key}/cancel", json={}, headers=self.headers)
        self.assertEqual(response.json["task"]["status"], "completed")
        self.assertEqual(self.client.get("/api/tasks/invalid").status_code, 400)
        self.assertEqual(self.client.post(f"/api/tasks/{key}/cancel", json={"send": True},
                                         headers=self.headers).status_code, 400)
