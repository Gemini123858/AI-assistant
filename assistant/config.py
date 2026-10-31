import json
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .errors import AssistantError


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    mode: str = "demo"
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.moonshot.cn/v1"
    model: str = "kimi-k2.6"
    timeout: int = 60
    max_tokens: int = 8192
    daily_calls: int = 30
    turn_calls: int = 50
    turn_seconds: int = 1200
    context_chars: int = 300000
    context_tokens: int = 128000
    token_count_mode: str = "auto"
    timezone: str = "Asia/Shanghai"
    extra_body: dict = field(default_factory=dict)
    browser_calls: int = 40
    browser_default_calls: int = 20
    browser_seconds: int = 900
    browser_steps: int = 120
    browser_vision: bool = False
    browser_proxy: str = ""
    access_token: str = field(default="", repr=False)
    qq_imap_user: str = ""
    qq_imap_auth_code: str = field(default="", repr=False)
    mail_poll_seconds: int = 60
    chat_self_names: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    trusted_hosts: list = field(default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"])

    @classmethod
    def from_env(cls):
        # Only load the project-local file, not arbitrary parent directories.
        load_dotenv(Path.cwd() / ".env", override=False)
        try:
            config = cls(
                data_dir=Path(os.getenv("ASSISTANT_DATA_DIR", "data")),
                mode=os.getenv("ASSISTANT_LLM_MODE", "demo"),
                api_key=os.getenv("ASSISTANT_API_KEY", ""),
                base_url=os.getenv("ASSISTANT_API_BASE", "https://api.moonshot.cn/v1"),
                model=os.getenv("ASSISTANT_MODEL", "kimi-k2.6"),
                timeout=int(os.getenv("ASSISTANT_TIMEOUT", "60")),
                max_tokens=int(os.getenv("ASSISTANT_MAX_TOKENS", "8192")),
                daily_calls=int(os.getenv("ASSISTANT_DAILY_CALLS", "30")),
                turn_calls=int(os.getenv("ASSISTANT_TURN_CALLS", "50")),
                turn_seconds=int(os.getenv("ASSISTANT_TURN_SECONDS", "1200")),
                context_chars=int(os.getenv("ASSISTANT_CONTEXT_CHARS", "300000")),
                context_tokens=int(os.getenv("ASSISTANT_CONTEXT_TOKENS", "128000")),
                token_count_mode=os.getenv("ASSISTANT_TOKEN_COUNT_MODE", "auto"),
                timezone=os.getenv("ASSISTANT_TIMEZONE", "Asia/Shanghai"),
                extra_body=json.loads(os.getenv("ASSISTANT_LLM_EXTRA_BODY", "{}")),
                browser_calls=int(os.getenv("ASSISTANT_BROWSER_CALLS", "40")),
                browser_default_calls=int(os.getenv("ASSISTANT_BROWSER_DEFAULT_CALLS", "20")),
                browser_seconds=int(os.getenv("ASSISTANT_BROWSER_SECONDS", "900")),
                browser_steps=int(os.getenv("ASSISTANT_BROWSER_STEPS", "120")),
                browser_vision=os.getenv("ASSISTANT_BROWSER_VISION", "false").lower() == "true",
                browser_proxy=os.getenv("ASSISTANT_BROWSER_PROXY", "").strip(),
                access_token=os.getenv("ASSISTANT_ACCESS_TOKEN", ""),
                qq_imap_user=os.getenv("QQ_IMAP_USER", ""),
                qq_imap_auth_code=os.getenv("QQ_IMAP_AUTH_CODE", ""),
                mail_poll_seconds=int(os.getenv("ASSISTANT_MAIL_POLL_SECONDS", "60")),
                chat_self_names=os.getenv("ASSISTANT_CHAT_SELF_NAMES", ""),
                host=os.getenv("ASSISTANT_HOST", "127.0.0.1").strip(),
                port=int(os.getenv("ASSISTANT_PORT", "8000")),
                trusted_hosts=[host.strip() for host in os.getenv(
                    "ASSISTANT_TRUSTED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if host.strip()],
            )
        except (ValueError, TypeError) as exc:
            raise AssistantError("环境变量配置格式错误，请检查整数及 EXTRA_BODY JSON。") from exc
        config.validate()
        return config

    def validate(self):
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            raise AssistantError("ASSISTANT_HOST 必须为监听 IP 地址，例如 127.0.0.1 或 0.0.0.0。") from None
        if not 1 <= self.port <= 65535:
            raise AssistantError("ASSISTANT_PORT 必须在 1～65535 之间。")
        if not address.is_loopback and len(self.access_token) < 24:
            raise AssistantError("允许远程访问前必须配置至少24字符的 ASSISTANT_ACCESS_TOKEN。")
        if (not self.trusted_hosts or any(not isinstance(host, str) or not host
                or any(char in host for char in "/@?#*\\ \t\r\n")
                or (":" in host and host != "[::1]") or host.startswith(".")
                for host in self.trusted_hosts)):
            raise AssistantError("ASSISTANT_TRUSTED_HOSTS 请填写逗号分隔的明确主机名或 IPv4，不含协议、端口或通配符。")
        if bool(self.qq_imap_user) != bool(self.qq_imap_auth_code):
            raise AssistantError("QQ_IMAP_USER 和 QQ_IMAP_AUTH_CODE 必须同时配置。")
        if self.qq_imap_user and (not self.qq_imap_user.lower().endswith("@qq.com")
                or any(c in self.qq_imap_user for c in '\r\n "')):
            raise AssistantError("首个邮箱接入仅支持完整 QQ 邮箱地址。")
        if self.qq_imap_user and len(self.access_token) < 24:
            raise AssistantError("接入私人邮箱前必须配置至少24字符的 ASSISTANT_ACCESS_TOKEN。")
        if self.access_token and len(self.access_token) < 24:
            raise AssistantError("ASSISTANT_ACCESS_TOKEN 至少24字符。")
        if not 30 <= self.mail_poll_seconds <= 3600:
            raise AssistantError("邮箱轮询间隔必须为30～3600秒。")
        if self.mode not in {"demo", "api"}:
            raise AssistantError("ASSISTANT_LLM_MODE 只能为 demo 或 api。")
        if self.mode == "api" and not self.api_key.strip():
            raise AssistantError("API 模式需要在启动环境中配置 ASSISTANT_API_KEY。")
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise AssistantError("ASSISTANT_API_BASE 必须为无凭据、无查询参数的 HTTPS 地址。")
        if not self.model.strip() or not 1 <= self.timeout <= 300:
            raise AssistantError("模型名不能为空，超时必须为 1～300 秒。")
        if not 128 <= self.max_tokens <= 16384 or not 1 <= self.daily_calls <= 10000:
            raise AssistantError("输出额度必须为 128～16384，日调用额度必须为 1～10000。")
        if not 1 <= self.turn_calls <= 500 or not 30 <= self.turn_seconds <= 7200 or not 4000 <= self.context_chars <= 2000000:
            raise AssistantError("会话预算：调用数1～500，耗时30～7200秒，上下文字符4000～2000000。")
        if not self.max_tokens < self.context_tokens <= 2000000 or self.token_count_mode not in {"auto", "off", "moonshot"}:
            raise AssistantError("上下文tokens必须大于预留输出额度且不超过2000000；计数模式为auto/off/moonshot。")
        if (not 1 <= self.browser_calls <= 200 or not 30 <= self.browser_seconds <= 3600
                or not 1 <= self.browser_steps <= 500 or not 1 <= self.browser_default_calls <= 200):
            raise AssistantError("网页任务调用上限必须为 1～200，耗时为 30～3600 秒，动作为 1～500 步。")
        if self.browser_proxy:
            try:
                proxy = urlsplit(self.browser_proxy)
                if (proxy.scheme != "http" or not proxy.hostname or not proxy.port
                        or proxy.username or proxy.password or proxy.path not in {"", "/"}
                        or proxy.query or proxy.fragment):
                    raise ValueError()
            except ValueError:
                raise AssistantError("ASSISTANT_BROWSER_PROXY 请填写 HTTP 代理地址，例如 http://127.0.0.1:7890。") from None
        if (not isinstance(self.extra_body, dict)
                or set(self.extra_body) - {"thinking", "reasoning_effort", "temperature"}):
            raise AssistantError("EXTRA_BODY 仅允许 thinking、reasoning_effort、temperature 参数。")
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise AssistantError("ASSISTANT_TIMEZONE 不是有效时区。") from exc
