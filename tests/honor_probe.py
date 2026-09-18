"""Free, anonymous Honor browser probe; no account, model or application calls."""

import json
import time
import uuid

from assistant.browser import BrowserScope, JobBrowser, honor_job_read, is_detail
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
            if honor_job_read(response.url):
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

    with ProbeBrowser(check, scope=BrowserScope(ENTRY)) as browser:
        page = browser.read(ENTRY)
        if not page["ready"]:
            page = browser.observe(wait_seconds=15)
        record("listing", page)
        assert page["ready"] and any((item.get("jobs") or 0) > 0 for item in reads), "No verified public jobs"
        search = next(element for element in page["elements"] if "fill" in element["actions"])
        page = browser.fill(search["id"], "软件", True)
        record("search", page)
        assert page["ready"], "Search not verified"
        # Only a real observed public detail link is eligible, never a guessed ID.
        detail = next((link for link in page["links"] if is_detail(link["url"])), None)
        if detail:
            page = browser.read(detail["url"])
            record("detail", page)
            assert page["ready"] and "listPositionDetail" in browser._honor_verified
        else:
            names = reads[-1]["job_names"]
            card = next(element for element in page["elements"]
                        if element["label"].split("\n", 1)[0] in names and "click" in element["actions"])
            page = browser.click(card["id"])
            record("detail", page)
            assert page["ready"] and "listPositionDetail" in browser._honor_verified
    print("Public probe finished; no model, login or submissions.", flush=True)


if __name__ == "__main__":
    main()
