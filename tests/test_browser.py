import io
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from assistant.browser import (ASSET_HOSTS, CSRF_PATH, SEARCH_PATH, SITE, JobBrowser,
                               allowed_request, navigation_url, resolver_rules)
from assistant.errors import AssistantError


def search_body(**changes):
    return json.dumps({"keyword": "算法", "limit": 10, "offset": 0,
                       "portal_type": 2, "portal_entrance": 1, **changes})


class BrowserPolicyTests(unittest.TestCase):
    def test_search_failure_is_not_empty_and_only_valid_retry_clears_response_error(self):
        browser = JobBrowser(lambda: None)
        url = "https://" + SITE + SEARCH_PATH
        failed = SimpleNamespace(url=url, status=405)
        browser._response(failed)
        self.assertFalse(browser.search_verified)
        self.assertIn("HTTP 405", browser.response_errors[SEARCH_PATH])
        good = SimpleNamespace(url=url, status=200, json=lambda: {"code": 0, "data": {"job_post_list": []}})
        # A foreign host must not validate a real search response.
        good.url = "https://" + sorted(ASSET_HOSTS)[0] + SEARCH_PATH
        browser._response(good)
        self.assertFalse(browser.search_verified)
        good.url = url
        browser.read_errors.append("Policy denial must remain visible")
        browser._response(good)
        self.assertTrue(browser.search_verified)
        self.assertFalse(browser.response_errors)
        self.assertEqual(browser.read_errors, ["Policy denial must remain visible"])
        browser._response(failed)
        self.assertFalse(browser.search_verified)

    def test_navigation_scope_is_exact_and_read_only(self):
        for path in ("/experienced/", "/experienced/position", "/experienced/position/123/detail"):
            self.assertTrue(navigation_url("https://" + SITE + path).startswith("https://" + SITE))
        for url in ("http://" + SITE + "/experienced", "https://127.0.0.1/experienced",
                    "https://" + SITE + ".evil.invalid/experienced", "https://" + SITE + ":444/experienced",
                    "https://secret@" + SITE + "/experienced", "https://" + SITE + "/experienced/login",
                    "https://" + SITE + "/experienced/apply", "https://" + SITE + "/campus/position",
                    "https://" + SITE + "/experienced/position?submit=true",
                    "https://" + SITE + "/experienced/position?page=0",
                    "https://" + SITE + "/experienced/position?page=1&page=2"):
            with self.subTest(url=url), self.assertRaises(AssistantError):
                navigation_url(url)

    def test_only_audited_search_post_is_allowed(self):
        url = "https://" + SITE + SEARCH_PATH
        self.assertTrue(allowed_request(url, "POST", "fetch", search_body()))
        for changes in ({"limit": 100}, {"offset": 200}, {"offset": True}, {"portal_type": 3},
                        {"upload": "resume"}, {"keyword": 123}, {"tag_id_list": [True]}):
            with self.subTest(changes=changes):
                self.assertFalse(allowed_request(url, "POST", "fetch", search_body(**changes)))
        for path in ("/api/v1/csrf/token", "/api/v1/job/apply", "/api/v1/mail/send"):
            self.assertFalse(allowed_request("https://" + SITE + path, "POST", "fetch", search_body()))
        self.assertFalse(allowed_request(url, "DELETE", "fetch"))
        self.assertFalse(allowed_request(url, "POST", "fetch", "bad JSON"))
        self.assertFalse(allowed_request(url, "POST", "fetch", search_body(keyword="private-profile-fragment")))
        self.assertFalse(allowed_request("https://" + SITE + "/api/v1/job/apply", "GET", "script"))

    def test_assets_cannot_be_navigation_or_writes(self):
        host = sorted(ASSET_HOSTS)[0]
        self.assertTrue(allowed_request("https://" + host + "/app.js", "GET", "script"))
        self.assertFalse(allowed_request("https://" + host + "/app.js", "GET", "document"))
        self.assertFalse(allowed_request("https://" + host + "/upload", "POST", "fetch", "{}"))
        for url in ("https://127.0.0.1/app.js", "https://10.0.0.1/app.js", "file:///etc/passwd",
                    "https://evil.invalid/app.js", "https://" + SITE + "/api/v1/user/mobile/login_status"):
            self.assertFalse(allowed_request(url, "GET", "fetch"))

    def test_anonymous_read_session_bootstrap_is_not_general_post_permission(self):
        url = "https://" + SITE + CSRF_PATH
        self.assertTrue(allowed_request(url, "POST", "xhr", '{"portal_entrance":1}'))
        for data in ({}, {"portal_entrance": 2}, {"portal_entrance": True},
                     {"portal_entrance": 1, "login": True}, {"password": "synthetic"}):
            self.assertFalse(allowed_request(url, "POST", "xhr", json.dumps(data)))
        self.assertFalse(allowed_request(url + "?login=true", "POST", "xhr", '{"portal_entrance":1}'))

    def test_site_generated_search_query_is_bounded_and_matches_body(self):
        url = "https://" + SITE + SEARCH_PATH
        query = "?limit=10&offset=0&portal_type=2&portal_entrance=1&_signature=_02B4.example-123"
        self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body()))
        query = ("?keyword=&limit=10&offset=0&job_category_id_list=&tag_id_list=&location_code_list="
                 "&subject_id_list=&recruitment_id_list=&portal_type=2&job_function_id_list="
                 "&storefront_id_list=&portal_entrance=1&_signature=example")
        self.assertTrue(allowed_request(url + query, "POST", "xhr", search_body(keyword="")))
        self.assertFalse(allowed_request(url + query, "POST", "xhr", search_body()))
        self.assertFalse(allowed_request(url + query, "POST", "xhr", search_body(keyword="", tag_id_list=["123"])))
        self.assertTrue(allowed_request(url + "?tag_id_list=123", "POST", "xhr", search_body(tag_id_list=["123"])))
        for query in ("?limit=100", "?offset=10", "?portal_type=3", "?portal_entrance=2",
                      "?upload=resume", "?keyword=private", "?limit=10&limit=10",
                      "?_signature=", "?_signature=" + "x" * 2049, "?_signature=http%3A%2F%2Fevil.invalid"):
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
