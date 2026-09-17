import secrets
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .config import Config
from .errors import AssistantError
from .service import Service
from .storage import Store


def create_app(config=None, model=None):
    config = config or Config.from_env()
    config.validate()
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=128 * 1024, TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
    app.json.ensure_ascii = False
    store = Store(config.data_dir)
    service = Service(store, config, model)
    app.extensions.update(store=store, service=service)
    csrf_token = secrets.token_urlsafe(32)

    @app.before_request
    def guard_request():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                raise AssistantError("拒绝跨站操作。", 403)
            supplied = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied, csrf_token):
                raise AssistantError("页面安全令牌无效，请刷新页面后重试。", 403)

    @app.after_request
    def security_headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                                 "style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
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
        return render_template("index.html", csrf_token=csrf_token)

    @app.get("/api/status")
    def status():
        return jsonify(mode=config.mode, model=config.model if config.mode == "api" else "演示（无真实推理）",
                       usage=store.usage(service.day()), daily_limit=config.daily_calls,
                       timezone=config.timezone, version="0.1.0", capabilities={"auto_collect": False,
                       "push": False, "send_email": False, "automatic_memory": False})

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

    return app
