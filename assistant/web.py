import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException, SecurityError

from .config import Config
from .errors import AssistantError
from .followup import FollowupAgent
from .service import Service
from .storage import Store, text_field
from .tasks import Tasks
from .mail import Mail
from .drafts import Drafts
from .conversations import Conversations


def create_app(config=None, model=None, browser_factory=None, mail_connector=None, smtp_connector=None):
    config = config or Config.from_env()
    config.validate()
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=128 * 1024, TRUSTED_HOSTS=config.trusted_hosts)
    app.json.ensure_ascii = False
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    store = Store(config.data_dir)
    service = Service(store, config, model)
    try:
        tasks = Tasks(service, browser_factory)
        mail = Mail(service, mail_connector)
        drafts = Drafts(service, mail, smtp_connector)
        followup_agent = FollowupAgent(service, browser_factory, mail)
        conversations = Conversations(service, tasks, mail, drafts)
    except Exception:
        store.close()
        raise
    app.extensions.update(store=store, service=service, tasks=tasks, mail=mail, drafts=drafts,
                          followup_agent=followup_agent, conversations=conversations)
    csrf_token = secrets.token_urlsafe(32)

    @app.before_request
    def guard_request():
        # Authentication redirects must not mask Flask's untrusted-Host error.
        if isinstance(request.routing_exception, SecurityError):
            raise request.routing_exception
        public = request.endpoint in {"login", "static"}
        if config.access_token and not public and not session.get("authenticated"):
            if request.path.startswith("/api/"):
                raise AssistantError("请先登录，再读取私人数据。", 401)
            return redirect("/login")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                raise AssistantError("拒绝跨站操作。", 403)
            supplied = request.form.get("csrf_token", "") if request.endpoint == "login" else request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied, csrf_token):
                raise AssistantError("页面安全令牌无效，请刷新页面后重试。", 403)

    @app.after_request
    def security_headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                                 "style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
        return response

    @app.errorhandler(AssistantError)
    def intentional_error(error):
        result = {"error": str(error)}
        if getattr(error, "policy", None):
            result["policy"] = error.policy
        return jsonify(result), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={413: "请求过大，本版仅支持小型文字导入。", 400: "请求格式或 Host 无效。"}
                       .get(error.code, "请求无效或页面不存在。")), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        # Avoid exception repr / request body in logs: upstream errors may contain secrets.
        app.logger.error("Unhandled application error (%s)", type(error).__name__)
        return jsonify(error="内部操作失败，原有数据未主动删除。请检查文件权限、磁盘及备份。"), 500

    def body():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise AssistantError("请求必须为 JSON 对象。")
        return payload

    def action_conversation():
        key = request.headers.get("X-Conversation-ID")
        if key:
            store.get_conversation(key)
        return key

    def draft_action(key, label, draft):
        if key:
            conversations.record_action(key, label, [conversations.records.read({"kind": "draft", "id": draft["id"]})])
        return jsonify(draft=draft)

    @app.get("/api/conversations")
    def conversation_list():
        return jsonify(conversations=conversations.list())

    @app.post("/api/conversations")
    def conversation_create():
        payload = body()
        return jsonify(conversation=store.create_conversation(payload.get("title", "新对话"))), 201

    @app.get("/api/conversations/<key>")
    def conversation_detail(key):
        return jsonify(conversation=store.get_conversation(key))

    @app.patch("/api/conversations/<key>")
    def conversation_rename(key):
        title = text_field(body().get("title"), "会话标题", 120)
        return jsonify(conversation=store.change_conversation(key, lambda record: record.update(title=title)))

    @app.delete("/api/conversations/<key>")
    def conversation_delete(key):
        store.delete_conversation(key)
        return jsonify(deleted=True)

    @app.post("/api/conversations/<key>/turns")
    def conversation_start(key):
        return jsonify(**conversations.start(key, body())), 202

    @app.get("/api/conversations/<key>/events")
    def conversation_events(key):
        try:
            after = int(request.args.get("after", 0))
            if after < 0:
                raise ValueError()
        except ValueError:
            raise AssistantError("事件序号必须为非负整数。") from None
        record = store.get_conversation(key)
        turn = next((turn for turn in record["turns"] if turn["status"] == "running"), None)
        live_task = conversations.records.read({"kind": "task", "id": turn["task_id"]}) if turn and turn.get("task_id") else None
        return jsonify(events=record["events"][after:], turns=record["turns"], title=record["title"],
                       updated_at=record["updated_at"], live_task=live_task)

    @app.post("/api/conversations/<key>/turns/<turn_id>/cancel")
    def conversation_cancel(key, turn_id):
        return jsonify(conversation=conversations.cancel(key, turn_id))

    @app.get("/api/records")
    def records_search():
        try:
            offset, limit = int(request.args.get("offset", 0)), int(request.args.get("limit", 30))
        except ValueError:
            raise AssistantError("分页参数必须为整数。") from None
        return jsonify(**conversations.records.search(kind=request.args.get("kind", "all"),
            query=request.args.get("query", ""), offset=offset, limit=limit))

    @app.get("/api/records/<kind>/<key>")
    def record_detail(kind, key):
        ref = {"kind": kind, "id": key}
        if request.args.get("summary") == "1":
            snapshot = conversations.records.read(ref)
            return jsonify(record={name: snapshot[name] for name in ("ref", "title", "status", "updated_at")})
        snapshot = conversations.records.read(ref, fetch_body=True)
        raw = conversations.records.raw(ref)
        if kind == "task":
            raw = {name: value for name, value in raw.items() if name != "memory"}
        return jsonify(record=snapshot, detail=raw)

    def notice_action(conversation, message, record):
        if conversation:
            conversations.record_action(conversation, message, [conversations.records.read({"kind": "notice", "id": record["id"]})])
        return jsonify(notice=record)

    @app.get("/api/notices")
    def notice_list():
        return jsonify(notices=conversations.notices.list(request.args.get("view", "all"), request.args.get("query", "")))

    @app.post("/api/notices")
    def notice_create():
        conversation = action_conversation()
        with service.operation():
            record, created = conversations.notices.create(body(), origin={"conversation_id": conversation})
            return notice_action(conversation if created else None, "用户新建通知", record), 201 if created else 200

    @app.post("/api/notices/from-analysis")
    def notice_from_analysis():
        conversation = action_conversation()
        payload = body()
        fields = conversations.notices.from_analysis(payload.get("ref"), payload.get("index", 0), payload.get("section", "analysis"))
        with service.operation():
            record, created = conversations.notices.create(fields, origin={"conversation_id": conversation})
            return notice_action(conversation if created else None, "用户将分析保存为通知", record), 201 if created else 200

    @app.get("/api/notices/<key>")
    def notice_detail(key):
        return jsonify(notice=store.get_notice(key))

    @app.patch("/api/notices/<key>")
    def notice_update(key):
        conversation = action_conversation()
        with service.operation():
            before = store.get_notice(key)
            record = conversations.notices.update(key, body())
            return notice_action(conversation if record != before else None, "用户编辑通知", record)

    @app.delete("/api/notices/<key>")
    def notice_delete(key):
        conversation = action_conversation()
        with service.operation():
            record = conversations.notices.set_deleted(key, body(), True)
            return notice_action(conversation, "用户删除通知（可恢复）", record)

    @app.post("/api/notices/<key>/restore")
    def notice_restore(key):
        conversation = action_conversation()
        with service.operation():
            record = conversations.notices.set_deleted(key, body(), False)
            return notice_action(conversation, "用户恢复通知", record)

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=csrf_token, auth_enabled=bool(config.access_token))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.access_token:
            return redirect("/")
        if request.method == "POST":
            if not secrets.compare_digest(request.form.get("token", "").encode(), config.access_token.encode()):
                return render_template("login.html", csrf_token=csrf_token, error="访问口令不正确。"), 401
            service.close_followups()
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect("/")
        return render_template("login.html", csrf_token=csrf_token)

    @app.post("/api/logout")
    def logout():
        service.close_followups()
        session.clear()
        return jsonify(logged_out=True)

    @app.get("/api/followups")
    def followups():
        return jsonify(**service.followups())

    @app.post("/api/followups/<kind>/<key>")
    def ask_followup(kind, key):
        return jsonify(session=service.ask_followup(kind, key, body().get("message"), followup_agent.answer))

    @app.post("/api/followups/<kind>/<key>/close")
    def close_followup(kind, key):
        if body():
            raise AssistantError("关闭追问不接受额外参数。")
        service.close_followup(kind, key)
        return jsonify(closed=True)

    @app.get("/api/status")
    def status():
        return jsonify(mode=config.mode, model=config.model if config.mode == "api" else "演示（无真实推理）",
                       usage=store.usage(service.day()), daily_limit=config.daily_calls,
                       timezone=config.timezone, version="0.4.0", identity=store.identity(),
                       conversation_budget={"calls": config.turn_calls, "seconds": config.turn_seconds,
                                            "context_chars": config.context_chars, "context_tokens": config.context_tokens,
                                            "max_output_tokens": config.max_tokens, "token_count_mode": config.token_count_mode},
                       browser_budget={"calls": config.browser_calls, "steps": config.browser_steps,
                                       "default_calls": min(config.browser_default_calls, config.browser_calls),
                                       "seconds": config.browser_seconds,
                                       "effective_calls": min(config.browser_calls, max(0, config.daily_calls - store.usage(service.day())["calls"]))},
                       capabilities={"auto_collect": mail.enabled, "browser_vision": config.browser_vision,
                       "browser_tasks": config.mode == "api", "browser_scope": "public_https_reading",
                       "browser_dns": "system",
                       "push": False, "send_email": drafts.enabled, "automatic_memory": mail.enabled})

    @app.get("/api/drafts")
    def draft_list():
        return jsonify(enabled=drafts.enabled, drafts=store.list_drafts())

    @app.post("/api/drafts")
    def draft_create():
        key = action_conversation()
        with service.operation():
            return draft_action(key, "用户新建草稿", drafts.create(body())), 201

    @app.get("/api/drafts/<key>")
    def draft_detail(key):
        return jsonify(draft=store.get_draft(key))

    @app.put("/api/drafts/<key>")
    def draft_update(key):
        conversation = action_conversation()
        with service.operation():
            before = store.get_draft(key)
            draft = drafts.update(key, body())
            return draft_action(conversation if draft != before else None, "用户手工保存草稿", draft)

    @app.post("/api/drafts/<key>/assist")
    def draft_assist(key):
        return jsonify(draft=drafts.assist(key, body()))

    @app.post("/api/drafts/<key>/restore")
    def draft_restore(key):
        conversation = action_conversation()
        with service.operation():
            return draft_action(conversation, "用户恢复草稿历史版本", drafts.restore(key, body()))

    @app.post("/api/drafts/<key>/copy")
    def draft_copy(key):
        conversation = action_conversation()
        with service.operation():
            original = store.get_draft(key)
            copied = drafts.create({**original, "source": {"kind": "records", "refs": [{"kind": "draft", "id": key}]}})
            return draft_action(conversation, "用户复制为独立草稿", copied), 201

    @app.delete("/api/drafts/<key>")
    def draft_delete(key):
        conversation = action_conversation()
        with service.operation():
            result = drafts.delete(key, body())
            if conversation:
                conversations.event(conversation, "user_action", text="用户删除草稿：" + key, records=[])
            return jsonify(**result)

    @app.post("/api/drafts/<key>/send")
    def draft_send(key):
        conversation = action_conversation()
        payload = body()
        if set(payload) != {"version"}:
            raise AssistantError("发送只接受当前草稿版本号。")
        return draft_action(conversation, "用户点击发送当前草稿版本", drafts.send(key, payload["version"]))

    @app.get("/api/mail")
    def mail_list():
        records = mail.records()
        return jsonify(status=mail.status(), total=len(records),
                       mails=[{k: v for k, v in r.items() if k != "text"} for r in records[:100]])

    @app.post("/api/mail/start")
    def mail_start():
        if body():
            raise AssistantError("启用邮箱同步不接受额外参数。")
        return jsonify(**mail.start())

    @app.post("/api/mail/sync")
    def mail_sync():
        if body():
            raise AssistantError("同步不接受额外参数。")
        if not mail.state():
            raise AssistantError("请先启用同步并建立新邮件基线。", 409)
        return jsonify(**mail.sync())

    @app.post("/api/mail/latest")
    def mail_latest():
        if body() != {"confirm": True}:
            raise AssistantError("读取最新一封已有邮件需要明确确认，不会批量导入历史。")
        return jsonify(mail=mail.read_latest())

    @app.post("/api/mail/triage")
    def mail_triage():
        if body():
            raise AssistantError("初筛不接受额外参数。")
        return jsonify(**mail.triage())

    @app.get("/api/mail/<key>")
    def mail_detail(key):
        return jsonify(mail=mail.get(key))

    @app.post("/api/mail/<key>/read")
    def mail_read(key):
        if body():
            raise AssistantError("读取正文不接受额外参数。")
        return jsonify(mail=mail.read_body(key))

    @app.post("/api/mail/<key>/analyze")
    def mail_analyze(key):
        if body():
            raise AssistantError("分析不接受额外参数。")
        return jsonify(mail=mail.analyze(key))

    @app.post("/api/mail/<key>/profile/<int:index>/confirm")
    def confirm_mail_profile(key, index):
        conversation = action_conversation()
        if body():
            raise AssistantError("确认画像建议不接受额外参数。")
        record = mail.confirm_profile(key, index)
        if conversation:
            conversations.record_action(conversation, "用户确认邮件中的本人画像建议", [conversations.records.read({"kind": "mail", "id": key})])
        return jsonify(mail=record)

    def public_task(task, detail=False):
        # Memory snapshots never need to be sent back with every task-progress poll.
        safe = {key: value for key, value in task.items() if key != "memory"}
        if not detail:
            safe["pages"] = [{key: page[key] for key in ("id", "url", "title", "kind", "fetched_at")}
                             for page in task["pages"]]
            for brief, page in zip(safe["pages"], task["pages"]):
                brief["ready"] = page.get("ready", True)
        return safe

    @app.get("/api/tasks")
    def list_tasks():
        return jsonify(tasks=[public_task(task) for task in store.list_tasks()])

    @app.post("/api/tasks")
    def start_task():
        return jsonify(task=public_task(tasks.start(body()))), 202

    @app.get("/api/tasks/<key>")
    def task_detail(key):
        return jsonify(task=public_task(store.get_task(key), detail=True))

    @app.post("/api/tasks/<key>/cancel")
    def cancel_task(key):
        if body():
            raise AssistantError("取消不接受额外参数。")
        return jsonify(task=public_task(tasks.cancel(key)))

    @app.post("/api/tasks/<key>/restart")
    def restart_task(key):
        if body():
            raise AssistantError("重新发起不接受额外参数。")
        previous = store.get_task(key)
        if previous["status"] in {"queued", "running"}:
            raise AssistantError("运行中的任务不能重新发起。", 409)
        payload = {"request": previous["request"]}
        if previous.get("schema_version") == 2:
            if "private_conditions" in previous:
                payload = {"goal": previous["public_request"], "private_conditions": previous["private_conditions"],
                           "profile_supplement": previous.get("profile_supplement", ""),
                           **previous["scope"]}
            else:
                payload.update(public_request=previous["public_request"], **previous["scope"])
        # A same-scope API restart must never silently broaden old authority.
        # The UI offers a new, explicitly confirmed public-reading task instead.
        payload.setdefault("public_web", False)
        return jsonify(task=public_task(tasks.start(payload))), 202

    @app.get("/api/messages")
    def messages():
        try:
            offset, limit = int(request.args.get("offset", 0)), int(request.args.get("limit", 50))
        except ValueError:
            raise AssistantError("分页参数必须为整数。") from None
        if offset < 0 or not 1 <= limit <= 100:
            raise AssistantError("offset 不能为负，limit 必须为 1～100。")
        records = store.list_messages()
        return jsonify(messages=records[offset:offset + limit], total=len(records), offset=offset, limit=limit)

    @app.post("/api/messages")
    def import_message():
        with service.operation():
            record, created = store.import_message(body())
        return jsonify(message=record, created=created), 201 if created else 200

    @app.get("/api/messages/<key>")
    def message_detail(key):
        return jsonify(message=store.get_message(key))

    @app.post("/api/messages/<key>/analyze")
    def analyze(key):
        payload = body()
        retry = payload.get("retry", False)
        if type(retry) is not bool:
            raise AssistantError("retry 必须为布尔值。")
        return jsonify(message=service.analyze(key, retry))

    @app.get("/api/chat")
    def chat_history():
        return jsonify(turns=store.chat_history())

    @app.post("/api/chat")
    def chat():
        return jsonify(turn=service.chat(body()))

    @app.put("/api/identity")
    def update_identity():
        return jsonify(identity=store.update_identity(body()))

    @app.get("/api/memory")
    def memory():
        return jsonify(**store.memory())

    @app.get("/api/memory/<kind>/history")
    def memory_history(kind):
        return jsonify(revisions=store.memory_history(kind))

    @app.put("/api/memory/<kind>")
    def update_memory(kind):
        conversation = action_conversation()
        with service.operation():
            result = store.update_memory(kind, body().get("content"), source={"actor": "user", "conversation_id": conversation})
            if conversation:
                conversations.record_action(conversation, "用户编辑画像或规则", [conversations.records.read({"kind": "memory", "id": kind})])
            return jsonify(**result)

    mail.resume()
    return app
