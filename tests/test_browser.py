import io
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from assistant.browser import (SITE, BrowserScope, JobBrowser, allowed_request,
                               element_actions, navigation_url, resolver_rules, is_detail)
from .browser_fixtures import SEARCH_PATH, ASSET_HOSTS
from assistant.errors import AssistantError


def search_body(**changes):
    return json.dumps({"keyword": "算法", "limit": 10, "offset": 0,
                       "portal_type": 2, "portal_entrance": 1, **changes})


class BrowserPolicyTests(unittest.TestCase):
    def test_unknown_posts_follow_same_policy_on_every_authorized_host(self):
        for host in ("catalog.example", "jobs.bytedance.com", "career.honor.com"):
            scope = BrowserScope("https://" + host + "/catalog")
            for body in (None, "", '{}', '[{"q":"delete upload"}]', 'q=删除&locale=zh_CN'):
                for kind in ("xhr", "fetch", "document"):
                    self.assertTrue(allowed_request("https://" + host + "/unknown/query?locale=zh", "POST", kind, body, scope))
            self.assertFalse(allowed_request("https://foreign.example/query", "POST", "xhr", '{}', scope))

    def test_generic_data_transport_errors_retry_and_no_site_response_schema(self):
        browser = JobBrowser(lambda: None, scope=BrowserScope("https://catalog.example"))
        request = SimpleNamespace(url="https://catalog.example/api/data", method="POST", resource_type="xhr", post_data='{}', headers={})
        browser._request_started(request)
        self.assertTrue(browser.diagnostics()["pending_data_requests"])
        browser._response(SimpleNamespace(url=request.url, request=request, status=503))
        self.assertTrue(browser.data_errors)
        browser._request_started(request)
        self.assertFalse(browser.data_errors)
        response = SimpleNamespace(url=request.url, request=request, status=200, json=Mock())
        browser._response(response)
        response.json.assert_not_called()
        self.assertFalse(browser._pending_data)
        self.assertFalse(browser.data_errors)
        browser._response(SimpleNamespace(url=request.url, request=Mock(), status=503))
        self.assertFalse(browser.data_errors, "A stale response cannot invalidate the current navigation")

    def test_custom_denied_paths_override_legacy_grants_and_encoded_query_variants(self):
        url = "https://catalog.example/api/data"
        scope = BrowserScope("https://catalog.example/catalog", read_post_urls=[url], blocked_urls=[url])
        for target in (url, url + "?page=2", url.replace("data", "%64ata")):
            for method in ("GET", "POST"):
                self.assertFalse(allowed_request(target, method, "fetch", '{}', scope))
            with self.assertRaises(AssistantError):
                navigation_url(target, scope)
        self.assertEqual(BrowserScope(**scope.as_dict()).as_dict(), scope.as_dict())
        encoded = BrowserScope("https://catalog.example/catalog", blocked_urls=["https://catalog.example/api/%64ata%3Fpath"])
        self.assertEqual(BrowserScope(**encoded.as_dict()).as_dict(), encoded.as_dict())
        self.assertFalse(allowed_request("https://catalog.example/api/data%3Fpath?x=1", "POST", "fetch", '{}', encoded))
        with self.assertRaises(AssistantError):
            BrowserScope("https://catalog.example", blocked_urls=["https://foreign.example/data"])

    def test_decorative_label_change_does_not_stale_but_write_change_still_denied(self):
        browser = JobBrowser(lambda: None)
        url = "https://" + SITE + "/experienced/position"
        browser.page = SimpleNamespace(url=url)
        browser._element_url = url
        info = {"connected": True, "visible": True, "tag": "button", "role": "button",
                "label": "展开 3 项", "disabled": False}
        handle = Mock()
        handle.evaluate.return_value = {**info, "label": "展开 4 项"}
        browser._elements["o1-e1"] = (handle, info)
        self.assertIs(browser._element("o1-e1", "click"), handle)
        handle.evaluate.return_value = {**info, "label": "发送"}
        with self.assertRaises(AssistantError):
            browser._element("o1-e1", "click")

    def test_cdp_callback_failure_aborts_paused_request_and_reports_internal_error(self):
        browser = JobBrowser(lambda: None)
        browser._cdp = Mock()
        browser._cdp.send.side_effect = [RuntimeError("synthetic protocol fault"), None]
        browser._redirect_response({"requestId": "synthetic"})
        self.assertEqual(browser._cdp.send.call_args.args[0], "Fetch.failRequest")
        self.assertTrue(browser.diagnostics()["internal_errors"])
        from playwright.sync_api import Error as PlaywrightError
        browser.internal_errors.clear()
        browser._cdp.send.side_effect = PlaywrightError("Protocol error: Invalid InterceptionId.")
        browser._redirect_response({"requestId": "already-cancelled"})
        self.assertFalse(browser.internal_errors)

    def test_capabilities_not_popup_workflow_and_sensitive_controls_denied(self):
        info = {"connected": True, "visible": True, "tag": "button", "label": "稍后再试", "disabled": False,
                "href": "", "dialog": True, "search": False, "pagination": False, "type": "button"}
        url = "https://" + SITE + "/experienced/position"
        self.assertEqual(element_actions(info, url), ["click"])
        self.assertEqual(element_actions({**info, "label": "暂不使用"}, url), ["click"])
        for label in ("立即投递", "登录", "提交", "上传简历", "同意条款", "收藏", "关注", "转发"):
            self.assertEqual(element_actions({**info, "label": label}, url), [])
        for label in ("立即体验", "未知操作", "确认选择", "我知道了"):
            self.assertEqual(element_actions({**info, "label": label}, url), ["click"])
        search = {**info, "tag": "input", "type": "text", "dialog": False, "search": True,
                  "label": "搜索职位", "placeholder": "搜索职位"}
        self.assertEqual(element_actions(search, url), ["fill", "press"])
        for changes in ({"type": "password"}, {"type": "email"}, {"disabled": True}, {"visible": False}):
            self.assertEqual(element_actions({**search, **changes}, url), [])
        self.assertEqual(element_actions(search, url + "/123/detail"), ["fill", "press"])
        self.assertEqual(element_actions({**info, "dialog": False, "search": True, "label": "未知操作"}, url), ["click"])
        self.assertEqual(element_actions({**info, "dialog": False, "pagination": True, "label": "2"}, url), ["click"])
        self.assertEqual(element_actions({**info, "dialog": False, "pagination": True, "label": "1000"}, url), ["click"])
        self.assertEqual(element_actions({**info, "dialog": False, "pagination": True, "label": "下一页"},
                                         url + "?current=20"), ["click"])

    def test_unobserved_and_changed_elements_are_not_executed(self):
        browser = JobBrowser(lambda: None)
        with self.assertRaises(AssistantError) as caught:
            browser._element("invented", "click")
        self.assertEqual(caught.exception.status, 409)
        expected = {"connected": True, "visible": True}
        handle = SimpleNamespace(evaluate=lambda _: {**expected, "connected": False})
        browser._elements["o1-e1"] = (handle, expected)
        with self.assertRaises(AssistantError) as caught:
            browser._element("o1-e1", "click")
        self.assertEqual(caught.exception.status, 409)

    def test_policy_blocks_are_not_network_errors_and_real_failure_clears_pending(self):
        browser = JobBrowser(lambda: None, scope=BrowserScope("https://catalog.example"))
        request = SimpleNamespace(url="https://catalog.example/api/data", method="POST", resource_type="xhr", post_data='{}', headers={}, failure="net::ERR_CONNECTION_RESET")
        browser._request_started(request)
        browser._network_failure(request)
        self.assertFalse(browser._pending_data)
        self.assertTrue(browser.data_errors)
        blocked = Mock()
        blocked.request = SimpleNamespace(url="https://catalog.example/api/sendMail", method="POST", resource_type="xhr", post_data='{}', headers={})
        previous = list(browser.network_errors)
        browser._route(blocked)
        blocked.abort.assert_called_once()
        self.assertEqual(browser.network_errors, previous)

    def test_navigation_scope_is_exact_and_read_only(self):
        for path in ("/experienced/", "/experienced/position", "/experienced/position/123/detail"):
            self.assertTrue(navigation_url("https://" + SITE + path).startswith("https://" + SITE))
        for url in ("http://" + SITE + "/experienced", "https://127.0.0.1/experienced",
                    "https://" + SITE + ".evil.invalid/experienced", "https://" + SITE + ":444/experienced",
                    "https://secret@" + SITE + "/experienced", "https://" + SITE + "/experienced/login",
                    "https://" + SITE + "/experienced/apply"):
            with self.subTest(url=url), self.assertRaises(AssistantError):
                navigation_url(url)

    def test_actual_search_navigation_empty_filters_and_current_are_bounded(self):
        url = "https://" + SITE + "/experienced/position"
        query = "?keywords=算法&category=&location=&project=&type=&job_hot_flag=&current=2&limit=10&functionCategory=&tag="
        self.assertIn("current=2", navigation_url(url + query))
        for query in ("?current=21", "?location=上海", "?keywords=图神经网络", "?limit=100", "?tag=synthetic"):
            with self.subTest(query=query):
                self.assertEqual(navigation_url(url + query), url + query)
        self.assertTrue(navigation_url("https://" + SITE + "/campus/position"))

    def test_generic_post_bounds_and_write_guards_not_site_pagination_schema(self):
        url = "https://" + SITE + SEARCH_PATH
        self.assertTrue(allowed_request(url, "POST", "fetch", search_body()))
        for changes in ({"upload": "resume"}, {"password": "synthetic"}, {"action": "sendMail"}, {"updateProfile": {}}):
            with self.subTest(changes=changes):
                self.assertFalse(allowed_request(url, "POST", "fetch", search_body(**changes)))
        self.assertTrue(allowed_request("https://" + SITE + "/api/v1/csrf/token", "POST", "fetch", search_body()))
        for path in ("/api/v1/job/apply", "/api/v1/mail/send", "/api/sendMail", "/api/createOrder"):
            self.assertFalse(allowed_request("https://" + SITE + path, "POST", "fetch", search_body()))
        self.assertFalse(allowed_request(url, "DELETE", "fetch"))
        self.assertFalse(allowed_request(url, "POST", "fetch", "bad JSON"))
        self.assertTrue(allowed_request(url, "POST", "fetch", search_body(keyword="图神经网络 上海", offset=250)))
        self.assertTrue(allowed_request(url, "POST", "fetch", search_body(portal_type=3, new_filter=["synthetic"])))
        self.assertTrue(allowed_request(url, "POST", "fetch", search_body(keyword="x" * 1001, limit=1000)))
        self.assertFalse(allowed_request(url, "POST", "fetch", 'x' * 65537))
        self.assertFalse(allowed_request(url, "POST", "fetch", search_body(keyword="算法" * 12000)))
        self.assertTrue(allowed_request(url, "POST", "fetch", 'q=mutation+in+biology'))
        self.assertTrue(allowed_request(url, "POST", "fetch", '删除相关资料', content_type="text/plain"))
        self.assertTrue(allowed_request(url, "POST", "fetch", 'query Jobs { jobs { title } }', content_type="application/graphql"))
        self.assertFalse(allowed_request(url, "POST", "fetch", '# comment\nmutation Jobs { createJob }', content_type="application/graphql"))
        self.assertFalse(allowed_request(url, "POST", "fetch", 'query=mutation+Jobs+%7BcreateJob%7D'))
        self.assertFalse(allowed_request(url, "POST", "fetch", '{}', content_type="multipart/form-data; boundary=test"))
        self.assertFalse(allowed_request("https://" + SITE + "/api/v1/job/apply", "GET", "script"))

    def test_assets_cannot_be_navigation_or_writes(self):
        host = sorted(ASSET_HOSTS)[0]
        scope = BrowserScope(resource_domains=[host])
        self.assertTrue(allowed_request("https://" + host + "/app.js", "GET", "script", scope=scope))
        self.assertFalse(allowed_request("https://" + host + "/app.js", "GET", "document", scope=scope))
        self.assertFalse(allowed_request("https://" + host + "/api/query", "POST", "fetch", "{}", scope))
        self.assertEqual(BrowserScope().resource_domains, frozenset({SITE}))
        for url in ("https://127.0.0.1/app.js", "https://10.0.0.1/app.js", "file:///etc/passwd",
                    "https://evil.invalid/app.js", "https://" + SITE + "/api/v1/user/login"):
            self.assertFalse(allowed_request(url, "GET", "fetch"))

    def test_generic_initialization_and_status_queries_but_no_credentials(self):
        url = "https://" + SITE + "/api/session/init"
        self.assertTrue(allowed_request(url, "POST", "xhr", '{"portal_entrance":1}'))
        self.assertTrue(allowed_request(url, "POST", "xhr", '{"portal_entrance":2}'))
        self.assertTrue(allowed_request(url, "POST", "xhr", '{}'))
        for data in ({"portal_entrance": 1, "login": True}, {"password": "synthetic"}):
            self.assertFalse(allowed_request(url, "POST", "xhr", json.dumps(data)))
        self.assertFalse(allowed_request(url + "?login=true", "POST", "xhr", '{"portal_entrance":1}'))
        self.assertTrue(allowed_request("https://" + SITE + "/api/isLogin", "POST", "xhr", None))

    def test_site_generated_search_query_is_bounded_and_matches_body(self):
        url = "https://" + SITE + SEARCH_PATH
        query = "?limit=10&offset=0&portal_type=2&portal_entrance=1&_signature=_02B4.example-123"
        self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body()))
        query = ("?keyword=&limit=10&offset=0&job_category_id_list=&tag_id_list=&location_code_list="
                 "&subject_id_list=&recruitment_id_list=&portal_type=2&job_function_id_list="
                 "&storefront_id_list=&portal_entrance=1&_signature=example")
        self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body(keyword="")))
        self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body()))
        self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body(keyword="", tag_id_list=["123"])))
        self.assertTrue(allowed_request(url + "?tag_id_list=123", "POST", "xhr", search_body(tag_id_list=["123"])))
        for query in ("?limit=100", "?offset=10", "?portal_type=3", "?portal_entrance=2", "?_signature=site-generated"):
            self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body()))
        for query in ("?upload=resume", "?action=delete"):
            with self.subTest(query=query):
                self.assertFalse(allowed_request(url + query, "POST", "xhr", search_body()))

    def test_dns_pinning_rejects_private_reserved_and_mixed_answers(self):
        for ips in (["127.0.0.1"], ["198.18.0.71"], ["10.0.0.1"], ["::1"], ["8.8.8.8", "127.0.0.1"]):
            answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]
            with self.subTest(ips=ips), patch("assistant.browser.socket.getaddrinfo", return_value=answer):
                with self.assertRaises(AssistantError):
                    resolver_rules({SITE})
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("assistant.browser.socket.getaddrinfo", return_value=answer):
            rules = resolver_rules({SITE})
        self.assertIn("MAP " + SITE + " 8.8.8.8", rules)
        self.assertIn("MAP * ~NOTFOUND", rules)

    def test_opt_in_doh_uses_fixed_endpoint_and_still_rejects_private_answers(self):
        for ip, allowed in (("8.8.8.8", True), ("127.0.0.1", False)):
            envelope = {"Status": 0, "Answer": [{"type": 1, "data": ip}]}
            response = io.BytesIO(json.dumps(envelope).encode())
            with patch("assistant.browser.build_opener") as opener:
                opener.return_value.open.return_value = response
                if allowed:
                    resolver_rules({SITE}, "google")
                else:
                    with self.assertRaises(AssistantError):
                        resolver_rules({SITE}, "google")
                request = opener.return_value.open.call_args.args[0]
                self.assertTrue(request.full_url.startswith("https://dns.google/resolve?"))
                self.assertNotIn("Authorization", request.headers)
