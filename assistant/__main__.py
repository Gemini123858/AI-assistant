import argparse

from .errors import AssistantError
from .config import Config
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="Run the local personal assistant web UI.")
    parser.add_argument("--host", help="监听 IP；默认读取 .env 的 ASSISTANT_HOST")
    parser.add_argument("--port", type=int, help="监听端口；默认读取 .env 的 ASSISTANT_PORT")
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if args.host is not None:
            config.host = args.host
        if args.port is not None:
            config.port = args.port
        config.validate()
        from waitress import serve
        app = create_app(config)
    except AssistantError as exc:
        parser.exit(1, f"无法启动：{exc}\n")
    print(f"个人助手 v0.4：http://{config.host}:{config.port}（监听地址）", flush=True)
    print("远程访问请使用网关 IP 和映射端口；邮件发送由用户点击草稿发送按钮触发。", flush=True)
    try:
        serve(app, host=config.host, port=config.port, threads=8,
              max_request_body_size=128 * 1024)
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["conversations"].close()
        app.extensions["mail"].close()
        app.extensions["tasks"].close()
        app.extensions["store"].close()


if __name__ == "__main__":
    main()
