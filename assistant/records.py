"""Business records shared by tools, conversation snapshots and the record picker."""

import copy
import hashlib
import json

from .errors import AssistantError
from .storage import draft_sources, text_field


KINDS = ("notification", "mail", "task", "draft", "memory")


def record_ref(value):
    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        raise AssistantError("资料类型无效。")
    return {"kind": value["kind"], "id": text_field(value.get("id"), "资料标识", 128)}


class Records:
    def __init__(self, service, mail):
        self.service, self.store, self.mail = service, service.store, mail

    def raw(self, ref, *, fetch_body=False):
        ref = record_ref(ref)
        kind, key = ref["kind"], ref["id"]
        if kind == "notification":
            return self.store.get_message(key)
        if kind == "mail":
            return self.mail.read_body(key) if fetch_body else self.mail.get(key)
        if kind == "task":
            return self.store.get_task(key)
        if kind == "draft":
            return self.store.get_draft(key)
        if key not in {"public_profile", "profile", "rules"}:
            raise AssistantError("记忆类型无效。")
        return {"id": key, "content": self.store.memory()[key]}

    def read(self, ref, *, fetch_body=False):
        raw = self.raw(ref, fetch_body=fetch_body)
        ref = {"kind": ref["kind"], "id": raw["id"]}
        kind = ref["kind"]
        notes, sources = [], []
        if kind == "notification":
            original = raw["original"]
            title = (raw.get("analysis") or {}).get("summary") or original["text"][:80]
            data = {key: raw.get(key) for key in ("original", "analysis", "error", "coverage")}
            status, stamp = raw["status"], raw.get("analyzed_at") or raw["imported_at"]
            if original["attachments"]:
                notes.append("附件未解析")
        elif kind == "mail":
            title, status = raw["subject"], "analyzed" if raw["analysis"] else "saved"
            stamp = raw.get("analyzed_at") or raw["synced_at"]
            data = {key: raw.get(key) for key in ("subject", "sender", "date_raw", "text", "attachments",
                "body_truncated", "analysis", "triage", "self_forwarded_candidate", "profile_updates", "error")}
            if raw["text"] is None:
                notes.append("仅邮件头，尚未读取正文")
            if raw.get("body_truncated"):
                notes.append("正文已截断")
            if raw.get("attachments"):
                notes.append("附件未解析")
        elif kind == "task":
            title, status, stamp = raw.get("public_request", raw["request"]), raw["status"], raw["updated_at"]
            data = {key: copy.deepcopy(raw.get(key)) for key in ("request", "public_request", "scope", "result",
                "note", "phase", "calls", "steps", "limits", "errors", "origin")}
            data["pages"] = [{key: page.get(key) for key in ("id", "url", "title", "fetched_at", "ready", "text_truncated")}
                             for page in raw["pages"]]
            if status != "completed":
                notes.append("网页任务未完整完成，不能据此断言没有结果")
        elif kind == "draft":
            title, status, stamp = raw["subject"] or "（无主题草稿）", raw["status"], raw["updated_at"]
            data = {key: copy.deepcopy(raw.get(key)) for key in
                    ("from", "to", "cc", "bcc", "subject", "body", "version", "send", "error")}
            ref["version"] = raw["version"]
            sources = draft_sources(raw)
        else:
            title = {"public_profile": "公开检索画像", "profile": "私人画像", "rules": "纠正规则"}[ref["id"]]
            status, stamp, data = "saved", "", {"content": raw["content"]}
        snapshot = {"ref": ref, "title": title, "status": status, "updated_at": stamp,
                    "data": data, "source_refs": sources, "coverage": {"complete": not notes, "notes": notes}}
        snapshot["fingerprint"] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return snapshot

    def search(self, *, kind="all", query="", offset=0, limit=30):
        if kind not in (*KINDS, "all"):
            raise AssistantError("资料类型无效。")
        query = text_field(query, "搜索词", 300, True).casefold()
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise AssistantError("资料分页参数无效。")
        refs = []
        if kind in {"all", "notification"}:
            refs.extend({"kind": "notification", "id": item["id"]} for item in self.store.list_messages())
        if kind in {"all", "mail"}:
            refs.extend({"kind": "mail", "id": item["id"]} for item in self.mail.records())
        if kind in {"all", "task"}:
            refs.extend({"kind": "task", "id": item["id"]} for item in self.store.list_tasks())
        if kind in {"all", "draft"}:
            refs.extend({"kind": "draft", "id": item["id"]} for item in self.store.list_drafts())
        if kind == "memory":
            refs.extend({"kind": "memory", "id": name} for name in self.store.memory())
        found = []
        for ref in refs:
            snapshot = self.read(ref)
            if query and query not in json.dumps(snapshot, ensure_ascii=False).casefold():
                continue
            summary = {key: snapshot[key] for key in ("ref", "title", "status", "updated_at", "coverage")}
            if ref["kind"] == "mail":
                summary["sender"] = snapshot["data"]["sender"]
            found.append(summary)
        found.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"records": found[offset:offset + limit], "total": len(found), "offset": offset, "limit": limit}
