"""Budget/privacy tests only: no real browser, credentials or API requests."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.service import Service
from assistant.storage import Store
from .live_browser_check import PROFILE, TracedModel, controlled_browser_factory, review_saved, scenario_passed
from .test_tasks import DETAIL, ScriptModel, call, final_result


class SavedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)
        self.store.update_memory("profile", PROFILE)
        task = self.store.create_task("合成证据复核", "bytedance_experienced", "api", self.store.memory(),
            {"calls": 8, "seconds": 300, "steps": 40, "pages": 30, "details": 20})
        pages = [{"id": f"p{index}", "url": DETAIL.replace("123", str(123 + index)), "title": "算法开发实习生",
                  "text": "算法开发实习生\n职位要求：熟悉 Python", "kind": "detail", "links": [], "ready": True,
                  "fetched_at": "2026-09-17T12:00:00+00:00"}
                 for index in (1, 2)]
        self.task = self.store.update_task(task["id"], status="partial", pages=pages, steps=2, calls=5)
        result = final_result()
        first = result["jobs"][0]
        first["page_id"] = "p1"
        second = copy.deepcopy(first)
        second["page_id"] = "p2"
        result["jobs"].append(second)
        self.result = result

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def service(self, outputs, spent=6):
        model = ScriptModel(outputs)
        service = Service(self.store, Config(data_dir=self.root, mode="api", api_key="synthetic", daily_calls=8), model)
        for _ in range(spent):
            self.store.reserve_call(service.day(), 8)
        return service

    def test_review_counts_cumulatively_and_never_resumes_original_task(self):
        invalid = copy.deepcopy(self.result)
        invalid["jobs"][0]["job_evidence"] = "改写后的引用"
        service = self.service([call("finish_task", invalid), call("finish_task", self.result)])
        with redirect_stdout(io.StringIO()):
            review_saved(service, 8)
        self.assertEqual(self.store.usage(service.day())["calls"], 8)
        self.assertEqual(len(service.model.calls), 2)
        self.assertTrue((self.root / "result-review-8.json").exists())
        self.assertEqual(self.store.get_task(self.task["id"])["status"], "partial")
        self.assertEqual(self.store.get_task(self.task["id"])["calls"], 5)

    def test_no_call_when_cumulative_budget_already_exhausted(self):
        service = self.service([], spent=8)
        with self.assertRaises(AssistantError):
            review_saved(service, 8)
        self.assertEqual(service.model.calls, [])

    def test_review_does_not_read_real_profile_or_an_active_task(self):
        service = self.service([])
        self.store.update_memory("profile", "合成的不同画像，模拟不允许读取的资料")
        with self.assertRaises(AssistantError):
            review_saved(service, 8)
        self.store.update_memory("profile", PROFILE)
        self.store.update_task(self.task["id"], status="running")
        with self.assertRaises(AssistantError):
            review_saved(service, 8)
        self.assertEqual(service.model.calls, [])

    def test_failed_api_is_not_retried(self):
        service = self.service([AssistantError("合成 API 超时", 502)])
        with self.assertRaises(AssistantError):
            review_saved(service, 8)
        self.assertEqual(len(service.model.calls), 1)
        self.assertEqual(self.store.usage(service.day())["calls"], 7)

    def test_shared_trace_budget_counts_failures_and_does_not_reset_with_daily_usage(self):
        service = self.service([call("finish_task", self.result), AssistantError("合成网络故障", 502)])
        delegate = service.model
        traced = TracedModel(service)
        self.store.reserve_call(service.day(), 8)
        traced.complete_tools([], [])
        self.store.reserve_call(service.day(), 8)
        with self.assertRaises(AssistantError):
            traced.complete_tools([], [])
        self.assertEqual(self.store._read_json(traced.budget_path)["attempts"], 8)
        self.assertTrue(json.loads((self.root / "model-attempt-8.json").read_text())["failed"])
        with patch.object(self.store, "usage", return_value={"calls": 0}):
            with self.assertRaises(AssistantError):
                traced.complete_tools([], [])
        self.assertEqual(len(delegate.calls), 2)

    def test_reused_run_cannot_silently_raise_its_paid_authorization(self):
        service = self.service([])
        TracedModel(service)
        service.config.daily_calls = 20
        with self.assertRaises(AssistantError):
            TracedModel(service)

    def test_failure_report_is_not_mislabelled_as_successful_query(self):
        failed = {**self.task, "status": "failed", "pages": [], "errors": [{"kind": "network"}],
                  "result": {"summary": "连续连接失败。"}}
        self.assertTrue(scenario_passed("network-stop", failed))
        self.assertFalse(scenario_passed("jobs", failed))
        failed["calls"] = failed["limits"]["calls"]
        self.assertFalse(scenario_passed("network-stop", failed), "budget-forced finish is not autonomous stopping")

    def test_transient_fault_is_not_reinjected_after_browser_restart(self):
        factory = controlled_browser_factory("recover")
        first, second = Mock(), Mock()
        request = SimpleNamespace(url="https://catalog.example/topics", method="GET", resource_type="document", post_data=None)
        first.request = second.request = request
        factory(lambda: None)._route(first)
        factory(lambda: None)._route(second)
        first.abort.assert_called_once_with("addressunreachable")
        second.abort.assert_not_called()
        second.fulfill.assert_called_once()

    def test_permanent_fault_remains_after_browser_restart(self):
        factory = controlled_browser_factory("network-stop")
        for _ in range(2):
            route = Mock()
            route.request = SimpleNamespace(url="https://catalog.example/topics", method="GET", resource_type="document")
            factory(lambda: None)._route(route)
            route.abort.assert_called_once_with("addressunreachable")


if __name__ == "__main__":
    unittest.main()
