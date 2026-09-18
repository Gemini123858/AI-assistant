import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException

from .config import Config
from .errors import AssistantError
from .service import Service
from .storage import Store
from .tasks import Tasks
from .mail import Mail


def create_app(config=None, model=None, browser_factory=None, mail_connector=None):
    config = config or Config.from_env()
    config.validate()
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=128 * 1024, TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
    app.json.ensure_ascii = False
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    store = Store(config.data_dir)
    service = Service(store, config, model)
    try:
        tasks = Tasks(service, browser_factory)
        mail = Mail(service, mail_connector)
    except Exception:
        store.close()
        raise
    app.extensions.update(store=store, service=service, tasks=tasks, mail=mail)
    csrf_token = secrets.token_urlsafe(32)

    @app.before_request
    def guard_request():
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
        return jsonify(error=str(error)), error.status

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
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect("/")
        return render_template("login.html", csrf_token=csrf_token)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(logged_out=True)

    @app.get("/api/status")
    def status():
        return jsonify(mode=config.mode, model=config.model if config.mode == "api" else "演示（无真实推理）",
                       usage=store.usage(service.day()), daily_limit=config.daily_calls,
                       timezone=config.timezone, version="0.3.0",
                       browser_budget={"calls": config.browser_calls, "steps": config.browser_steps,
                                       "seconds": config.browser_seconds,
                                       "effective_calls": min(config.browser_calls, max(0, config.daily_calls - store.usage(service.day())["calls"]))},
                       capabilities={"auto_collect": mail.enabled, "browser_vision": config.browser_vision,
                       "browser_tasks": config.mode == "api", "browser_scope": "task_authorized_public_domains",
                       "push": False, "send_email": False, "automatic_memory": mail.enabled})

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
        if body():
            raise AssistantError("确认画像建议不接受额外参数。")
        return jsonify(mail=mail.confirm_profile(key, index))

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
            payload.update(public_request=previous["public_request"], **previous["scope"])
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

    @app.get("/api/memory")
    def memory():
        return jsonify(**store.memory())

    @app.put("/api/memory/<kind>")
    def update_memory(kind):
        with service.operation():
            return jsonify(**store.update_memory(kind, body().get("content")))

    mail.resume()
    return app
