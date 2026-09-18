import argparse

from .errors import AssistantError
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="Run the local personal assistant web UI.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        app = create_app()
    except AssistantError as exc:
        parser.exit(1, f"无法启动：{exc}\n")
    print(f"个人助手：http://127.0.0.1:{args.port}（仅本机；邮箱/网页只读，无邮件发送）")
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        app.extensions["mail"].close()
        app.extensions["tasks"].close()
        app.extensions["store"].close()


if __name__ == "__main__":
    main()
