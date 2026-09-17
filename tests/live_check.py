"""Explicit, paid, synthetic-data integration check. Not discovered by unittest."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.service import Service
from assistant.storage import Store

from .test_storage import notification


def main():
    config = Config.from_env()
    if config.mode != "api":
        raise AssistantError("真实联调需要 ASSISTANT_LLM_MODE=api 及 Key 配置。")
    root = Path("data/live-check") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    store = Store(root)
    service = Service(store, config)
    report = {"schema_version": 1, "model": config.model, "status": "running", "checks": {}, "outputs": {}}
    try:
        store.update_memory("profile", "合成测试用户是通知涉及课程的学生，正在考虑参加活动。")
        first, _ = store.import_message(notification())
        result = service.analyze(first["id"])
        report["outputs"]["first"] = result
        kinds = {time["kind"] for time in result["analysis"]["times"]}
        report["checks"]["baseline_time_types"] = {"deadline", "event"} <= kinds
        calls = store.usage(service.day())["calls"]
        service.analyze(first["id"])
        report["checks"]["idempotent_analysis"] = store.usage(service.day())["calls"] == calls
        service.chat({"message": "/规则 报名截止时间必须分类为 deadline，不能当成 event；活动开始时间分类为 event。"})
        second, _ = store.import_message(notification(message_id="synthetic-live-002",
                    text="请在9月22日20:00前完成报名；活动安排在9月29日15:00开始。"))
        after = service.analyze(second["id"])
        report["outputs"]["after_correction"] = after
        times = after["analysis"]["times"]
        report["checks"]["new_notice_deadline"] = any(time["kind"] == "deadline" and "22" in time["text"] for time in times)
        report["checks"]["new_notice_event"] = any(time["kind"] == "event" and "29" in time["text"] for time in times)
        report["checks"]["memory_version_changed"] = result["analysis_context"]["memory_hash"] != after["analysis_context"]["memory_hash"]
        chat = service.chat({"message": "请为这条通知写一封询问活动地点的邮件文本草稿。不要发送，未知姓名使用占位符，并引用通知。",
                             "message_ids": [second["id"]]})
        report["outputs"]["chat_draft"] = chat
        report["checks"]["draft_reply_exists"] = bool(chat["reply"].strip())
        report["checks"]["draft_cites_notice"] = second["id"] in chat["cited_ids"]
        report["status"] = "passed" if all(report["checks"].values()) else "needs_review"
    except AssistantError as exc:
        report.update(status="failed", error=str(exc))
    finally:
        report["api_attempts"] = store.usage(service.day())["calls"]
        store._write_json(root.absolute() / "report.json", report)
        store.close()
    print(json.dumps({"status": report["status"], "checks": report["checks"],
                      "api_attempts": report["api_attempts"], "report": str(root / "report.json"),
                      "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssistantError as exc:
        raise SystemExit(str(exc)) from None
