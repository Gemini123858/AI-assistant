import re
import tempfile
import unittest
from pathlib import Path

from assistant.config import Config
from assistant.web import create_app

from .test_tasks import DETAIL, LIST, FixtureBrowser, ScriptModel, PROFILE, call, final_result
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
        self.assertEqual(status.json["version"], "0.4.0")
        self.assertNotIn("synthetic-never-sent", status.get_data(as_text=True))
        self.assertEqual(self.model.calls, [])

    def test_new_goal_contract_restart_and_ambiguous_payload_rejection(self):
        for payload in ({"goal": ""}, {"goal": "公开目标", "request": "旧要求"},
                        {"goal": "公开目标", "public_request": "另一个目标"},
                        {"request": "旧要求", "private_conditions": "私人条件"}):
            self.assertEqual(self.client.post("/api/tasks", json=payload, headers=self.headers).status_code, 400)
        self.assertEqual(self.model.calls, [])
        self.model.outputs = [call("analyze_results", {}), call("finish_task", result())]
        payload = {"goal": "查询计算机相关岗位及详细要求", "profile_supplement": "本次关注图算法",
                   "private_conditions": "合成私人筛选条件"}
        started = self.client.post("/api/tasks", json=payload, headers=self.headers)
        self.assertEqual(started.status_code, 202)
        self.app.extensions["tasks"].wait()
        key = started.json["task"]["id"]
        task = self.client.get(f"/api/tasks/{key}").json["task"]
        self.assertEqual(task["public_request"], payload["goal"])
        self.assertEqual(task["private_conditions"], payload["private_conditions"])
        self.assertEqual(task["profile_supplement"], payload["profile_supplement"])
        self.model.outputs = [call("analyze_results", {}), call("finish_task", result())]
        restarted = self.client.post(f"/api/tasks/{key}/restart", json={}, headers=self.headers)
        self.assertEqual(restarted.status_code, 202)
        self.app.extensions["tasks"].wait()
        self.assertEqual(restarted.json["task"]["private_conditions"], payload["private_conditions"])
        self.assertEqual(restarted.json["task"]["profile_supplement"], payload["profile_supplement"])

    def test_action_named_reading_page_starts_without_exception_setup(self):
        payload = {"request": "合成阅读", "public_request": "读取公开文章",
                   "start_url": "https://jobs.bytedance.com/wiki/delete"}
        started = self.client.post("/api/tasks", json=payload, headers=self.headers)
        self.assertEqual(started.status_code, 202)
        self.app.extensions["tasks"].wait()
        key = started.json["task"]["id"]
        saved = self.app.extensions["store"].get_task(key)
        self.assertEqual(saved["scope"]["start_url"], payload["start_url"])
        self.assertEqual(self.client.get(f"/api/tasks/{key}").status_code, 200)

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

    def test_only_latest_completed_task_keeps_closeable_followup(self):
        first = self.start()
        session = self.client.get("/api/followups").json["task"]
        self.assertEqual(session["key"], first)
        self.assertNotIn("messages", session)
        self.model.outputs = [call("finish_followup", {"reply": "基于已保存结果继续回答。"})]
        reply = self.client.post(f"/api/followups/task/{first}", json={"message": "还有什么未知项？"},
                                 headers=self.headers)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json["session"]["turns"][-1]["role"], "assistant")
        self.assertNotIn("messages", reply.json["session"])
        self.model.outputs = outputs()
        second = self.start()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get("/api/followups").json["task"]["key"], second)
        self.assertEqual(self.client.post(f"/api/followups/task/{first}", json={"message": "旧追问"},
                                         headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post(f"/api/followups/task/{second}/close", json={},
                                         headers=self.headers).status_code, 200)
        self.assertIsNone(self.client.get("/api/followups").json["task"])

    def test_task_followup_can_browse_with_original_scope_and_keep_new_evidence(self):
        key = self.start()
        followup_list = LIST + "?followup=1"
        self.model.outputs = [
            call("browse_public", {"goal": "补查岗位详情", "url": followup_list}),
            call("open_link", {"link_id": "f1-l1"}),
            call("return_to_followup", {}),
            call("finish_followup", {"reply": "已补查详情，要求包括 Python。"}),
        ]
        response = self.client.post(f"/api/followups/task/{key}",
                                    json={"message": "请继续打开列表中的岗位详情。"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Python", response.json["session"]["turns"][-1]["text"])
        self.assertIn(followup_list, FixtureBrowser.reads)
        self.assertIn(DETAIL, FixtureBrowser.reads)
        internal = self.app.extensions["service"]._followups["task"]
        self.assertTrue(any(page["url"] == followup_list for page in internal["state"]["pages"]))

    def test_task_followup_cannot_expand_a_limited_original_scope(self):
        self.model.outputs = [call("finish_task", result())]
        started = self.client.post("/api/tasks", json={"request": "限定站内阅读", "public_request": "读取目录",
            "start_url": "https://catalog.example/topics/", "domains": ["catalog.example"],
            "public_web": False}, headers=self.headers)
        self.assertEqual(started.status_code, 202)
        self.app.extensions["tasks"].wait()
        key = started.json["task"]["id"]
        FixtureBrowser.reads = []
        self.model.outputs = [
            call("browse_public", {"goal": "尝试扩大范围", "url": "https://outside.example/"}),
            call("finish_followup", {"reply": "该地址不在原任务范围内，未访问。"}),
        ]
        response = self.client.post(f"/api/followups/task/{key}", json={"message": "打开另一个网站。"},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("https://outside.example/", FixtureBrowser.reads)
        self.assertIn("不在原任务范围", response.json["session"]["turns"][-1]["text"])

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
        self.assertEqual(status["browser_budget"], {"calls": 40, "default_calls": 20, "steps": 120, "seconds": 900, "effective_calls": 29})
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
