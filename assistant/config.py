import json
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
    max_tokens: int = 4096
    daily_calls: int = 30
    timezone: str = "Asia/Shanghai"
    extra_body: dict = field(default_factory=dict)
    browser_calls: int = 40
    browser_seconds: int = 900
    browser_steps: int = 120
    browser_vision: bool = False
    browser_dns: str = "system"

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
                max_tokens=int(os.getenv("ASSISTANT_MAX_TOKENS", "4096")),
                daily_calls=int(os.getenv("ASSISTANT_DAILY_CALLS", "30")),
                timezone=os.getenv("ASSISTANT_TIMEZONE", "Asia/Shanghai"),
                extra_body=json.loads(os.getenv("ASSISTANT_LLM_EXTRA_BODY", "{}")),
                browser_calls=int(os.getenv("ASSISTANT_BROWSER_CALLS", "40")),
                browser_seconds=int(os.getenv("ASSISTANT_BROWSER_SECONDS", "900")),
                browser_steps=int(os.getenv("ASSISTANT_BROWSER_STEPS", "120")),
                browser_vision=os.getenv("ASSISTANT_BROWSER_VISION", "false").lower() == "true",
                browser_dns=os.getenv("ASSISTANT_BROWSER_DNS", "system"),
            )
        except (ValueError, TypeError) as exc:
            raise AssistantError("环境变量配置格式错误，请检查整数及 EXTRA_BODY JSON。") from exc
        config.validate()
        return config

    def validate(self):
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
        if (not 1 <= self.browser_calls <= 200 or not 30 <= self.browser_seconds <= 3600
                or not 1 <= self.browser_steps <= 500):
            raise AssistantError("网页任务调用上限必须为 1～200，耗时为 30～3600 秒，动作为 1～500 步。")
        if self.browser_dns not in {"system", "google"}:
            raise AssistantError("ASSISTANT_BROWSER_DNS 只能为 system 或 google。")
        if (not isinstance(self.extra_body, dict)
                or set(self.extra_body) - {"thinking", "reasoning_effort", "temperature"}):
            raise AssistantError("EXTRA_BODY 仅允许 thinking、reasoning_effort、temperature 参数。")
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise AssistantError("ASSISTANT_TIMEZONE 不是有效时区。") from exc
