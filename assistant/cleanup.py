"""Offline data cleanup. Preview by default; never contact a model or mailbox."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil

from dotenv import load_dotenv

from .errors import AssistantError


CATEGORIES = {
    "conversations": "会话与旧聊天",
    "drafts": "草稿及发送记录",
    "notices": "通知卡片",
    "tasks": "网页任务及截图",
    "mail": "本地邮件及同步状态",
    "texts": "导入原文及分析",
    "profile": "用户画像及修改历史",
    "rules": "纠正规则及修改历史",
    "identity": "助手名称与用户昵称",
    "usage": "模型调用计数",
}


@contextmanager
def locked_data_dir(directory):
    root = Path(directory).absolute()
    if root.is_symlink() or root.resolve() in {Path("/"), Path.home().resolve(), Path.cwd().resolve(), Path(__file__).resolve().parent.parent}:
        raise AssistantError("请选择应用的专用数据目录，不能清理项目目录、主目录或根目录。")
    lock_path = root / ".writer.lock"
    if not root.is_dir() or not lock_path.is_file():
        raise AssistantError("未找到应用数据目录或.writer.lock；请核对--data-dir，不会创建或清理陌生目录。")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AssistantError("应用仍在使用数据目录，请先停止服务再清理。", 409) from None
        try:
            yield root
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def memory_paths(root, kinds):
    memory = root / "memory"
    if memory.is_symlink() or (memory / "history").is_symlink():
        raise AssistantError("记忆目录不能是符号链接，未执行清理。")
    paths = [memory / (kind + ".md") for kind in kinds]
    histories = set()
    for path in (memory / "history").glob("*.json"):
        if path.is_symlink():
            raise AssistantError("记忆历史不能是符号链接，未执行清理。")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["kind"] in kinds:
            paths.append(path)
            histories.add(path.stem)
    pending = root / ".memory-pending.json"
    if pending.exists():
        if pending.is_symlink():
            raise AssistantError("记忆恢复标记不能是符号链接，未执行清理。")
        revision = json.loads(pending.read_text(encoding="utf-8"))["revision"]
        if revision in histories:
            paths.append(pending)
        elif not (memory / "history" / (revision + ".json")).is_file():
            raise AssistantError("记忆恢复标记缺少对应历史，请先恢复备份或使用全部清理。")
    return paths


def cleanup_plan(root, categories, *, all_data=False, keep_identity=False):
    selected = set(CATEGORIES) if all_data else set(categories)
    if keep_identity:
        selected.discard("identity")
    paths = {
        "conversations": [root / name for name in ("conversations", "chat.json", "conversation-migration.json")],
        "drafts": [root / "drafts"], "notices": [root / "notices"],
        "tasks": [root / "tasks", root / "screenshots"], "mail": [root / "mail"],
        "texts": [root / "messages"], "identity": [root / "identity.json"], "usage": [root / "usage.json"],
    }
    if all_data:
        paths["profile"] = [root / "memory", root / ".memory-pending.json"]
        paths["rules"] = []
    else:
        for category, kinds in (("profile", {"public_profile", "profile"}), ("rules", {"rules"})):
            if category in selected:
                paths[category] = memory_paths(root, kinds)
    plan = [("用户画像、纠正规则及修改历史" if all_data and name == "profile" else CATEGORIES[name],
             [p for p in paths[name] if p.exists() or p.is_symlink()])
            for name in CATEGORIES if name in selected]
    if all_data:
        covered = {p.relative_to(root).parts[0] for _, items in plan for p in items}
        preserved = {".writer.lock", "browser-cache"} | ({"identity.json"} if keep_identity else set())
        plan.append(("其他运行数据、日志和调试目录", sorted(p for p in root.iterdir() if p.name not in covered | preserved)))
    return [(label, items) for label, items in plan if items]


def file_count(path):
    if not path.is_dir() or path.is_symlink():
        return 1
    count = 0
    for directory, folders, files in os.walk(path, followlinks=False):
        count += len(files) + sum((Path(directory) / name).is_symlink() for name in folders)
    return count


def apply_plan(plan):
    for _, paths in plan:
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="停止应用后清理本机数据。默认只预览；实际删除不可撤销，不操作远端邮箱。")
    parser.add_argument("--data-dir", help="默认读取项目.env的ASSISTANT_DATA_DIR，否则为data。")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="清理全部运行数据，保留.writer.lock和browser-cache。")
    selection.add_argument("--only", nargs="+", choices=CATEGORIES, metavar="类别", help="按类别清理：" + ", ".join(CATEGORIES))
    parser.add_argument("--keep-identity", action="store_true", help="全部清理时保留助手名称与昵称。")
    parser.add_argument("--apply", action="store_true", help="执行删除；不指定则仅预览。")
    parser.add_argument("--yes", action="store_true", help="与--apply一起使用，跳过交互确认。")
    args = parser.parse_args(argv)
    if args.yes and not args.apply:
        parser.error("--yes必须与--apply一起使用。")
    if args.keep_identity and not args.all:
        parser.error("--keep-identity仅用于--all；分项清理不要选择identity即可。")
    load_dotenv(Path.cwd() / ".env", override=False)
    directory = args.data_dir or os.getenv("ASSISTANT_DATA_DIR", "data")
    try:
        with locked_data_dir(directory) as root:
            plan = cleanup_plan(root, args.only or [], all_data=args.all, keep_identity=args.keep_identity)
            print(f"数据目录：{root}")
            for label, paths in plan:
                print(f"  {label}：{sum(file_count(path) for path in paths)} 个文件，{len(paths)} 个清理目标")
            if not plan:
                print("没有需要清理的数据。")
                return 0
            print("仅清理本地文件；项目根目录的.env及远端邮件不变，浏览器安装缓存保留。")
            if not args.all:
                print("未选择的记录及历史快照保持原样，可能保留已删资料的内容或失效引用；清空全部内容请用--all。")
            if args.all and not args.keep_identity:
                print("本次也会清除助手名称与昵称。")
            if args.all or (args.only and "mail" in args.only):
                print("邮箱同步状态将重置，重启后需在设置页重新启用同步。")
            print("浏览器内未发送文字和未保存草稿不属于服务器文件，需另行清理浏览器站点数据。")
            if not args.apply:
                print("仅预览，未删除。确认范围后添加--apply执行。")
                return 0
            if not args.yes and input("删除不可撤销，确认请输入 DELETE：").strip() != "DELETE":
                print("已取消，未删除。")
                return 1
            apply_plan(plan)
            print("清理完成，下次启动将重建必要的空目录和默认文件。")
            return 0
    except (AssistantError, OSError, ValueError, KeyError, TypeError, EOFError) as error:
        print(f"清理未完成：{error}。请检查数据目录后重试。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
