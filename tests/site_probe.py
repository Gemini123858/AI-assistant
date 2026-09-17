"""Explicit public-site integration check: no model calls, accounts or form writes."""

import time
import argparse
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from assistant.browser import ENTRY, SEARCH_PATH, KEYWORDS, JobBrowser, allowed_request, navigation_url
from assistant.errors import AssistantError


class DiagnosticBrowser(JobBrowser):
    """Anonymous public-site metadata only; never dump signatures, headers or cookies."""

    snapshot = False

    def _snapshot(self):
        if not self.snapshot:
            return
        root = Path("data/site-probe") / uuid.uuid4().hex
        root.mkdir(mode=0o700, parents=True)
        self.page.screenshot(path=str(root / "page.png"), timeout=3000)
        # A fresh anonymous page only; no HTML, storage, cookies or request headers.
        observations = self.page.locator(
            'button, [role="button"], [role="dialog"], dialog').evaluate_all(
                "els => els.slice(0,80).map(e => ({tag:e.tagName,role:e.getAttribute('role'),"
                "text:e.innerText.slice(0,1500),visible:!!e.getClientRects().length}))")
        print("Public UI observations:", json.dumps(observations, ensure_ascii=False), flush=True)
        print("Anonymous screenshot:", str(root / "page.png"), flush=True)

    def __enter__(self):
        super().__enter__()
        self._failures = set()
        self.page.on("requestfailed", self._failed)
        self.page.on("pageerror", lambda error: print("Page JavaScript error:", error.message[:250], flush=True))
        self.page.on("response", lambda response: print(
            "HTTP error:", response.status, urlsplit(response.url).hostname,
            urlsplit(response.url).path, flush=True) if response.status >= 400 else None)
        return self

    def _failed(self, request):
        host = urlsplit(request.url).hostname
        key = (host, request.method, request.resource_type, request.failure)
        if key not in self._failures:
            self._failures.add(key)
            print("failed:", request.method, host, urlsplit(request.url).path,
                  request.resource_type, request.failure, flush=True)

    def _route(self, route):
        request = route.request
        if urlsplit(request.url).path == "/api/v1/csrf/token":
            print("Anonymous CSRF request metadata:", "method:", request.method,
                  "type:", request.resource_type, "query fields:",
                  [key for key, _ in parse_qsl(urlsplit(request.url).query, keep_blank_values=True)],
                  "body length:", len(request.post_data or ""), flush=True)
            try:
                body = json.loads(request.post_data)
                print("Anonymous CSRF body fields:", sorted(body), "public portal id:",
                      body.get("portal_entrance"), flush=True)
            except (ValueError, TypeError, AttributeError):
                pass
        if (urlsplit(request.url).path == SEARCH_PATH
                and not allowed_request(request.url, request.method, request.resource_type, request.post_data)):
            try:
                data = json.loads(request.post_data)
                metadata = {key: ({"count": len(value), "item_types": sorted({type(item).__name__ for item in value})}
                                  if isinstance(value, list) else
                                  value if type(value) is int or value in KEYWORDS else type(value).__name__)
                            for key, value in data.items()}
                print("Denied search field metadata:", metadata, "query fields:",
                      [key for key, _ in parse_qsl(urlsplit(request.url).query, keep_blank_values=True)],
                      "empty query segments:", urlsplit(request.url).query.split("&").count(""), flush=True)
                for key, value in parse_qsl(urlsplit(request.url).query, keep_blank_values=True):
                    if key == "_signature":
                        print("Signature metadata: length:", len(value), "unexpected characters:",
                              sorted(set(re.sub(r"[A-Za-z0-9._~+/=-]", "", value))), flush=True)
                    elif key in {"limit", "offset", "portal_type", "portal_entrance"}:
                        print("Mirrored numeric query:", key, repr(value[:40]), flush=True)
                    else:
                        print("Other query field:", key, "length:", len(value), flush=True)
            except (ValueError, TypeError, AttributeError):
                print("Denied search: invalid JSON object.", flush=True)
        super()._route(route)

    def _response(self, response):
        if urlsplit(response.url).path == SEARCH_PATH and response.status >= 400:
            try:
                body = response.text().lower()
                print("Public search error metadata:", "status:", response.status,
                      "content type:", response.headers.get("content-type", ""),
                      "known labels:", [label for label in ("csrf", "captcha", "verification", "anti-spam")
                                        if label in body], flush=True)
            except Exception:
                print("Public search error body could not be inspected.", flush=True)
        super()._response(response)

    def read(self, url):
        try:
            page = super().read(url)
            self._snapshot()
            return page
        except AssistantError:
            print("Public-page diagnostics:", "path:", urlsplit(self.page.url).path,
                  "title:", self.page.title(), "body characters:",
                  len(self.page.locator("body").inner_text()), "anchors:",
                  self.page.locator("a[href]").count(), "read errors:", self.read_errors, flush=True)
            for href in self.page.locator("a[href]").evaluate_all("els => els.map(e => e.href)"):
                try:
                    print("safe anchor:", navigation_url(href), flush=True)
                except AssistantError:
                    pass
            self._snapshot()
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dns", choices=["system", "google"], default="system")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--snapshot", action="store_true",
                        help="Save anonymous screenshots to ignored data/site-probe")
    parser.add_argument("--details", type=int, choices=range(1, 4), default=1,
                        help="Read 1–3 distinct observed public job details")
    parser.add_argument("--listing-only", action="store_true",
                        help="Skip the previously inspected homepage during list diagnostics")
    args = parser.parse_args()
    deadline = time.monotonic() + 90

    def check():
        if time.monotonic() >= deadline:
            raise AssistantError("官网探测达到90秒上限。")

    factory = DiagnosticBrowser if args.diagnostics or args.snapshot else JobBrowser
    with factory(check, args.dns) as browser:
        if isinstance(browser, DiagnosticBrowser):
            browser.snapshot = args.snapshot
        listing_url = "https://jobs.bytedance.com/experienced/position"
        if not args.listing_only:
            entry = browser.read(ENTRY)
            print("entry:", entry["url"], "safe links:", len(entry["links"]), flush=True)
            listing_url = next(link["url"] for link in entry["links"] if link["url"].endswith("/position"))
        listing = browser.read(listing_url)
        print("listing:", listing["url"], "safe links:", len(listing["links"]), flush=True)
        search = browser.read(browser.search_url("算法", 1))
        print("search:", search["url"], "safe links:", len(search["links"]), flush=True)
        detail_urls = list(dict.fromkeys(link["url"] for link in search["links"]
                                        if link["url"].endswith("/detail")))
        if len(detail_urls) < args.details:
            raise AssistantError("可观察到的详情链接不足，不能完成本次多详情检查。", 502)
        for detail_url in detail_urls[:args.details]:
            detail = browser.read(detail_url)
            assert "职位要求" in detail["text"]
            print("detail:", detail["url"], "characters:", len(detail["text"]),
                  "blocked requests:", detail["blocked_requests"], flush=True)
    print("Public-site read path passed; no login, model calls or submissions.")


if __name__ == "__main__":
    main()
