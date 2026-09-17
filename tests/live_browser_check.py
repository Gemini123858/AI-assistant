"""Explicit, paid integration test. Synthetic profile, public jobs, at most six calls.

Never invoked by unit tests/CI. Run only after the user authorizes this check.
"""

import argparse
import time
import uuid
from pathlib import Path

from assistant.config import Config
from assistant.service import Service
from assistant.storage import Store
from assistant.tasks import Tasks


def main():
    parser = argparse.ArgumentParser(description="Explicit paid Kimi/browser integration check")
    parser.add_argument("--dns", choices=["system", "google"], default="system")
    parser.add_argument("--calls", type=int, choices=range(1, 7), default=6,
                        help="This run's authorized model attempt limit (1–6)")
    args = parser.parse_args()
    config = Config.from_env()
    if config.mode != "api":
        parser.error("需要明确 API 模式和私有环境配置；不会回退到演示模式。")
    config.data_dir = Path("data/live-browser-check") / uuid.uuid4().hex
    config.browser_calls = config.daily_calls = args.calls
    config.browser_seconds = 300
    config.browser_dns = args.dns
    config.validate()
    store = Store(config.data_dir)
    tasks = None
    try:
        store.update_memory("profile", "合成测试画像（不是用户事实）：会 Python，有机器学习课程项目。其他学历、经验、地点偏好和可入职时间未知。")
        tasks = Tasks(Service(store, config))
        task = tasks.start({"request": "这是合成测试。请从入口进入岗位列表，搜索算法，读取至少两份岗位详情。"
                                      "结合合成画像列出潜在匹配或待确认项，给出原文依据和缺失条件；不登录、不投递。"
                                      f"最多{args.calls}次模型调用，留一次用于 finish_task。"})
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
        if (saved["status"] != "completed" or not saved["result"]
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
