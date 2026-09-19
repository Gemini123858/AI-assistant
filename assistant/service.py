import hashlib
import json
import copy
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import AssistantError
from .llm import ANALYZE, CHAT, SYSTEM, APIModel, DemoModel, validate_analysis, validate_chat
from .storage import text_field


class Service:
    def __init__(self, store, config, model=None):
        self.store = store
        self.config = config
        self.model = model if model is not None else (APIModel(config) if config.mode == "api" else DemoModel())
        self._operation_lock = threading.Lock()
        self._followup_lock = threading.RLock()
        self._followups = {"task": None, "mail": None}

    @contextmanager
    def operation(self):
        # Serialize mutation workflows, not just individual file writes.
        if not self._operation_lock.acquire(blocking=False):
            raise AssistantError("助手正在处理另一个操作，请稍后再试。", 409)
        try:
            yield
        finally:
            self._operation_lock.release()

    def day(self):
        return datetime.now(ZoneInfo(self.config.timezone)).date().isoformat()

    @staticmethod
    def _followup_kind(kind):
        if kind not in {"task", "mail"}:
            raise AssistantError("追问类型无效。", 400)
        return kind

    def open_followup(self, kind, key, context):
        kind = self._followup_kind(kind)
        serialized = json.dumps(context, ensure_ascii=False)
        if len(serialized) > 36000:
            raise AssistantError("追问上下文超过本版限制，原分析结果已保存但未开启追问。", 409)
        with self._followup_lock:
            self._followups[kind] = {"kind": kind, "key": key, "turns": [],
                "context": copy.deepcopy(context), "state": {"pages": []}}

    def followups(self):
        with self._followup_lock:
            return {kind: (None if item is None else
                    {"kind": kind, "key": item["key"], "turns": copy.deepcopy(item["turns"])})
                    for kind, item in self._followups.items()}

    def close_followup(self, kind, key=None):
        kind = self._followup_kind(kind)
        with self._followup_lock:
            item = self._followups[kind]
            if key is not None and (item is None or item["key"] != key):
                raise AssistantError("该追问会话已归档或被更新任务替代。", 409)
            self._followups[kind] = None

    def close_followups(self):
        with self._followup_lock:
            self._followups = {"task": None, "mail": None}

    def ask_followup(self, kind, key, message, agent):
        kind = self._followup_kind(kind)
        question = text_field(message, "追问", 4000)
        with self.operation(), self._followup_lock:
            item = self._followups[kind]
            if item is None or item["key"] != key:
                raise AssistantError("该追问会话已归档或被更新任务替代。", 409)
            if len(item["turns"]) >= 40:
                raise AssistantError("本次追问已达20轮，请关闭后发起新的任务或分析。", 409)
            working = copy.deepcopy(item)
            reply = text_field(agent(working, question), "追问回答", 6000)
            working["turns"].extend(({"role": "user", "text": question},
                                     {"role": "assistant", "text": reply}))
            self._followups[kind] = item = working
            return {"kind": kind, "key": key, "turns": copy.deepcopy(item["turns"])}

    def _complete(self, instruction, data):
        data = {**data, "current_time": datetime.now(ZoneInfo(self.config.timezone)).isoformat()}
        serialized = json.dumps(data, ensure_ascii=False)
        if len(serialized) > 40000:
            raise AssistantError("上下文超过本版 40000 字上限，请减少选中的通知或精简记忆。")
        if self.config.mode == "api":
            # Reserve before making a request. Failed attempts consume the budget too.
            self.store.reserve_call(self.day(), self.config.daily_calls)
        return self.model.complete([{"role": "system", "content": SYSTEM + "\n" + instruction},
                                    {"role": "user", "content": serialized}])

    def analyze(self, key, retry=False):
        with self.operation():
            record = self.store.get_message(key)
            if record["status"] == "success" and not retry:
                return record  # idempotent: no new API call
            if record["status"] == "failed" and not retry:
                raise AssistantError("上次分析失败，请使用明确的重试操作。", 409)
            memory = self.store.memory()
            context = {"mode": self.config.mode, "model": self.config.model if self.config.mode == "api" else "demo",
                       "memory_hash": hashlib.sha256(json.dumps(memory, sort_keys=True).encode()).hexdigest(),
                       "timezone": self.config.timezone, "prompt_version": 2}
            try:
                result = validate_analysis(self._complete(ANALYZE, {"notification": record["original"],
                                           "memory": memory, "timezone": self.config.timezone}), record["original"])
            except AssistantError as exc:
                self.store.save_result(key, error=str(exc), context=context)
                raise
            return self.store.save_result(key, analysis=result, context=context)

    def chat(self, payload):
        if not isinstance(payload, dict):
            raise AssistantError("对话必须为 JSON 对象。")
        user = text_field(payload.get("message"), "对话消息", 4000)
        with self.operation():
            history = self.store.chat_history()
            if len(history) >= 200:
                raise AssistantError("会话已达 200 轮上限，请先备份并归档。", 409)
            for prefix, kind in (("/记住 ", "profile"), ("/规则 ", "rules")):
                if user.startswith(prefix):
                    content = text_field(user[len(prefix):], "记忆内容", 4000)
                    self.store.update_memory(kind, content, append=True)
                    reply = "已记住，后续分析和对话会使用这条信息。旧通知不会自动重新分析。"
                    return self.store.add_chat(user, reply, [])
            if user in {"/查看画像", "/查看规则"}:
                kind = "profile" if user == "/查看画像" else "rules"
                return self.store.add_chat(user, self.store.memory()[kind] or "当前为空。", [])
            keys = payload.get("message_ids")
            if keys is None:
                records = self.store.list_messages()[:10]
            else:
                if not isinstance(keys, list) or len(keys) > 10 or any(not isinstance(key, str) for key in keys):
                    raise AssistantError("本版对话最多选择 10 条通知。")
                records = [self.store.get_message(key) for key in dict.fromkeys(keys)]
            notifications = [{"id": record["id"], "original": record["original"],
                              "analysis": record["analysis"], "status": record["status"],
                              "coverage": record["coverage"]} for record in records]
            result = validate_chat(self._complete(CHAT, {"question": user,
                       "notifications": notifications, "memory": self.store.memory(),
                       "timezone": self.config.timezone,
                       "history": [{"user": turn["user"], "reply": turn["reply"]} for turn in history[-6:]]}),
                       {record["id"] for record in records})
            return self.store.add_chat(user, result["reply"], result["cited_ids"])
