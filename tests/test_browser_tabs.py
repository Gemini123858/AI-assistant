"""Synthetic tab/interaction checks; no network, browser process or model calls."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from assistant.browser import BrowserScope, JobBrowser, element_actions
from assistant.errors import AssistantError


class BrowserTabTests(unittest.TestCase):
    def browser(self):
        browser = JobBrowser(lambda: None, scope=BrowserScope("https://catalog.example/", public_web=True))
        first, second = Mock(), Mock()
        first.url, second.url = "https://catalog.example/", "https://catalog.example/detail"
        for page in (first, second):
            page.is_closed.return_value = False
        browser.page = first
        browser.context = Mock()
        browser._cdp = None
        browser._attach_page(first)
        browser._cdp = browser._tab_cdps[first]
        browser._document_ok = True
        browser._attach_page(second)
        return browser, first, second

    def test_popup_is_retained_until_explicit_close_and_tab_state_is_isolated(self):
        browser, first, second = self.browser()
        second.close.assert_not_called()
        self.assertEqual([tab["tab_id"] for tab in browser.list_tabs()["tabs"]], ["t1", "t2"])
        response = SimpleNamespace(status=200, request=SimpleNamespace(
            resource_type="document", frame=second.main_frame, url=second.url, method="GET", post_data=None, headers={}))
        browser._page_event(second, browser._http_status, response)
        self.assertIs(browser.page, first)
        browser.network_errors.append({"target": "first.example", "type": "script", "code": "synthetic"})
        browser.observe = Mock(return_value={"ready": True})
        browser.switch_tab("t2")
        self.assertIs(browser.page, second)
        self.assertTrue(browser._document_ok)
        self.assertEqual(browser.network_errors, [])
        browser.close_tab("t2")
        second.close.assert_called_once()
        first.close.assert_not_called()
        self.assertIs(browser.page, first)
        self.assertEqual(browser.network_errors[0]["target"], "first.example")
        with self.assertRaises(AssistantError):
            browser.switch_tab("t2")

    def test_text_click_uses_observed_text_and_normal_browser_event_dispatch(self):
        browser, first, _ = self.browser()
        browser._observed_text = "应用软件设计\n软件测试"
        target = Mock()
        target.is_visible.return_value = True
        frame = Mock()
        frame.get_by_text.return_value.count.return_value = 1
        frame.get_by_text.return_value.nth.return_value = target
        first.frames = [frame]
        browser.observe = Mock(return_value={"ready": True})
        browser.click_text("应用软件设计")
        frame.get_by_text.assert_called_once_with("应用软件设计", exact=True)
        target.click.assert_called_once_with(timeout=5000)
        with self.assertRaises(AssistantError):
            browser.click_text("未观察到的标题")

    def test_observation_recovers_after_last_tab_closes_itself(self):
        browser, first, second = self.browser()
        first.is_closed.return_value = True
        second.is_closed.return_value = True
        browser._tabs.clear()
        replacement = Mock()
        replacement.is_closed.return_value = False
        browser.context.new_page.return_value = replacement
        browser._ensure_page()
        self.assertIs(browser.page, replacement)
        self.assertEqual(browser.list_tabs()["active_tab"], "t3")

    def test_clicks_do_not_apply_label_path_or_element_type_policies(self):
        info = {"connected": True, "visible": True, "disabled": False,
                "tag": "div", "role": "button", "label": "应用软件设计", "href": ""}
        for label, href in (("删除账号的说明", "https://other.example/delete"),
                            ("提交", ""), ("", "javascript:void(0)")):
            self.assertEqual(element_actions({**info, "label": label, "href": href}, "https://catalog.example/"), ["click"])
        self.assertEqual(element_actions({**info, "disabled": True}, "https://catalog.example/"), [])


if __name__ == "__main__":
    unittest.main()
