"""Explicit public-site integration check: no model calls, accounts or form writes."""

import time
import argparse

from assistant.browser import ENTRY, JobBrowser
from assistant.errors import AssistantError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dns", choices=["system", "google"], default="system")
    args = parser.parse_args()
    deadline = time.monotonic() + 90

    def check():
        if time.monotonic() >= deadline:
            raise AssistantError("官网探测达到90秒上限。")

    with JobBrowser(check, args.dns) as browser:
        entry = browser.read(ENTRY)
        print("entry:", entry["url"], "safe links:", len(entry["links"]), flush=True)
        listing_url = next(link["url"] for link in entry["links"] if link["url"].endswith("/position"))
        listing = browser.read(listing_url)
        print("listing:", listing["url"], "safe links:", len(listing["links"]), flush=True)
        search = browser.read(browser.search_url("算法", 1))
        print("search:", search["url"], "safe links:", len(search["links"]), flush=True)
        detail_url = next(link["url"] for link in search["links"] if link["url"].endswith("/detail"))
        detail = browser.read(detail_url)
        assert "职位要求" in detail["text"]
        print("detail:", detail["url"], "characters:", len(detail["text"]),
              "blocked requests:", detail["blocked_requests"], flush=True)
    print("Public-site read path passed; no login, model calls or submissions.")


if __name__ == "__main__":
    main()
