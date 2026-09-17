import io
import json
import socket
import unittest
from unittest.mock import patch

from assistant.browser import ASSET_HOSTS, SEARCH_PATH, SITE, allowed_request, navigation_url, resolver_rules
from assistant.errors import AssistantError


def search_body(**changes):
    return json.dumps({"keyword": "算法", "limit": 10, "offset": 0,
                       "portal_type": 2, "portal_entrance": 1, **changes})


class BrowserPolicyTests(unittest.TestCase):
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
