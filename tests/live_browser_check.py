"""Explicit, paid integration test. Synthetic profile, at most the approved calls.

Never invoked by unit tests/CI. Run only after the user authorizes this check.
"""

import argparse
import json
import re
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.llm import validate_tool_message
from assistant.service import Service
from assistant.storage import Store
from assistant.tasks import TASK_SYSTEM, TOOLS, Tasks, validate_result
from .browser_fixtures import ASSET_HOSTS


PROFILE = "合成测试画像（不是用户事实）：会 Python，有机器学习课程项目。其他学历、经验、地点偏好和可入职时间未知。"


def browser_preflight():
    """Check the executable before spending any paid calls; no network access."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chromium",
                                          args=["--no-proxy-server"])
        try:
            print("Chromium preflight passed:", browser.version, flush=True)
        finally:
            browser.close()


class TracedModel:
    """Private synthetic-test diagnostics only; never capture credentials/headers."""
    def __init__(self, service):
        self.service, self.delegate = service, service.model
        self.budget_path = service.store.root / ".paid-budget.json"
        if not self.budget_path.exists():
            service.store._write_json(self.budget_path, {"schema_version": 1,
                "limit": service.config.daily_calls, "attempts": service.store.usage(service.day())["calls"]})
        if service.store._read_json(self.budget_path).get("limit") != service.config.daily_calls:
            raise AssistantError("不能在复用联调目录时改变累计授权上限；追加额度需单独确认。")

    def complete_tools(self, messages, tools, timeout=None):
        budget = self.service.store._read_json(self.budget_path)
        if type(budget.get("attempts")) is not int or not 0 <= budget["attempts"] < budget["limit"]:
            raise AssistantError("本轮累计付费尝试上限已到达；未请求模型。", 429)
        attempt = budget["attempts"] + 1
        self.service.store._write_json(self.budget_path, {**budget, "attempts": attempt})
        self.service.store._write_json(self.service.store.root / f"model-input-{attempt}.json",
                                      {"messages": messages, "tools": tools})
        try:
            result = self.delegate.complete_tools(messages, tools, timeout)
        except AssistantError as error:
            self.service.store._write_json(self.service.store.root / f"model-attempt-{attempt}.json",
                                          {"error": str(error), "failed": True})
            raise
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


def wait_task(tasks, store, key):
    deadline, previous = time.monotonic() + 315, None
    while time.monotonic() < deadline:
        tasks.wait(timeout=5)
        saved = store.get_task(key)
        progress = (saved["status"], saved["calls"], len(saved["pages"]))
        if progress != previous:
            print("Progress: status / attempts / pages:", *progress, flush=True)
            previous = progress
        if saved["status"] not in {"queued", "running"}:
            return saved
    tasks.cancel(key)
    tasks.wait(timeout=5)
    return store.get_task(key)


def scenario_passed(scenario, saved):
    result = saved["result"]
    if scenario == "network-stop":
        return bool(saved["status"] == "failed" and result and saved["errors"] and not saved["pages"]
                    and saved["calls"] < saved["limits"]["calls"])
    if saved["status"] != "completed" or not result:
        return False
    if scenario == "jobs":
        cited = {item["page_id"] for item in result.get("jobs", []) + result.get("findings", [])
                 if any(p["id"] == item["page_id"] and p["kind"] == "detail" for p in saved["pages"])}
        return len(cited) >= 2
    if scenario == "recover":
        return bool(saved["errors"] and any(p.get("ready") for p in saved["pages"]))
    if scenario == "vision":
        return any(a["tool"] == "take_screenshot" and a["status"] == "success" for a in saved["actions"])
    return bool(result.get("findings"))


def controlled_browser_factory(scenario):
    """Fault state survives model-selected restarts within this one test case."""
    from assistant.browser import BrowserScope, allowed_request
    from .browser_generic_smoke import LocalBrowser, START

    state = {"attempts": 0}

    class ControlledBrowser(LocalBrowser):
        def __init__(self, check):
            super().__init__(check, scope=BrowserScope(START))

        def _route(self, route):
            request = route.request
            if (scenario in {"recover", "network-stop"} and request.resource_type == "document"
                    and allowed_request(request.url, request.method, request.resource_type, scope=self.scope)):
                state["attempts"] += 1
                if scenario == "network-stop" or state["attempts"] == 1:
                    route.abort("addressunreachable")
                    return
            return super()._route(route)

    return ControlledBrowser


def main():
    parser = argparse.ArgumentParser(description="Explicit paid Kimi/browser integration check")
    parser.add_argument("--dns", choices=["system", "google"], default="system")
    parser.add_argument("--calls", type=int, choices=range(1, 201), required=True,
                        help="Explicit authorized total attempt cap (1–200); no default paid budget")
    parser.add_argument("--task-calls", type=int, choices=range(1, 201),
                        help="Optional task cap inside the SAME cumulative run budget")
    parser.add_argument("--run-id", help="Reuse one synthetic test directory and cumulative budget across scenarios")
    parser.add_argument("--scenario", choices=["jobs", "docs", "recover", "network-stop", "vision"], default="jobs")
    parser.add_argument("--job-urls", nargs=2,
                        help="Focused jobs check using two already observed public detail URLs, without a new search")
    parser.add_argument("--popup-check", action="store_true",
                        help="Spend one of the SAME call budget on a synthetic promotion with real Kimi")
    parser.add_argument("--review-id", help="Review an existing synthetic run ID; --calls is its CUMULATIVE cap")
    args = parser.parse_args()
    config = Config.from_env()
    if config.mode != "api":
        parser.error("需要明确 API 模式和私有环境配置；不会回退到演示模式。")
    if args.review_id and (not re.fullmatch(r"[a-f0-9]{32}", args.review_id) or args.popup_check):
        parser.error("复核需要合法合成测试标识，不能同时运行推广检查。")
    if args.run_id and (not re.fullmatch(r"[a-f0-9]{32}", args.run_id) or args.review_id):
        parser.error("run-id 需要合法合成测试标识，不能与 review-id 混用。")
    if args.job_urls and (args.scenario != "jobs" or args.review_id):
        parser.error("job-urls 仅用于独立岗位详情测试。")
    if args.job_urls:
        from assistant.browser import BrowserScope, navigation_url
        scope = BrowserScope(args.job_urls[0])
        for url in args.job_urls:
            navigation_url(url, scope)
    if not args.review_id:
        browser_preflight()
    config.data_dir = Path("data/live-browser-check") / (args.review_id or args.run_id or uuid.uuid4().hex)
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
        if (store.memory()["profile"] not in {"", PROFILE} or store.memory()["rules"]
                or any(t["memory"]["profile"] != PROFILE for t in store.list_tasks())):
            raise AssistantError("联调仅接受本脚本的合成画像，不读取真实用户数据。")
        store.update_memory("profile", PROFILE)
        popup_passed = popup_check(service) if args.popup_check else True
        spent = store._read_json(service.model.budget_path)["attempts"]
        if spent >= args.calls:
            print("Call budget exhausted by promotion check; public-site task not started.", flush=True)
            raise SystemExit(1)
        config.browser_calls = min(args.task_calls or args.calls, args.calls - spent)
        payload = {"start_url": "https://jobs.bytedance.com/experienced/position",
                           "public_request": "合成测试：查询算法研究/实习岗位，自主选择检索策略，至少尝试一个组合查询（例如算法 实习），必要时调整查询。访问第二页及至少两份不同详情；不登录、不投递。读取后 analyze_results 再结束。",
                           "request": "这是合成测试。请查询算法研究/实习岗位，读取至少两份岗位详情。"
                                      "结合合成画像列出潜在匹配或待确认项，给出原文依据和缺失条件；不登录、不投递。"
                                      f"最多{config.browser_calls}次模型调用，提前收尾并保留来源定位及纠正余量；区分入口成功和后续失败，不笼统声称全部访问失败。"}
        factory = None
        if args.job_urls:
            payload = {"start_url": args.job_urls[0],
                       "public_request": "读取下列两份已观察到的公开岗位详情，完成后 analyze_results 结合合成画像分析；不要求重新搜索或翻页，不登录、不投递：" + "\n".join(args.job_urls),
                       "request": "结合合成画像逐项说明这两份岗位的技能交集、硬性条件与优先项，未知条件保留。请给出实际岗位名称和可核查引用，不能把正式岗位直接等同必须有工作经验。"}
        if args.scenario == "docs":
            payload = {"start_url": "https://docs.python.org/3/library/urllib.parse.html",
                       "public_request": "查询 Python 官方 urllib.parse 文档：urljoin 如何解析相对 URL，基准 URL 的尾部斜线有何影响，以及绝对 URL 输入有哪些安全注意项？访问该文档的至少一个相关链接，记录实际来源，不访问其他域。用 findings 引用编号行，不需要画像分析。",
                       "request": "解释相对 URL 的尾部斜线差异及 urljoin 的安全注意项，只引用实际读取的来源。"}
        elif args.scenario in {"recover", "network-stop", "vision"}:
            from .browser_generic_smoke import START
            factory = controlled_browser_factory(args.scenario)
            payload = {"start_url": START, "public_request": "读取此公开资料目录，找出图结构资料及其说明。操作失败时根据工具事实自主恢复或停止，报告实际错误，不能编造根因。",
                       "request": "合成故障注入检查，不代表真实网站故障；请完成目录读取或有证据地报告阻塞。"}
            if args.scenario == "vision":
                config.browser_vision = True
                payload["public_request"] = "合成匿名网页视觉测试：请先 take_screenshot 查看网页布局，然后用 findings 引用正文描述可见搜索区和筛选区，不填写私人表单或点击发送/保存。"
        if args.scenario == "jobs":
            # Explicit resource scope for this fixture, not browser policy.
            payload["resource_domains"] = sorted(ASSET_HOSTS)
        tasks = Tasks(service, factory)
        print("Private synthetic evidence directory:", str(config.data_dir), flush=True)
        # Locally fulfilled fault/vision sites cannot resolve or reach the Internet.
        with patch("assistant.browser.resolver_rules", return_value="MAP * ~NOTFOUND") if factory else nullcontext():
            task = tasks.start(payload)
            saved = wait_task(tasks, store, task["id"])
        print("Integration status:", saved["status"], "model attempts:", saved["calls"],
              "pages:", len(saved["pages"]), flush=True)
        print("Private synthetic evidence directory:", str(config.data_dir), flush=True)
        print("Total cumulative paid attempts:", store._read_json(service.model.budget_path)["attempts"], "/", args.calls, flush=True)
        if not popup_passed or not scenario_passed(args.scenario, saved):
            print("Check not passed:", saved["note"])
            raise SystemExit(1)
        print("Scenario passed:", args.scenario, "; judgments still require human review.")
    finally:
        if tasks:
            tasks.close()
        store.close()


if __name__ == "__main__":
    main()
