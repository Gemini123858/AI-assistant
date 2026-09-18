"""A single-writer file store; no database and no pluggable storage framework."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import AssistantError
from .llm import validate_analysis


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def text_field(value, name, maximum=12000, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise AssistantError(f"{name} 必须为不超过 {maximum} 字的文本。")
    value = value.strip()
    if not value and not allow_empty:
        raise AssistantError(f"{name} 不能为空。")
    return value


def normalize_message(payload):
    if not isinstance(payload, dict):
        raise AssistantError("通知必须是 JSON 对象。")
    original = {
        "source": text_field(payload.get("source", "manual"), "来源", 80),
        "conversation": text_field(payload.get("conversation"), "群/会话", 120),
        "sender": text_field(payload.get("sender", "未知"), "发送者", 120),
        "text": text_field(payload.get("text"), "通知正文"),
    }
    # Validate emptiness/size above, but retain exact source text for provenance.
    original["text"] = payload["text"]
    sent_at = text_field(payload.get("sent_at"), "原消息时间", 80)
    try:
        instant = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise AssistantError("原消息时间必须为带时区的 ISO 时间，例如 2026-09-16T10:00:00+08:00。") from exc
    original["sent_at"] = instant.astimezone(timezone.utc).isoformat()
    external_id = text_field(payload.get("message_id", ""), "原消息 ID", 200, True)
    original["message_id"] = external_id
    attachments = payload.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise AssistantError("附件元信息必须是不超过 20 项的文本列表。")
    original["attachments"] = [text_field(item, "附件描述", 500) for item in attachments]
    # Timestamp is required: identical words at different times are not duplicates.
    identity = ([original["source"], original["conversation"], external_id] if external_id else
                [original[key] for key in ("source", "conversation", "sender", "sent_at", "text")])
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return key, original


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if self.root.resolve() in {Path.cwd().resolve(), Path.home().resolve(), Path("/")}:
            raise AssistantError("请使用专用数据子目录，不能将工作区、用户主目录或根目录作为数据目录。", 500)
        self._mutex = threading.RLock()
        self._lock_file = None
        self._closed = False
        for directory in (self.root, self.root / "messages", self.root / "memory",
                          self.root / "memory" / "history", self.root / "tasks"):
            if directory.is_symlink():
                raise AssistantError("数据目录不能是符号链接。", 500)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_uid != os.getuid():
                raise AssistantError("数据目录必须属于当前用户。", 500)
            # Dedicated application directories only; protect on shared servers.
            directory.chmod(0o700)
        descriptor = os.open(self.root / ".writer.lock",
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._lock_file = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise AssistantError("已有实例正在使用该数据目录，请勿启动第二个写入进程。", 409) from exc
        try:
            self._recover_memory()
            for kind in ("profile", "rules"):
                path = self.root / "memory" / f"{kind}.md"
                if not path.exists():
                    self._write(path, "")
        except Exception:
            self.close()
            raise

    def close(self):
        with self._mutex:
            self._closed = True
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None

    def _ensure_open(self):
        if self._closed:
            raise AssistantError("存储已关闭。", 503)

    @staticmethod
    def _sync_directory(directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, path, content):
        self._ensure_open()
        if path.is_symlink():
            raise AssistantError("数据文件不能是符号链接。", 500)
        descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write_json(self, path, value):
        self._write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _read_text(self, path):
        self._ensure_open()
        if path.is_symlink():
            raise AssistantError("数据文件不能是符号链接。", 500)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise AssistantError(f"无法读取数据文件 {path.name}，请检查或从备份恢复。", 500) from exc

    def _read_json(self, path, versions=(1,)):
        try:
            data = json.loads(self._read_text(path))
        except json.JSONDecodeError as exc:
            raise AssistantError(f"数据文件 {path.name} 已损坏，不会自动覆盖。", 500) from exc
        if not isinstance(data, dict) or data.get("schema_version") not in versions:
            raise AssistantError(f"数据文件 {path.name} 的格式版本不受支持。", 500)
        return data

    def _message_path(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise AssistantError("通知标识无效。")
        return self.root / "messages" / f"{key}.json"

    def _validate_record(self, record, key):
        try:
            expected, original = normalize_message(record["original"])
            if expected != key or record.get("id") != key or original != record["original"]:
                raise ValueError("identity mismatch")
            if record.get("status") not in {"pending", "success", "failed"}:
                raise ValueError("invalid status")
            if record["status"] == "success" and not isinstance(record.get("analysis"), dict):
                raise ValueError("missing analysis")
            if record.get("analysis") is not None:
                record["analysis"] = validate_analysis(record["analysis"], original)
            # These optional envelope fields have backwards-compatible defaults.
            record.setdefault("analysis", None)
            record.setdefault("error", None)
            record.setdefault("coverage", "text_only")
            return record
        except (KeyError, ValueError, AssistantError) as exc:
            raise AssistantError(f"通知文件 {key}.json 内容不合法，不会自动覆盖。", 500) from exc

    def import_message(self, payload):
        key, original = normalize_message(payload)
        with self._mutex:
            path = self._message_path(key)
            if path.exists():
                record = self.get_message(key)
                if record["original"] != original:
                    raise AssistantError("同一消息身份对应不同原文或元信息，请核对；原记录未覆盖。", 409)
                return record, False
            record = {"schema_version": 1, "id": key, "original": original,
                      "status": "pending", "analysis": None, "error": None,
                      "coverage": "text_only", "imported_at": now_iso()}
            self._write_json(path, record)
            return record, True

    def get_message(self, key):
        with self._mutex:
            path = self._message_path(key)
            if not path.exists():
                raise AssistantError("通知不存在。", 404)
            return self._validate_record(self._read_json(path), key)

    def list_messages(self):
        with self._mutex:
            self._ensure_open()
            records = [self.get_message(path.stem) for path in (self.root / "messages").glob("*.json")]
            return sorted(records, key=lambda item: item["original"]["sent_at"], reverse=True)

    def save_result(self, key, analysis=None, error=None, context=None):
        with self._mutex:
            record = self.get_message(key)
            record.update(status="failed" if error else "success", analysis=analysis,
                          error=error, analyzed_at=now_iso(), analysis_context=context)
            self._write_json(self._message_path(key), record)
            return record

    def memory(self):
        with self._mutex:
            self._recover_memory()
            return {kind: self._read_text(self.root / "memory" / f"{kind}.md")
                    for kind in ("profile", "rules")}

    def _task_path(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise AssistantError("任务标识无效。")
        return self.root / "tasks" / f"{key}.json"

    def create_task(self, request, portal, mode, memory, limits, scope=None, public_request=None, private_conditions=None):
        with self._mutex:
            key = uuid.uuid4().hex
            record = {"schema_version": 2 if scope else 1, "id": key, "request": request, "portal": portal,
                      "mode": mode, "memory": memory, "limits": limits, "status": "queued",
                      "created_at": now_iso(), "updated_at": now_iso(), "cancel_requested": False,
                      "calls": 0, "steps": 0, "pages": [], "actions": [], "errors": [], "result": None,
                      "note": "仅限授权的公开网页读取；不登录、不发送或提交。"}
            if scope:
                record.update(scope=scope, public_request=public_request, phase="browsing", stop_reason="")
                if private_conditions is not None:
                    record["private_conditions"] = private_conditions
            self._write_json(self._task_path(key), record)
            return record

    def get_task(self, key):
        with self._mutex:
            path = self._task_path(key)
            if not path.exists():
                raise AssistantError("任务不存在。", 404)
            record = self._read_json(path, versions=(1, 2))
            try:
                # Existing v0.2 records predate browser action transcripts.
                record.setdefault("actions", [])
                version = record.get("schema_version")
                modern = version == 2
                if (record.get("id") != key or record.get("status") not in
                        {"queued", "running", "completed", "partial", "failed", "cancelled", "interrupted"}
                        or version not in {1, 2}
                        or record.get("portal") != ("public_web" if modern else "bytedance_experienced")
                        or record.get("mode") not in {"api", "demo"}
                        or not isinstance(record.get("pages"), list)
                        or not isinstance(record.get("errors"), list)
                        or type(record.get("cancel_requested")) is not bool):
                    raise ValueError("invalid envelope")
                text_field(record["request"], "任务要求", 2000)
                text_field(record["note"], "任务状态说明", 3000)
                for kind in ("profile", "rules"):
                    text_field(record["memory"][kind], "任务记忆", allow_empty=True)
                if modern:
                    from .browser import BrowserScope
                    scope = record["scope"]
                    if BrowserScope(**scope).as_dict() != scope:
                        raise ValueError("invalid scope")
                    text_field(record["public_request"], "公开查询说明", 4000)
                    if "private_conditions" in record:
                        text_field(record["private_conditions"], "私人筛选条件", 2000, True)
                    if record["phase"] not in {"browsing", "analysis"}:
                        raise ValueError("invalid phase")
                    text_field(record["stop_reason"], "停止原因", 3000, True)
                maxima = (("calls", 200), ("seconds", 3600), ("steps", 500), ("pages", 500), ("details", 500)) if modern else (
                    ("calls", 30), ("seconds", 600), ("steps", 40), ("pages", 30), ("details", 20))
                for limit, maximum in maxima:
                    value = record["limits"][limit]
                    if type(value) is not int or not 1 <= value <= maximum:
                        raise ValueError("invalid limit")
                for counter in ("calls", "steps"):
                    if type(record[counter]) is not int or not 0 <= record[counter] <= record["limits"][counter]:
                        raise ValueError("invalid counter")
                if len(record["pages"]) > record["limits"]["pages"] or len(record["errors"]) > (record["limits"]["steps"] + 1 if modern else 40):
                    raise ValueError("invalid coverage")
                seen = set()
                for page in record["pages"]:
                    if (not isinstance(page["id"], str) or not re.fullmatch(r"p[1-9][0-9]{0,2}", page["id"])
                            or page["id"] in seen or page["kind"] not in ({"detail", "listing", "page"} if modern else {"detail", "listing"})
                            or not isinstance(page["links"], list) or len(page["links"]) > (200 if modern else 80)):
                        raise ValueError("invalid page")
                    seen.add(page["id"])
                    text_field(page["url"], "页面地址", 2000)
                    text_field(page["text"], "页面正文", 65536 if modern else 16000, modern)
                    text_field(page["title"], "页面标题", 300, True)
                    text_field(page["fetched_at"], "页面获取时间", 80)
                    if type(page.get("ready", True)) is not bool:
                        raise ValueError("invalid readiness")
                    elements = page.get("elements", [])
                    if not isinstance(elements, list) or len(elements) > (200 if modern else 80):
                        raise ValueError("invalid elements")
                    for element in elements:
                        text_field(element["id"], "元素编号", 80)
                        text_field(element["label"], "元素标签", 200, True)
                        if not isinstance(element["actions"], list) or set(element["actions"]) - (
                                {"click", "fill", "press", "select", "check"} if modern else {"click", "fill"}):
                            raise ValueError("invalid capabilities")
                if not isinstance(record["actions"], list) or len(record["actions"]) > record["steps"]:
                    raise ValueError("invalid actions")
                for action in record["actions"]:
                    if (type(action["step"]) is not int or not 1 <= action["step"] <= record["steps"]
                            or action["tool"] not in {"open_link", "open_url", "observe_page", "click_element", "fill_field", "scroll_page",
                                                      "go_back", "refresh_page", "wait_page", "press_key", "select_option", "check_field",
                                                      "restart_browser", "take_screenshot", "read_source", "search_source", "hover_element", "dismiss_overlay"}
                            or action["status"] not in {"running", "success", "incomplete", "failed", "interrupted"}
                            or not isinstance(action["arguments"], dict)):
                        raise ValueError("invalid action")
                if record["result"] is not None and not isinstance(record["result"], dict):
                    raise ValueError("invalid result")
            except (KeyError, TypeError, ValueError, AttributeError, AssistantError):
                raise AssistantError("任务记录损坏，不会自动覆盖。", 500) from None
            return record

    def list_tasks(self):
        with self._mutex:
            self._ensure_open()
            records = [self.get_task(path.stem) for path in (self.root / "tasks").glob("*.json")]
            return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def update_task(self, key, **changes):
        # Read-modify-write under one lock: cancellation cannot be lost by a worker.
        allowed = {"status", "cancel_requested", "calls", "steps", "pages", "actions", "errors", "result", "note", "phase", "stop_reason"}
        if set(changes) - allowed:
            raise AssistantError("不能修改任务身份或已授权上下文。", 500)
        with self._mutex:
            record = self.get_task(key)
            if record["cancel_requested"] and changes.get("status") in {"completed", "partial", "failed"}:
                changes.update(status="cancelled", result=None, note="结束前收到取消请求；证据保留，结果未发布。")
                if record.get("schema_version") == 2:
                    changes["stop_reason"] = changes["note"]
            record.update(changes, updated_at=now_iso())
            self._write_json(self._task_path(key), record)
            return record

    def save_task_screenshot(self, key, step, data_url):
        self._task_path(key)
        if (type(step) is not int or not 1 <= step <= 500 or not isinstance(data_url, str)
                or not data_url.startswith("data:image/png;base64,") or len(data_url) > 3_000_000):
            raise AssistantError("截图格式无效，未保存。", 500)
        with self._mutex:
            directory = self.root / "screenshots"
            if directory.is_symlink():
                raise AssistantError("截图目录不能是符号链接。", 500)
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.stat().st_uid != os.getuid():
                raise AssistantError("截图目录必须属于当前用户。", 500)
            directory.chmod(0o700)
            name = f"{key}-{step}.json"
            self._write_json(directory / name, {"schema_version": 1, "task_id": key, "step": step,
                                               "fetched_at": now_iso(), "image": data_url, "masked": True})
            return name

    def update_memory(self, kind, content, append=False):
        if kind not in {"profile", "rules"}:
            raise AssistantError("记忆类型必须是 profile 或 rules。")
        content = text_field(content, "记忆内容", allow_empty=True)
        with self._mutex:
            self._recover_memory()
            before = self.memory()[kind]
            after = ((before.rstrip() + "\n" if before else "") + "- " + content) if append else content
            after = text_field(after, "记忆内容", allow_empty=True)
            if before == after:
                return self.memory()
            revision = uuid.uuid4().hex
            snapshot = {"schema_version": 1, "kind": kind, "before": before,
                        "after": after, "created_at": now_iso()}
            self._write_json(self.root / "memory" / "history" / f"{revision}.json", snapshot)
            # Durable intent first. On interruption the next startup finishes this update.
            self._write_json(self.root / ".memory-pending.json",
                             {"schema_version": 1, "revision": revision})
            self._recover_memory()
            return self.memory()

    def _recover_memory(self):
        pending = self.root / ".memory-pending.json"
        if not pending.exists():
            return
        revision = self._read_json(pending).get("revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{32}", revision):
            raise AssistantError("记忆恢复记录无效。", 500)
        snapshot = self._read_json(self.root / "memory" / "history" / f"{revision}.json")
        if snapshot.get("kind") not in {"profile", "rules"} or not isinstance(snapshot.get("after"), str):
            raise AssistantError("记忆快照损坏，不能恢复。", 500)
        self._write(self.root / "memory" / f"{snapshot['kind']}.md", snapshot["after"])
        pending.unlink()
        self._sync_directory(self.root)

    def chat_history(self):
        with self._mutex:
            path = self.root / "chat.json"
            if not path.exists():
                return []
            turns = self._read_json(path).get("turns")
            if (not isinstance(turns, list) or any(not isinstance(turn, dict)
                    or not isinstance(turn.get("user"), str)
                    or not isinstance(turn.get("reply"), str) for turn in turns)):
                raise AssistantError("对话记录格式损坏。", 500)
            return turns

    def add_chat(self, user, reply, cited_ids):
        with self._mutex:
            turns = self.chat_history()
            if len(turns) >= 200:
                raise AssistantError("当前会话达到 200 轮上限，请先备份并归档 chat.json。", 409)
            turn = {"user": user, "reply": reply, "cited_ids": cited_ids, "created_at": now_iso()}
            turns.append(turn)
            self._write_json(self.root / "chat.json", {"schema_version": 1, "turns": turns})
            return turn

    def usage(self, day):
        with self._mutex:
            path = self.root / "usage.json"
            data = self._read_json(path) if path.exists() else {}
            if data.get("day") != day:
                return {"day": day, "calls": 0}
            calls = data.get("calls")
            if type(calls) is not int or calls < 0:
                raise AssistantError("调用额度文件损坏。", 500)
            return {"day": day, "calls": calls}

    def reserve_call(self, day, limit):
        with self._mutex:
            usage = self.usage(day)
            if usage["calls"] >= limit:
                raise AssistantError("已达到当天真实 API 调用上限，请调整额度或明天再试。", 429)
            usage["calls"] += 1
            self._write_json(self.root / "usage.json", {"schema_version": 1, **usage})
