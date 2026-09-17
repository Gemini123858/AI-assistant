import io
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from dotenv import dotenv_values

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.llm import APIModel, DemoModel, NoRedirect


def response(content=None, finish="stop"):
    value = {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content or {"reply": "合成回答", "cited_ids": []})}}]}
    return io.BytesIO(json.dumps(value).encode())


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(mode="api", api_key="synthetic-secret", extra_body={"thinking": {"type": "disabled"}})
        self.model = APIModel(self.config)

    def test_wire_format_and_no_tools(self):
        with patch.object(self.model.opener, "open", return_value=response()) as opened:
            self.assertEqual(self.model.complete([{"role": "user", "content": "JSON"}])["reply"], "合成回答")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.moonshot.cn/v1/chat/completions")
        body = json.loads(request.data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)
        self.assertEqual(body["thinking"]["type"], "disabled")

    def test_http_errors_do_not_leak_upstream_body(self):
        error = HTTPError("https://example.invalid", 401, "synthetic-secret", {}, io.BytesIO(b"synthetic-secret"))
        with patch.object(self.model.opener, "open", side_effect=error):
            with self.assertRaises(AssistantError) as caught:
                self.model.complete([])
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    def test_timeout_is_not_automatically_retried(self):
        with patch.object(self.model.opener, "open", side_effect=URLError("synthetic-secret")) as opened:
            with self.assertRaises(AssistantError):
                self.model.complete([])
        self.assertEqual(opened.call_count, 1)

    def test_truncation_and_malformed_json_are_rejected(self):
        for result in (response(finish="length"), io.BytesIO(b"{malformed"), response(content=[1])):
            with self.subTest(result=result), patch.object(self.model.opener, "open", return_value=result):
                with self.assertRaises(AssistantError):
                    self.model.complete([])

    def test_redirect_is_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_extra_body_cannot_override_messages_or_budget(self):
        for extra in ({"messages": []}, {"max_tokens": 1000000}, []):
            with self.subTest(extra=extra), self.assertRaises(AssistantError):
                Config(extra_body=extra).validate()

    def test_insecure_or_credential_bearing_api_base_is_rejected(self):
        for url in ("http://example.invalid", "https://user:secret@example.invalid", "https://example.invalid?key=secret"):
            with self.subTest(url=url), self.assertRaises(AssistantError):
                Config(base_url=url).validate()

    def test_demo_is_explicitly_nonsemantic(self):
        result = DemoModel().complete([{"content": json.dumps({"notification": {"text": "通知"}})}])
        self.assertTrue(result["summary"].startswith("[演示]"))
        self.assertEqual(result["times"], [])

    def test_public_environment_template_contains_no_key(self):
        values = dotenv_values(Path(__file__).parents[1] / ".env.example")
        # Boolean assertion: never display the offending secret on failure.
        self.assertTrue(not values.get("ASSISTANT_API_KEY"), "公开模板不能包含真实 Key。")
