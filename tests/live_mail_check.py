"""Explicit paid Kimi check, SYNTHETIC IMAP only; never logs into an account."""
import json
import uuid
from dataclasses import replace
from pathlib import Path

from assistant.config import Config
from assistant.mail import Mail
from assistant.service import Service
from assistant.storage import Store
from .test_mail import FakeIMAP, TOKEN


class TracedModel:
    def __init__(self, service):
        self.service, self.delegate = service, service.model

    def request(self, messages, tools=None, timeout=None):
        store = self.service.store
        attempt = store.usage(self.service.day())["calls"]
        store._write_json(store.root/f"input-{attempt}.json", {"messages": messages, "tools": tools})
        result = self.delegate.complete(messages) if tools is None else self.delegate.complete_tools(messages, tools, timeout)
        store._write_json(store.root/f"output-{attempt}.json", result)
        return result

    def complete(self, messages):
        return self.request(messages)

    def complete_tools(self, messages, tools, timeout=None):
        return self.request(messages, tools, timeout)


def main():
    config = replace(Config.from_env(), data_dir=Path("data/live-mail-check")/uuid.uuid4().hex,
                     daily_calls=5, qq_imap_user="owner@qq.com", qq_imap_auth_code="synthetic",
                     access_token=TOKEN, chat_self_names="本人")
    config.validate()
    if config.mode != "api":
        raise SystemExit("Requires explicit API mode; this test spends at most 5 paid attempts.")
    store = Store(config.data_dir)
    service = Service(store, config)
    service.model = TracedModel(service)
    imap = FakeIMAP()
    mail = Mail(service, lambda: imap)
    try:
        mail.sync()
        imap.uids = [10, 11]
        mail.sync()
        mail.triage()
        record = mail.analyze(mail.records()[0]["id"])
        report = {"schema_version": 1, "note": "真实Kimi+合成IMAP，不代表QQ账号连通或真实邮件效果。",
                  "calls": store.usage(service.day())["calls"], "mail_id": record["id"],
                  "analysis": record["analysis"], "profile_updates": record["profile_updates"]}
        store._write_json(store.root/"check-report.json", report)
        print("Private synthetic report:", store.root/"check-report.json")
        print("Paid attempts:", report["calls"], "/ 5; events:", len(record["analysis"]["events"]))
        if not record["analysis"]["events"]:
            raise SystemExit("No events; inspect actual output, not a passing semantic result.")
    finally:
        mail.close()
        store.close()


if __name__ == "__main__":
    main()
