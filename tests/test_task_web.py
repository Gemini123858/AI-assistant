import re
import tempfile
import unittest
from pathlib import Path

from assistant.config import Config
from assistant.web import create_app

from .test_tasks import FixtureBrowser, ScriptModel, PROFILE, call, final_result
from .test_generic_tasks import result


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
        self.assertEqual(status.json["version"], "0.3.0")
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

    def test_custom_scope_and_separate_public_context_survive_explicit_restart(self):
        self.model.outputs = [call("finish_task", result())]
        payload = {"request": "PRIVATE_RESTART_REQUIREMENT", "public_request": "公开资料查询",
                   "start_url": "https://catalog.example/topics/", "domains": ["catalog.example", "docs.example"],
                   "resource_domains": ["cdn.example"]}
        response = self.client.post("/api/tasks", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.app.extensions["tasks"].wait()
        old = self.client.get(f"/api/tasks/{response.json['task']['id']}").json["task"]
        self.model.outputs = [call("finish_task", result())]
        response = self.client.post(f"/api/tasks/{old['id']}/restart", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.app.extensions["tasks"].wait()
        new = self.client.get(f"/api/tasks/{response.json['task']['id']}").json["task"]
        self.assertEqual(new["scope"], old["scope"])
        self.assertEqual(new["public_request"], payload["public_request"])
        self.assertEqual(new["request"], payload["request"])
        self.assertEqual(new["status"], "completed")
        self.assertNotIn(payload["request"], str(self.model.calls))

    def test_status_exposes_effective_daily_budget_without_starting_calls(self):
        store, service = self.app.extensions["store"], self.app.extensions["service"]
        store.reserve_call(service.day(), service.config.daily_calls)
        status = self.client.get("/api/status").json
        self.assertEqual(status["browser_budget"], {"calls": 40, "steps": 120, "seconds": 900, "effective_calls": 29})
        self.assertFalse(status["capabilities"]["browser_vision"])
        self.assertEqual(self.model.calls, [])

    def test_schema_one_task_and_job_result_remain_readable_via_api(self):
        store = self.app.extensions["store"]
        legacy = store.create_task("旧任务", "bytedance_experienced", "api", store.memory(),
                                   {"calls": 12, "seconds": 300, "steps": 40, "pages": 30, "details": 20})
        store.update_task(legacy["id"], status="partial", result={"summary": "旧结果", "jobs": [], "questions": []})
        response = self.client.get(f"/api/tasks/{legacy['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["task"]["schema_version"], 1)
        self.assertEqual(response.json["task"]["result"]["summary"], "旧结果")
        self.assertNotIn("memory", response.json["task"])
