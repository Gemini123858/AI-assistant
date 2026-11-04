"""QQ text front end. Business data stays in the existing Python application."""

import argparse
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import uuid

from .errors import AssistantError
from .storage import Store, text_field


BRIDGE = Path(__file__).with_name("qq_bridge") / "bridge.mjs"


class QQChat:
    def __init__(self, conversations, app_id, owner, send):
        self.conversations = conversations
        self.store = conversations.store
        self.owner, self.send = owner, send
        self.path = self.store.root / "qqbot.json"
        self.state = self.store._read_json(self.path) if self.path.exists() else {}
        if self.state.get("app_id") != app_id or self.state.get("owner") != owner:
            self.state = {"schema_version": 1, "app_id": app_id, "owner": owner,
                          "conversation_id": None, "seen": []}
        self.pending, self.deliveries, self.cursors, self.last_words = {}, {}, {}, {}

    def receive(self, message):
        if message.get("kind") != "c2c" or message.get("senderId") != self.owner:
            return
        message_id = text_field(message.get("messageId"), "QQ消息标识", 256)
        if message_id in self.state["seen"]:
            return
        self.state["seen"] = (self.state["seen"] + [message_id])[-200:]
        self._save()
        try:
            text = text_field(message.get("text"), "QQ消息", 12000, allow_empty=True).strip()
            if message.get("hasAttachments"):
                self._reply(message_id, "当前仅支持文字；图片、语音和文件尚未接入，请发送文字内容。")
                return
            if not text:
                return
            record = self._current()
            result = self.conversations.start(record["id"], {"message": text},
                on_response=lambda event: self._reply(message_id, event["public_text"], key=record["id"],
                    turn_id=event["turn_id"], seq=event["seq"]))
            self.pending[result["turn_id"]] = (record["id"], message_id)
            self.cursors[result["turn_id"]] = 0
        except AssistantError as error:
            self._reply(message_id, str(error))

    def poll(self):
        for turn_id, (key, message_id) in list(self.pending.items()):
            try:
                record = self.store.get_conversation(key)
            except AssistantError as error:
                if error.status != 404:
                    raise
                self._reply(message_id, "对应会话已删除，无法读取结果。")
                del self.pending[turn_id]
                continue
            turn = self.conversations.turn(record, turn_id)
            last = self.cursors.get(turn_id, 0)
            for event in record["events"]:
                if event.get("turn_id") != turn_id or event["seq"] <= last:
                    continue
                if event["type"] == "answer":
                    self._reply(message_id, event["text"], key=key, turn_id=turn_id, seq=event["seq"])
                self.cursors[turn_id] = event["seq"]
            if turn["status"] == "running":
                continue
            if turn["status"] != "completed":
                self._reply(message_id, turn["note"], key=key, turn_id=turn_id)
            del self.pending[turn_id]
            self.cursors.pop(turn_id, None)
            self.last_words.pop((key, turn_id), None)

    def delivery(self, event):
        source = self.deliveries.pop(event.get("id"), None)
        if source and source[0]:
            key, turn_id, seq = source
            try:
                self.conversations.event(key, "qq_delivery", turn_id=turn_id,
                    status="sent" if event.get("ok") else "failed", response_seq=seq,
                    text="QQ回复已发送。" if event.get("ok") else "QQ回复发送失败或结果不明；本地记录已保留。",
                    truncated=bool(event.get("truncated")))
            except AssistantError as error:
                if error.status != 404:
                    raise

    def _current(self):
        key = self.state["conversation_id"]
        if key:
            try:
                record = self.store.get_conversation(key)
                if record.get("channel") != "qq":
                    return self.store.change_conversation(key, lambda value: value.update(channel="qq"))
                return record
            except AssistantError as error:
                if error.status != 404:
                    raise
        record = self.store.create_conversation("QQ 对话", channel="qq")
        self.state["conversation_id"] = record["id"]
        self._save()
        return record

    def _save(self):
        self.store._write_json(self.path, self.state)

    def _reply(self, message_id, text, *, key=None, turn_id=None, seq=None):
        text = text.strip()
        if key and turn_id:
            if self.last_words.get((key, turn_id)) == text:
                return
            self.last_words[(key, turn_id)] = text
        identity = uuid.uuid4().hex
        self.deliveries[identity] = (key, turn_id, seq)
        try:
            self.send({"type": "send", "id": identity, "messageId": message_id, "text": text})
        except (OSError, ValueError):
            self.delivery({"id": identity, "ok": False})


def bridge_command(config):
    node = shutil.which(config.qq_bot_node)
    if not node:
        raise AssistantError("QQ接入需要 Node.js >=20.3；请配置 PATH 或 ASSISTANT_QQ_NODE。")
    if not (BRIDGE.parent / "node_modules" / "@tencent-connect" / "qqbot-nodejs").is_dir():
        raise AssistantError("请先在 assistant/qq_bridge 中运行 npm ci --omit=optional --ignore-scripts。")
    return [node, str(BRIDGE)]


def bridge_environment():
    # The QQ child needs no model, mail or web-login secrets.
    names = ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    return {name: os.environ[name] for name in names if name in os.environ}


def credentials(config):
    values = [os.getenv(name, "").strip() for name in ("QQBOT_APPID", "QQBOT_SECRET", "QQBOT_OWNER_OPENID")]
    if any(values):
        data = dict(zip(("appId", "appSecret", "userOpenid"), values))
    else:
        path = config.data_dir / "qqbot-credentials.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AssistantError("请先运行 python -m assistant.qqbot bind 扫码绑定，或同时配置 QQBOT_APPID、QQBOT_SECRET、QQBOT_OWNER_OPENID。") from None
    if not isinstance(data, dict) or any(not isinstance(data.get(k), str) or not data[k].strip()
                                         for k in ("appId", "appSecret", "userOpenid")):
        raise AssistantError("QQ凭据不完整：必须提供机器人AppID、AppSecret和本人userOpenid（不是QQ号）。")
    return {k: data[k] for k in ("appId", "appSecret", "userOpenid")}


class QQBot:
    def __init__(self, conversations):
        self.conversations, self.config = conversations, conversations.config
        self.process = None
        self.thread = self.reader = None
        self.closed = threading.Event()
        self.events = queue.Queue()
        self._write_lock = threading.Lock()
        self.state = "not_started" if self.config.qq_bot_enabled else "disabled"
        self.last_error = ""

    def start(self):
        if not self.config.qq_bot_enabled:
            return
        auth = credentials(self.config)
        command = bridge_command(self.config)
        self.chat = QQChat(self.conversations, auth["appId"], auth["userOpenid"], self._write)
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=bridge_environment())
            self._write({"type": "configure", "credentials": auth})
        except OSError:
            self.close()
            raise AssistantError("QQ连接进程无法启动，请检查Node及依赖安装。") from None
        self.state = "connecting"
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.reader.start()
        self.thread.start()

    def status(self):
        return {"enabled": self.config.qq_bot_enabled, "state": self.state, "error": self.last_error}

    def _write(self, event):
        with self._write_lock:
            self.process.stdin.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read(self):
        for line in self.process.stdout:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    self.events.put(event)
            except ValueError:
                pass
        self.events.put({"type": "closed"})

    def _run(self):
        while not self.closed.is_set():
            try:
                try:
                    event = self.events.get(timeout=0.25)
                except queue.Empty:
                    event = {}
                kind = event.get("type")
                if kind == "closed":
                    self.state = "disconnected"
                    self.last_error = "QQ连接进程已退出；检查依赖、凭据和网络后重启应用。"
                    break
                if kind == "ready":
                    self.state, self.last_error = "connected", ""
                elif kind == "message":
                    self.chat.receive(event)
                elif kind == "delivery":
                    self.chat.delivery(event)
                    if not event.get("ok"):
                        self.last_error = "QQ回复发送失败或结果不明；完整记录已保存在本地。"
                elif kind == "error":
                    self.last_error = "QQ连接或协议出错，请检查网络、账号权限与绑定状态。"
                elif kind == "disconnected":
                    self.state = "reconnecting" if event.get("willReconnect") else "disconnected"
                self.chat.poll()
            except Exception as error:
                logging.getLogger(__name__).error("QQ front end failed (%s)", type(error).__name__)
                self.last_error = "QQ接入处理失败；业务记录保留，可以检查本地记录。"

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.process:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            if self.reader:
                self.reader.join(timeout=2)
            self.process.stdout.close()
        self.state = "stopped"


def main():
    parser = argparse.ArgumentParser(description="QQ机器人扫码绑定；停止应用后运行，不调用模型。")
    parser.add_argument("action", choices=["bind"])
    parser.parse_args()
    from .config import Config
    store = None
    try:
        config = Config.from_env()
        command = bridge_command(config)
        store = Store(config.data_dir)
        result = subprocess.run(command + ["bind", str(store.root / "qqbot-credentials.json")],
                                env=bridge_environment(), check=False)
        if result.returncode:
            parser.exit(1, "QQ绑定未完成。\n")
    except AssistantError as error:
        parser.exit(1, str(error) + "\n")
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
