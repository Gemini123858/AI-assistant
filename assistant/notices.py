"""Editable notification cards, independent of source text and analysis."""

import copy
import hashlib
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import AssistantError
from .records import record_ref
from .recurrence import next_occurrence, weekly_rule
from .storage import text_field


FIELDS = {"title", "summary", "action", "start_at", "end_at", "deadline", "uncertainties", "source_refs", "recurrence"}
TIME_FIELDS = ("start_at", "end_at", "deadline")


class Notices:
    def __init__(self, records):
        self.records, self.store, self.config = records, records.store, records.service.config

    def fields(self, value, current=None):
        if not isinstance(value, dict) or set(value) - FIELDS:
            raise AssistantError("通知字段无效。")
        result = copy.deepcopy(current) if current else {"title": "", "summary": "", "action": "",
            "start_at": "", "end_at": "", "deadline": "", "uncertainties": [], "source_refs": []}
        for name, limit in (("title", 200), ("summary", 3000), ("action", 2000)):
            if name in value:
                result[name] = text_field(value[name], name, limit, name != "title")
        if not result["title"]:
            raise AssistantError("通知标题不能为空。")
        for name in TIME_FIELDS:
            if name not in value:
                continue
            stamp = text_field(value[name], name, 80, True)
            if stamp:
                try:
                    if len(stamp) == 10:
                        date.fromisoformat(stamp)
                    else:
                        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if instant.tzinfo is None or instant.utcoffset() is None:
                            raise ValueError()
                except ValueError:
                    raise AssistantError("通知时间须为YYYY-MM-DD日期或带时区ISO时间；未知时间留空。") from None
            result[name] = stamp
        if "recurrence" in value:
            rule = weekly_rule(value["recurrence"], self.config.timezone)
            if rule:
                result["recurrence"] = rule
            else:
                result.pop("recurrence", None)
        if result.get("recurrence") and any(result[key] for key in TIME_FIELDS):
            raise AssistantError("循环通知不能同时设置单次时间；转换时请将start_at、end_at、deadline清空。")
        if "uncertainties" in value:
            items = value["uncertainties"]
            if not isinstance(items, list) or len(items) > 20:
                raise AssistantError("待确认项须为不超过20项的列表。")
            result["uncertainties"] = [text_field(item, "待确认项", 500) for item in items]
        if "source_refs" in value:
            refs = value["source_refs"]
            if not isinstance(refs, list) or len(refs) > 30:
                raise AssistantError("通知来源须为不超过30项的列表。")
            checked = []
            for item in refs:
                if not isinstance(item, dict) or set(item) - {"kind", "id", "page_id", "quote", "url"}:
                    raise AssistantError("通知来源字段无效。")
                ref = record_ref(item)
                if ref["kind"] not in {"text", "mail", "task"}:
                    raise AssistantError("通知来源请选择邮件、导入文本或网页任务。")
                raw = self.records.raw(ref)
                source = dict(ref)
                if ref["kind"] == "task":
                    pages = raw["pages"]
                    if item.get("page_id"):
                        page = next((p for p in pages if p["id"] == item["page_id"]), None)
                        if page is None:
                            raise AssistantError("通知引用的网页不存在。")
                        pages = [page]
                        source.update(page_id=page["id"], url=page["url"])
                    text = "\n".join(p["text"] for p in pages)
                else:
                    text = raw["original"]["text"] if ref["kind"] == "text" else raw.get("text") or ""
                if item.get("quote"):
                    quote = text_field(item["quote"], "通知原文依据", 5000)
                    if quote not in text:
                        raise AssistantError("通知依据须逐字来自已保存原文；邮件正文未读取时请先读取。")
                    source["quote"] = quote
                if source not in checked:
                    checked.append(source)
            result["source_refs"] = checked
        return result

    def create(self, payload, *, actor="manual", origin=None):
        content = self.fields(payload)
        dedupe_key = None
        if content["source_refs"]:
            dedupe_key = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return self.store.create_notice(content, actor=actor, origin=origin, dedupe_key=dedupe_key)

    @staticmethod
    def version(payload):
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] < 1:
            raise AssistantError("操作通知需要当前版本号。")
        return payload["version"]

    def update(self, key, payload, *, actor="manual"):
        version = self.version(payload)
        record = self.store.get_notice(key)
        # Unchanged source links survive even when the original record was removed.
        content = self.fields(payload.get("changes"), record["content"])
        return self.store.change_notice(key, version, content=content, actor=actor)

    def set_deleted(self, key, payload, deleted, *, actor="manual"):
        return self.store.change_notice(key, self.version(payload), action="delete" if deleted else "restore", actor=actor)

    def list(self, view="all", query=""):
        if view not in {"all", "upcoming", "expired", "undated", "deleted"}:
            raise AssistantError("通知筛选无效。")
        query = text_field(query, "通知搜索词", 300, True).casefold()
        now = datetime.now(ZoneInfo(self.config.timezone))
        items = []
        for record in self.store.list_notices():
            if bool(record["deleted_at"]) != (view == "deleted"):
                continue
            content = record["content"]
            if query and query not in json.dumps(content, ensure_ascii=False).casefold():
                continue
            stamp = content["deadline"] or content["start_at"] or content["end_at"]
            day, instant, expired = None, None, False
            if content.get("recurrence"):
                stamp = next_occurrence(content["recurrence"], now)
                record["next_occurrence"] = stamp
                expired = not stamp
            if stamp:
                if len(stamp) == 10:
                    day = date.fromisoformat(stamp)
                    instant = datetime.fromisoformat(stamp).replace(tzinfo=now.tzinfo)
                    expired = day < now.date()
                else:
                    instant = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(now.tzinfo)
                    day, expired = instant.date(), instant < now
            if (view == "undated" and (day is not None or content.get("recurrence"))) or (view == "expired" and not expired):
                continue
            if view == "upcoming" and (day is None or expired or not now.date() <= day <= now.date() + timedelta(days=7)):
                continue
            items.append(((instant is None, expired, instant.timestamp() if instant else 0), record))
        items.sort(key=lambda pair: pair[0])
        return [record for _, record in items]

    def from_analysis(self, ref, index=0, section="analysis"):
        ref = record_ref(ref)
        raw = self.records.raw(ref)
        if type(index) is not int or index < 0:
            raise AssistantError("分析条目无效。")
        source = dict(ref)
        payload = {"source_refs": [source]}
        if ref["kind"] == "mail" and section == "events":
            items = (raw.get("analysis") or {}).get("events", [])
            if index >= len(items):
                raise AssistantError("未找到邮件分析事项。")
            event = items[index]
            payload.update(title=event["title"], summary=event["relevance"], action=event["action"], uncertainties=event["uncertainties"])
            source["quote"] = event["evidence"]
            stamp = event.get("normalized") or ""
            payload["deadline" if event["time_kind"] == "deadline" else "start_at"] = stamp
            if event["time_text"] and not stamp:
                payload["uncertainties"] = [*payload["uncertainties"], "时间待确认：" + event["time_text"]]
        elif ref["kind"] == "text" and section == "analysis":
            analysis = raw.get("analysis")
            if not analysis:
                raise AssistantError("请先分析这份文本。")
            payload.update(title=analysis["summary"][:200], summary=analysis["summary"], action="\n".join(analysis["actions"]), uncertainties=analysis["uncertainties"])
            source["quote"] = analysis["evidence"]
            for item in analysis["times"]:
                key = "deadline" if item["kind"] == "deadline" else "start_at"
                if item.get("normalized") and key not in payload:
                    payload[key] = item["normalized"]
                else:
                    payload["uncertainties"] = [*payload["uncertainties"], "原文时间：" + item["text"]]
        elif ref["kind"] == "task" and section in {"findings", "jobs"}:
            items = (raw.get("result") or {}).get(section, [])
            if index >= len(items):
                raise AssistantError("未找到网页分析条目。")
            item = items[index]
            source.update(page_id=item["page_id"], quote=item.get("quote") or item.get("job_evidence", ""))
            payload.update(title=(item.get("title") or item["text"])[:200], summary=item.get("text") or item["reason"], uncertainties=item["uncertainties"])
        else:
            raise AssistantError("该资料没有可转换的分析条目。")
        return payload
