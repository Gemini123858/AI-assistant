"""New public-task contracts and recovery; synthetic model/browser only."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.browser import BrowserError, BrowserScope, SITE, allowed_request, navigation_url
from assistant.config import Config
from assistant.errors import AssistantError
from assistant.service import Service
from assistant.storage import Store
from assistant.tasks import TASK_SYSTEM, Tasks, validate_result
from .test_tasks import FixtureBrowser, ScriptModel, LIST, ENTRY, call


def result(page="p1", **changes):
    return {"summary": "仅引用已保存的公开信息。", "questions": [], "outcome": "completed",
            "stop_reason": "查询已完成，覆盖有限。", "findings": [{"page_id": page,
                "text": "这是已读取来源中的信息。", "line_start": 1, "line_end": 1,
                "uncertainties": []}], **changes}


class RecoveringBrowser(FixtureBrowser):
    fail_count = 0
    startup_count = 0
    startup_failures = 0

    def __enter__(self):
        type(self).startup_count += 1
        if type(self).startup_count <= type(self).startup_failures:
            raise BrowserError("合成启动时 DNS 不可达。", "network")
        return self

    def read(self, url):
        if type(self).fail_count:
            type(self).fail_count -= 1
            self.reads.append(url)
            raise BrowserError("合成连接失败 net::ERR_ADDRESS_UNREACHABLE", "network")
        return super().read(url)


class GenericTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.store = Store(self.root)
        self.tasks = None
        FixtureBrowser.reads, FixtureBrowser.fail_detail = [], False
        RecoveringBrowser.fail_count = RecoveringBrowser.startup_count = RecoveringBrowser.startup_failures = 0

    def tearDown(self):
        if self.tasks:
            self.tasks.close()
        self.store.close()
        self.temporary.cleanup()

    def runner(self, outputs, **config):
        model = ScriptModel(outputs)
        self.tasks = Tasks(Service(self.store, Config(data_dir=self.root, **config), model), FixtureBrowser)
        return model

    def start_generic(self, **payload):
        task = self.tasks.start({"request": "私人筛选条件", "public_request": "图神经网络 上海 研究实习",
                                 "start_url": LIST, **payload})
        self.tasks.wait(timeout=10)
        return self.store.get_task(task["id"])

    def test_first_navigation_failure_can_retry_same_url_and_complete(self):
        model = self.runner([call("open_url", {"url": LIST}), call("finish_task", result())])
        self.tasks.browser_factory = RecoveringBrowser
        RecoveringBrowser.fail_count = 1
        task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["steps"], 2)
        self.assertEqual(len(task["errors"]), 1)
        self.assertEqual(task["errors"][0]["kind"], "network")
        self.assertEqual(FixtureBrowser.reads, [LIST, LIST])
        self.assertIn("ERR_ADDRESS_UNREACHABLE", json.dumps(model.calls[0], ensure_ascii=False))
        self.assertEqual(task["result"]["findings"][0]["quote"], "岗位列表")

    def test_goal_survives_trimming_analysis_and_resume_without_private_leak(self):
        goal, private = "查询计算机相关岗位并读取详细要求", "PRIVATE_CONDITIONS_SENTINEL"
        self.store.update_memory("public_profile", "PUBLIC_PROFILE_SENTINEL")
        self.store.update_memory("profile", "PRIVATE_PROFILE_SENTINEL")
        model = self.runner([call("search_source", {"page_id": "p1", "query": "岗位"}),
            call("analyze_results", {}), call("resume_browsing", {}),
            call("analyze_results", {}), call("finish_task", result())])
        delegate = model.complete_tools
        def with_long_history(messages, tools, timeout=None):
            if not model.calls:
                messages.insert(2, {"role": "user", "content": "合成长上下文" * 30000})
            return delegate(messages, tools, timeout)
        with patch.object(model, "complete_tools", side_effect=with_long_history):
            task = self.tasks.start({"goal": goal, "profile_supplement": "TASK_PROFILE_SUPPLEMENT",
                                     "private_conditions": private, "start_url": LIST})
            self.tasks.wait(timeout=10)
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["request"], goal)
        self.assertEqual(saved["private_conditions"], private)
        self.assertEqual(saved["profile_supplement"], "TASK_PROFILE_SUPPLEMENT")
        for index, messages in enumerate(model.calls):
            context = json.loads(messages[1]["content"])
            self.assertEqual(context["task_goal"], goal)
            self.assertEqual(context["public_profile"], "PUBLIC_PROFILE_SENTINEL")
            self.assertEqual(context["task_profile_supplement"], "TASK_PROFILE_SUPPLEMENT")
            serialized = json.dumps(messages)
            if index in (0, 1, 3):
                self.assertNotIn(private, serialized)
                self.assertNotIn("PRIVATE_PROFILE_SENTINEL", serialized)
            else:
                self.assertEqual(context["private_conditions"], private)
        self.assertIn("上下文已裁剪", model.calls[1][1]["content"])

    def test_browser_prompt_prefers_new_evidence_and_early_finish(self):
        self.assertIn("最短证据路径", TASK_SYSTEM)
        self.assertIn("不要紧接着重复observe_page", TASK_SYSTEM)
        self.assertIn("连续两次无进展应停止该路径", TASK_SYSTEM)
        self.assertIn("remaining.calls", TASK_SYSTEM)

    def test_private_conditions_cannot_be_skipped_by_public_finish(self):
        model = self.runner([call("finish_task", result()), call("analyze_results", {}),
                             call("finish_task", result())])
        task = self.tasks.start({"goal": "查询计算机岗位", "private_conditions": "PRIVATE_CONDITIONS_SENTINEL",
                                 "start_url": LIST})
        self.tasks.wait(timeout=10)
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["phase"], "analysis")
        self.assertIn("先调用analyze_results", model.calls[1][-1]["content"])
        self.assertNotIn("PRIVATE_CONDITIONS_SENTINEL", json.dumps(model.calls[1]))

    def test_closing_budget_enters_private_analysis_before_final_call(self):
        model = self.runner([call("search_source", {"page_id": "p1", "query": "岗位"}),
                             call("finish_task", result())], browser_calls=2)
        task = self.tasks.start({"goal": "查询计算机岗位", "private_conditions": "PRIVATE_CONDITIONS_SENTINEL",
                                 "start_url": LIST})
        self.tasks.wait(timeout=10)
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["calls"], 2)
        self.assertNotIn("PRIVATE_CONDITIONS_SENTINEL", json.dumps(model.calls[0]))
        self.assertIn("PRIVATE_CONDITIONS_SENTINEL", json.dumps(model.calls[1]))
        self.assertEqual({t["function"]["name"] for t in model.tools[1]}, {"finish_task"})

    def test_local_search_is_compact_and_survives_context_trimming(self):
        search = call("search_source", {"page_id": "p1", "query": "岗位"})
        model = self.runner([search, call("finish_task", result())])
        delegate = model.complete_tools
        def with_long_history(messages, tools, timeout=None):
            if not model.calls:
                messages.insert(1, {"role": "user", "content": "合成长上下文" * 30000})
            return delegate(messages, tools, timeout)
        with patch.object(model, "complete_tools", side_effect=with_long_history):
            task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        context = json.loads(model.calls[1][1]["content"])
        self.assertEqual(context["recent_sources"][0]["numbered_lines"], [{"line": 1, "text": "岗位列表"}])
        self.assertNotIn("elements", context["recent_sources"][0])
        self.assertNotIn("links", context["recent_sources"][0])

    def test_failed_homepage_can_be_bypassed_without_homepage_workflow(self):
        self.runner([call("open_url", {"url": LIST}), call("finish_task", result())])
        self.tasks.browser_factory = RecoveringBrowser
        RecoveringBrowser.fail_count = 1
        task = self.start_generic(start_url=ENTRY)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["pages"][0]["url"], LIST)

    def test_permanent_network_failure_can_report_and_stop_early(self):
        model = self.runner([call("open_url", {"url": LIST}), call("finish_task", {
            "summary": "必要页面连续连接失败，不能报告零岗位。", "questions": [],
            "outcome": "failed", "stop_reason": "ERR_ADDRESS_UNREACHABLE；已重试，根因未知。"})])
        self.tasks.browser_factory = RecoveringBrowser
        RecoveringBrowser.fail_count = 100
        task = self.start_generic()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["calls"], 2)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(task["errors"]), 2)
        self.assertIn("根因未知", task["stop_reason"])
        self.assertEqual(task["pages"], [])

    def test_browser_startup_can_be_recovered_by_model_tool(self):
        self.runner([call("restart_browser", {}), call("open_url", {"url": LIST}),
                     call("finish_task", result())])
        self.tasks.browser_factory = RecoveringBrowser
        RecoveringBrowser.startup_failures = 1
        task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(RecoveringBrowser.startup_count, 2)

    def test_private_analysis_has_no_browser_tools_and_resume_clears_private_turns(self):
        self.store.update_memory("profile", "PRIVATE_PROFILE_SENTINEL")
        model = self.runner([call("analyze_results", {}),
            call("fill_field", {"element_id": "current-search", "text": "PRIVATE_PROFILE_SENTINEL", "submit": True}),
            call("resume_browsing", {}), call("open_url", {"url": ENTRY}), call("finish_task", result())])
        task = self.start_generic(request="PRIVATE_REQUIREMENT_SENTINEL")
        self.assertEqual(task["status"], "completed")
        for index in (0, 3, 4):
            serialized = json.dumps(model.calls[index], ensure_ascii=False)
            self.assertNotIn("PRIVATE_PROFILE_SENTINEL", serialized)
            self.assertNotIn("PRIVATE_REQUIREMENT_SENTINEL", serialized)
        self.assertIn("PRIVATE_PROFILE_SENTINEL", json.dumps(model.calls[1]))
        self.assertEqual({t["function"]["name"] for t in model.tools[1]},
                         {"read_source", "search_source", "finish_task", "resume_browsing"})
        self.assertEqual(FixtureBrowser.reads, [LIST, ENTRY])

    def test_free_search_and_revisits_are_not_keyword_or_url_limited(self):
        model = self.runner([call("fill_field", {"element_id": "current-search",
            "text": "图神经网络 上海 研究实习", "submit": True}),
            call("open_url", {"url": LIST}), call("finish_task", result())])
        task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        schema = next(t for t in model.tools[0] if t["function"]["name"] == "fill_field")
        self.assertNotIn("enum", schema["function"]["parameters"]["properties"]["text"])
        self.assertEqual(task["steps"], 3)
        self.assertIn("图神经网络 上海", FixtureBrowser.reads[1])

    def test_unprogressing_retries_warn_but_use_total_budget(self):
        model = self.runner([call("open_url", {"url": LIST}) for _ in range(4)] + [call("finish_task", result())])
        task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["steps"], 5)
        self.assertIn("loop_warning", json.loads(model.calls[-1][-1]["content"]))
        self.assertIn("loop_warning", task["actions"][-1])
        self.assertEqual(task["actions"][-1]["remaining"]["steps"], 115)

    def test_limited_task_domains_are_immutable_and_cross_domain_errors_are_recoverable(self):
        self.runner([call("open_url", {"url": "https://foreign.example/info"}), call("finish_task", result())])
        initial = self.tasks.start({"goal": "公开信息查询", "start_url": LIST, "public_web": False})
        self.tasks.wait(timeout=10)
        task = self.store.get_task(initial["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(FixtureBrowser.reads, [LIST])
        self.assertEqual(task["scope"]["domains"], [SITE])
        with self.assertRaises(AssistantError):
            self.store.update_task(task["id"], scope={"domains": ["foreign.example"]})

    def test_new_tasks_default_to_public_reading_without_changing_their_goal(self):
        model = self.runner([call("open_url", {"url": "https://other.example/info"}), call("finish_task", result())])
        task = self.start_generic()
        self.assertTrue(task["scope"]["public_web"])
        self.assertEqual(FixtureBrowser.reads, [LIST, "https://other.example/info"])
        self.assertEqual(json.loads(model.calls[-1][1]["content"])["task_goal"], task["public_request"])

    def test_numbered_local_source_can_be_read_during_private_analysis(self):
        model = self.runner([call("analyze_results", {}),
                             call("read_source", {"page_id": "p1", "start_line": 1, "lines": 1}),
                             call("finish_task", result())])
        task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        source = json.loads(model.calls[-1][-1]["content"])["source"]
        self.assertEqual(source["numbered_lines"], [{"line": 1, "text": "岗位列表"}])
        self.assertEqual(FixtureBrowser.reads, [LIST])

    def test_new_limits_and_daily_effective_budget_are_visible_and_not_auto_raised(self):
        self.assertEqual((Config().browser_calls, Config().browser_steps, Config().browser_seconds), (40, 120, 900))
        self.runner([call("finish_task", result())], mode="api", api_key="synthetic", daily_calls=3)
        task = self.start_generic()
        self.assertEqual(task["limits"]["calls"], 3)
        self.assertEqual(self.tasks.service.config.daily_calls, 3)
        self.assertEqual(task["schema_version"], 2)
        with self.assertRaises(AssistantError):
            validate_result(result(page="missing"), task)

    def test_step_cap_stops_without_hidden_extra_api_calls(self):
        model = self.runner([call("open_url", {"url": LIST}), call("open_url", {"url": LIST})], browser_steps=2)
        task = self.start_generic()
        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["steps"], 2)
        self.assertEqual(len(model.calls), 2)

    def test_large_screenshot_is_preserved_for_model_and_saved_privately(self):
        image = "data:image/png;base64," + "A" * 170000
        with patch.object(FixtureBrowser, "screenshot", return_value=image, create=True):
            model = self.runner([call("take_screenshot", {}), call("finish_task", result())], browser_vision=True)
            task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        blocks = model.calls[-1][-1]["content"]
        self.assertEqual(blocks[-1]["image_url"]["url"], image)
        file = self.root / "screenshots" / f"{task['id']}-2.json"
        self.assertTrue(file.exists())
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(task["actions"][-1]["screenshot"], file.name)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_vision_tools_are_not_silently_enabled(self):
        model = self.runner([call("finish_task", result())])
        self.start_generic()
        self.assertNotIn("take_screenshot", [t["function"]["name"] for t in model.tools[0]])

    def test_general_citations_reject_unverified_or_fabricated_lines(self):
        self.runner([call("finish_task", result())])
        task = self.start_generic()
        bad = result()
        bad["findings"][0]["line_end"] = 100
        with self.assertRaises(AssistantError):
            validate_result(bad, task)
        task["pages"][0]["ready"] = False
        with self.assertRaises(AssistantError):
            validate_result(result(), task)

    def test_unseen_line_requires_read_source_and_can_be_fixed_within_budget(self):
        original = FixtureBrowser.read
        def long_page(browser, url):
            page = original(browser, url)
            page["text"] = "\n".join(f"合成资料第{n}行" for n in range(1, 81))
            return page
        final = result()
        final["findings"][0].update(line_start=65, line_end=65)
        with patch.object(FixtureBrowser, "read", long_page):
            model = self.runner([call("finish_task", final),
                call("read_source", {"page_id": "p1", "start_line": 65, "lines": 1}),
                call("finish_task", final)], browser_calls=3)
            task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["calls"], 3)
        self.assertIn("尚未提供给模型", model.calls[1][-1]["content"])
        self.assertEqual(json.loads(model.calls[2][-1]["content"])["source"]["text"], "合成资料第65行")

    def test_job_lines_derive_evidence_and_title_on_non_bytedance_page(self):
        page = {"id": "p1", "kind": "page", "url": "https://careers.example/roles/engineer",
                "title": "合成研发实习岗位", "text": "合成研发实习岗位\n熟悉 Python\n经验未知",
                "fetched_at": "2026-09-17T12:00:00+00:00", "seen_ranges": [[1, 3]]}
        output = result(findings=[], jobs=[{"page_id": "p1", "fit": "needs_confirmation",
            "reason": "需要确认经验。", "uncertainties": ["经验未知"], "line_start": 2, "line_end": 2}])
        checked = validate_result(output, {"pages": [page], "memory": {"profile": "会 Python"}})
        self.assertEqual(checked["jobs"][0]["title"], page["title"])
        self.assertEqual(checked["jobs"][0]["job_evidence"], "熟悉 Python")
        self.assertEqual(checked["jobs"][0]["profile_evidence"], [])
        page["title"] = "招聘栏目"
        page["text"] = "招聘栏目\n实际岗位名称\n熟悉 Python"
        output["jobs"][0].update(title_line=2, line_start=3, line_end=3)
        checked = validate_result(output, {"pages": [page], "memory": {"profile": "会 Python"}})
        self.assertEqual(checked["jobs"][0]["title"], "实际岗位名称")
        output["jobs"][0]["title_line"] = 4
        with self.assertRaises(AssistantError):
            validate_result(output, {"pages": [page], "memory": {"profile": "会 Python"}})

    def test_saved_source_keyword_search_returns_numbered_evidence_without_navigation(self):
        model = self.runner([call("analyze_results", {}),
            call("search_source", {"page_id": "p1", "query": "岗位"}), call("finish_task", result())])
        task = self.start_generic()
        self.assertEqual(task["status"], "completed")
        matches = json.loads(model.calls[-1][-1]["content"])["matches"]
        self.assertEqual(matches[0]["numbered_lines"], [{"line": 1, "text": "岗位列表"}])
        self.assertEqual(FixtureBrowser.reads, [LIST])

    def test_partial_finish_does_not_overwrite_pending_cancel(self):
        self.runner([call("finish_task", result())])
        task = self.start_generic()
        self.store.update_task(task["id"], status="running", cancel_requested=True)
        saved = self.store.update_task(task["id"], status="partial", result={"summary": "不能发布"})
        self.assertEqual(saved["status"], "cancelled")
        self.assertIsNone(saved["result"])


class GenericScopeTests(unittest.TestCase):
    def test_legacy_post_grants_are_compatible_but_no_longer_required(self):
        scope = BrowserScope("https://docs.example/catalog", read_post_urls=["https://docs.example/data"])
        self.assertTrue(allowed_request("https://docs.example/data?page=2", "POST", "fetch", '{"q":"图结构","new_field":2}', scope))
        self.assertTrue(allowed_request("https://docs.example/data", "POST", "document", 'q=Python', scope))
        self.assertTrue(allowed_request("https://docs.example/other", "POST", "fetch", '{}', scope))
        for url, body in (("https://docs.example/data", '{"operation":"delete"}'),
                          ("https://docs.example/data", '{"query":"mutation { createUser }"}')):
            self.assertFalse(allowed_request(url, "POST", "fetch", body, scope))
        with self.assertRaises(AssistantError):
            BrowserScope("https://docs.example/catalog", read_post_urls=["https://foreign.example/data"])
        self.assertEqual(BrowserScope(**scope.as_dict()).as_dict(), scope.as_dict())

    def test_relative_url_semantics_fragments_and_write_queries_are_preserved_or_denied(self):
        self.assertEqual(navigation_url(ENTRY), ENTRY)
        url = "https://" + SITE + "/public/#section"
        self.assertEqual(navigation_url(url), url)
        self.assertTrue(navigation_url("https://" + SITE + "/search?q=delete"))
        for query in ("?submit=true", "?action=delete", "?operation=send"):
            with self.subTest(query=query), self.assertRaises(AssistantError):
                navigation_url("https://" + SITE + "/public/" + query)

    def test_generic_scope_paths_filters_and_non_listed_search_words(self):
        scope = BrowserScope("https://docs.example/catalog", ["docs.example"])
        self.assertEqual(navigation_url("https://docs.example/catalog?q=图神经网络&city=上海&page=88", scope),
                         "https://docs.example/catalog?q=图神经网络&city=上海&page=88")
        self.assertTrue(allowed_request("https://docs.example/app.js", "GET", "script", scope=scope))
        self.assertTrue(allowed_request("https://docs.example/api/preferences", "GET", "xhr", scope=scope))
        # Browser-reported static subresources load from any public host (transport
        # pins each host's public IPs); only xhr/fetch stay inside the scope.
        self.assertFalse(allowed_request("https://" + SITE + "/api/v1/common/setting", "GET", "xhr", scope=scope))
        self.assertFalse(allowed_request("https://docs.example/api/save", "POST", "fetch", "{}", scope))

    def test_invalid_authority_and_credentials_never_become_scope(self):
        for host in ("127.0.0.1", "localhost", "*.example.com", "a.local", "example.com:443", "https://example.com", "a..example"):
            with self.subTest(host=host), self.assertRaises(AssistantError):
                BrowserScope("https://example.com", [host])
        for url in ("https://key@example.com", "http://example.com", "https://example.com:444", "https://example.com/delete"):
            with self.subTest(url=url), self.assertRaises(AssistantError):
                BrowserScope(url)
