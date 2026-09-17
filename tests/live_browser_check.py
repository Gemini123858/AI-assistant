"""Explicit, paid integration test. Synthetic profile, at most the approved calls.

Never invoked by unit tests/CI. Run only after the user authorizes this check.
"""

import argparse
import json
import re
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.llm import validate_tool_message
from assistant.service import Service
from assistant.storage import Store
from assistant.tasks import TASK_SYSTEM, TOOLS, Tasks, validate_result


PROFILE = "合成测试画像（不是用户事实）：会 Python，有机器学习课程项目。其他学历、经验、地点偏好和可入职时间未知。"


class TracedModel:
    """Private synthetic-test diagnostics only; never capture credentials/headers."""
    def __init__(self, service):
        self.service, self.delegate = service, service.model

    def complete_tools(self, messages, tools, timeout=None):
        attempt = self.service.store.usage(self.service.day())["calls"]
        result = self.delegate.complete_tools(messages, tools, timeout)
        self.service.store._write_json(self.service.store.root / f"model-attempt-{attempt}.json", result)
        return result


def review_saved(service, limit):
    """Independent evidence review, NOT resume or a passing browser integration."""
    records = service.store.list_tasks()
    if (len(records) != 1 or records[0]["status"] in {"queued", "running"}
            or records[0]["memory"]["profile"] != PROFILE
            or records[0]["memory"]["rules"] or service.store.memory()["profile"] != PROFILE):
        raise AssistantError("复核只接受本脚本生成的一份合成画像任务，不读取真实用户画像。")
    task = records[0]
    pages = [page for page in task["pages"] if page["kind"] == "detail" and page.get("ready", True)]
    if len(pages) < 2:
        raise AssistantError("缺少两份已验证的公开详情，不能复核。")
    messages = [{"role": "system", "content": TASK_SYSTEM}, {"role": "user", "content": json.dumps({
        "request": "独立复核保存的匿名公开岗位证据，不重新浏览，不恢复原任务。请针对这两份详情结合合成画像返回筛选结果。只用 finish_task。job_evidence 复制一段连续原文并保留换行；profile_evidence 从画像逐字复制，未知条件不能判为已满足。",
        "memory": task["memory"], "pages": pages}, ensure_ascii=False)}]
    while service.store.usage(service.day())["calls"] < limit:
        service.store.reserve_call(service.day(), limit)
        message = validate_tool_message(service.model.complete_tools(messages, [TOOLS[-1]], timeout=service.config.timeout))
        messages.append(message)
        calls = message["tool_calls"]
        if len(calls) != 1 or calls[0]["function"]["name"] != "finish_task":
            raise AssistantError("复核不执行浏览器动作。", 403)
        try:
            result = validate_result(json.loads(calls[0]["function"]["arguments"]), task)
            if len(result["jobs"]) < 2:
                raise AssistantError("复核结果未覆盖两份已读取详情。", 502)
        except AssistantError as error:
            print("Review citation rejected:", str(error), flush=True)
            messages.append({"role": "tool", "tool_call_id": calls[0]["id"],
                             "content": json.dumps({"failed": True, "error": str(error)}, ensure_ascii=False)})
            continue
        record = {
            "source_task_id": task["id"], "result": result,
            "note": "独立证据复核；原浏览任务状态未改变，不等同完整浏览联调通过。"}
        attempt = service.store.usage(service.day())["calls"]
        service.store._write_json(service.store.root / f"result-review-{attempt}.json", record)
        service.store._write_json(service.store.root / "result-review.json", record)
        print("Saved-evidence review passed:", len(result["jobs"]), "jobs; total paid attempts:",
              service.store.usage(service.day())["calls"], "/", limit, flush=True)
        return
    raise AssistantError("复核额度用完；未发布未验证结果。")


def popup_check(service):
    """One real model call chooses a generic action on a synthetic Chromium page.

    The executor does not decide which button to press. This is separate from
    successful handling of an actually observed promotion on the public site.
    """
    from .browser_task_smoke import LocalResponseBrowser
    from .test_tasks import LIST

    record = {"scenario": "synthetic promotion / real Kimi / real Chromium", "passed": False}
    with patch("assistant.browser.resolver_rules", return_value="MAP * ~NOTFOUND"):
        with LocalResponseBrowser(lambda: None) as browser:
            observation = browser.read(LIST)
            record["observation"] = observation
            messages = [{"role": "system", "content": TASK_SYSTEM}, {"role": "user", "content": json.dumps({
                "request": "合成浏览器动作测试：当前有岗位推荐推广弹层。请关闭弹层保留岗位页面，不体验推荐、不登录。只提出一个关闭弹层的点击动作，不访问其他链接。",
                "page": observation}, ensure_ascii=False)}]
            service.store.reserve_call(service.day(), service.config.daily_calls)
            try:
                message = validate_tool_message(service.model.complete_tools(messages, TOOLS,
                    timeout=service.config.timeout))
                record["model_message"] = message
                calls = message["tool_calls"]
                if len(calls) != 1 or calls[0]["function"]["name"] != "click_element":
                    raise AssistantError("模型没有选择单个通用点击动作。", 502)
                arguments = json.loads(calls[0]["function"]["arguments"])
                if set(arguments) != {"element_id"}:
                    raise AssistantError("点击参数不合法。", 502)
                after = browser.click(arguments["element_id"])
                record["after"] = after
                record["passed"] = "AI 岗位推荐" not in after["text"] and after["ready"]
            except AssistantError as error:
                record["error"] = str(error)
    service.store._write_json(service.store.root / "popup-check.json", record)
    print("Synthetic promotion + real Kimi generic click:", "passed" if record["passed"] else "not passed", flush=True)
    return record["passed"]


def main():
    parser = argparse.ArgumentParser(description="Explicit paid Kimi/browser integration check")
    parser.add_argument("--dns", choices=["system", "google"], default="system")
    parser.add_argument("--calls", type=int, choices=range(1, 9), default=6,
                        help="This run's authorized total model attempt limit (1–8)")
    parser.add_argument("--popup-check", action="store_true",
                        help="Spend one of the SAME call budget on a synthetic promotion with real Kimi")
    parser.add_argument("--review-id", help="Review an existing synthetic run ID; --calls is its CUMULATIVE cap")
    args = parser.parse_args()
    config = Config.from_env()
    if config.mode != "api":
        parser.error("需要明确 API 模式和私有环境配置；不会回退到演示模式。")
    if args.review_id and (not re.fullmatch(r"[a-f0-9]{32}", args.review_id) or args.popup_check):
        parser.error("复核需要合法合成测试标识，不能同时运行推广检查。")
    config.data_dir = Path("data/live-browser-check") / (args.review_id or uuid.uuid4().hex)
    if args.review_id and not config.data_dir.is_dir():
        parser.error("合成测试记录不存在，不会创建空复核记录。")
    config.browser_calls = config.daily_calls = args.calls
    config.browser_seconds = 300
    config.browser_dns = args.dns
    config.validate()
    store = Store(config.data_dir)
    tasks = None
    try:
        service = Service(store, config)
        service.model = TracedModel(service)
        if args.review_id:
            review_saved(service, args.calls)
            return
        store.update_memory("profile", PROFILE)
        popup_passed = popup_check(service) if args.popup_check else True
        spent = store.usage(service.day())["calls"]
        if spent >= args.calls:
            print("Call budget exhausted by promotion check; public-site task not started.", flush=True)
            raise SystemExit(1)
        config.browser_calls = args.calls - spent
        tasks = Tasks(service)
        task = tasks.start({"request": "这是合成测试。请从入口进入岗位列表，搜索算法，读取至少两份岗位详情。"
                                      "结合合成画像列出潜在匹配或待确认项，给出原文依据和缺失条件；不登录、不投递。"
                                      f"最多{config.browser_calls}次模型调用，留一次用于 finish_task；区分入口成功和后续失败，不笼统声称全部访问失败。"})
        deadline, previous = time.monotonic() + 315, None
        while time.monotonic() < deadline:
            tasks.wait(timeout=5)
            saved = store.get_task(task["id"])
            progress = (saved["status"], saved["calls"], len(saved["pages"]))
            if progress != previous:
                print("Progress: status / attempts / pages:", *progress, flush=True)
                previous = progress
            if saved["status"] not in {"queued", "running"}:
                break
        saved = store.get_task(task["id"])
        print("Integration status:", saved["status"], "model attempts:", saved["calls"],
              "pages:", len(saved["pages"]), flush=True)
        print("Private synthetic evidence directory:", str(config.data_dir), flush=True)
        print("Total paid attempts including promotion check:", store.usage(service.day())["calls"], "/", args.calls, flush=True)
        if (not popup_passed or saved["status"] != "completed" or not saved["result"]
                or len(saved["result"]["jobs"]) < 2):
            print("Check not passed:", saved["note"])
            raise SystemExit(1)
        print("Public-site + Kimi tool path passed; suitability quality still requires human review.")
    finally:
        if tasks:
            tasks.close()
        store.close()


if __name__ == "__main__":
    main()
