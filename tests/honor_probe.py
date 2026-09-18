"""Free, anonymous Honor browser probe; no account, model or application calls."""

import json
import time
import uuid

from assistant.browser import BrowserScope, JobBrowser, is_detail
from assistant.storage import Store


ENTRY = ("https://career.honor.com/SU60ee9e002f9d247b98da489e/mc/position/campus"
         "?projectId=101401%2C101601%2C101801%2C101901"
         "&recruitType=1&isLimitShowPostScope=1")


def main():
    store = Store("data/honor-probe/" + uuid.uuid4().hex)
    print("Private diagnostics:", store.root, flush=True)
    deadline = time.monotonic() + 120
    snapshots, reads = [], []

    def check():
        if time.monotonic() > deadline:
            raise RuntimeError("Public probe time budget exhausted")

    class ProbeBrowser(JobBrowser):
        def _response(self, response):
            super()._response(response)
            if "/wecruit/positionInfo/listPosition" in response.url:
                try:
                    body = response.json()
                    store._write(store.root / f"response-{len(reads)}.json", json.dumps(body, ensure_ascii=False))
                    data = body.get("data", {})
                    jobs = data.get("pageForm", {}).get("pageData")
                    reads.append({"target": self.target(response.url), "status": response.status,
                                  "state": body.get("state"), "data_fields": list(data),
                                  "jobs": len(jobs) if isinstance(jobs, list) else None,
                                  "job_names": [job.get("postName") for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []})
                except Exception:
                    reads.append({"target": self.target(response.url), "status": response.status})

    def record(label, page):
        snapshots.append({"label": label, **page})
        store._write(store.root / "snapshots.json", json.dumps(snapshots, ensure_ascii=False))
        store._write(store.root / "reads.json", json.dumps(reads, ensure_ascii=False))
        print(label, "ready:", page["ready"], "characters:", len(page["text"]), flush=True)
        print("reads:", json.dumps(reads, ensure_ascii=False), flush=True)
        print("text:", page["text"][:1500], flush=True)
        print("controls:", json.dumps(page["elements"][:30], ensure_ascii=False), flush=True)
        print("diagnostics:", json.dumps(browser.diagnostics(), ensure_ascii=False), flush=True)

    def settle(label, page):
        if not page["ready"]:
            page = browser.observe(wait_seconds=15)
        if not page["ready"] and page["network_errors"]:
            record(label + "-failed-first-load", page)
            page = browser.refresh()
            if not page["ready"]:
                page = browser.observe(wait_seconds=15)
        return page

    # Probe fixture dependencies are explicit scope input, not browser rules.
    resources = ["as.alipayobjects.com", "at.alicdn.com", "wecruit-cdn.hotjob.cn"]
    with ProbeBrowser(check, scope=BrowserScope(ENTRY, resource_domains=resources)) as browser:
        page = settle("listing", browser.read(ENTRY))
        record("listing", page)
        assert page["ready"] and any((item.get("jobs") or 0) > 0 for item in reads), "No verified public jobs"
        search = next(element for element in page["elements"] if "fill" in element["actions"])
        page = settle("search", browser.fill(search["id"], "软件", True))
        record("search", page)
        assert page["ready"], "Search not verified"
        # Only a real observed public detail link is eligible, never a guessed ID.
        detail = next((link for link in page["links"] if is_detail(link["url"])), None)
        if detail:
            page = settle("detail", browser.read(detail["url"]))
            record("detail", page)
            assert page["ready"] and "任职要求" in page["text"] and "工作职责" in page["text"]
        else:
            names = reads[-1]["job_names"]
            card = next(element for element in page["elements"]
                        if element["label"].split("\n", 1)[0] in names and "click" in element["actions"])
            page = settle("detail", browser.click(card["id"]))
            record("detail", page)
            assert page["ready"] and "任职要求" in page["text"] and "工作职责" in page["text"]
    print("Public probe finished; no model, login or submissions.", flush=True)


if __name__ == "__main__":
    main()
