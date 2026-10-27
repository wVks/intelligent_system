"""Explicit local/demo and live configuration. Secrets never reach the frontend."""

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_path: Path = PROJECT_ROOT / "var" / "study.db"
    uploads_path: Path | None = None
    mode: str = "offline"
    api_base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    analysis_model: str = ""
    max_calls: int = 6
    request_timeout: float = 45
    tool_timeout: float = 10
    provider_timeout: float = 20
    chat_max_tokens: int = 1000
    analysis_max_tokens: int = 3000
    rate_limit_per_minute: int = 10
    max_concurrent_requests: int = 2
    upload_timeout: float = 20
    live_budget_rub: float | None = None
    live_budget_path: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "database_path", Path(self.database_path))
        object.__setattr__(self, "uploads_path", Path(self.uploads_path) if self.uploads_path else self.database_path.parent / "uploads")
        object.__setattr__(self, "live_budget_path", Path(self.live_budget_path) if self.live_budget_path
                           else self.database_path.parent / "live-budget.json")
        if self.live_budget_rub is not None:
            try:
                budget = float(self.live_budget_rub)
            except (TypeError, ValueError) as exc:
                raise ValueError("live_budget_rub must be in (0, 25]") from exc
            if type(self.live_budget_rub) is bool or not math.isfinite(budget) or not 0 < budget <= 25:
                raise ValueError("live_budget_rub must be in (0, 25]")
            object.__setattr__(self, "live_budget_rub", budget)
        if self.mode not in {"offline", "live"}:
            raise ValueError("STUDYNAV_MODE must be offline or live")
        for name in ("max_calls", "request_timeout", "tool_timeout", "provider_timeout",
                     "rate_limit_per_minute", "max_concurrent_requests", "upload_timeout"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("chat_max_tokens", "analysis_max_tokens"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def live_configured(self):
        url = urlparse(self.api_base_url)
        return bool(self.api_key and self.model and url.hostname and url.scheme == "https")

    @classmethod
    def from_env(cls):
        return cls(
            database_path=Path(os.getenv("STUDYNAV_DB_PATH", str(PROJECT_ROOT / "var/study.db"))),
            uploads_path=Path(os.environ["STUDYNAV_UPLOADS_PATH"]) if os.getenv("STUDYNAV_UPLOADS_PATH") else None,
            mode=os.getenv("STUDYNAV_MODE", "offline"),
            api_base_url=os.getenv("LLM_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", ""),
            model=os.getenv("LLM_MODEL", ""),
            analysis_model=os.getenv("LLM_ANALYSIS_MODEL", ""),
            max_calls=int(os.getenv("STUDYNAV_MAX_TOOL_CALLS", "6")),
            request_timeout=float(os.getenv("STUDYNAV_REQUEST_TIMEOUT", "45")),
            tool_timeout=float(os.getenv("STUDYNAV_TOOL_TIMEOUT", "10")),
            provider_timeout=float(os.getenv("STUDYNAV_PROVIDER_TIMEOUT", "20")),
            chat_max_tokens=int(os.getenv("STUDYNAV_CHAT_MAX_TOKENS", "1000")),
            analysis_max_tokens=int(os.getenv("STUDYNAV_ANALYSIS_MAX_TOKENS", "3000")),
            rate_limit_per_minute=int(os.getenv("STUDYNAV_RATE_LIMIT", "10")),
            live_budget_rub=float(os.environ["STUDYNAV_LIVE_BUDGET_RUB"])
            if os.getenv("STUDYNAV_LIVE_BUDGET_RUB") else None,
            live_budget_path=Path(os.environ["STUDYNAV_LIVE_BUDGET_PATH"])
            if os.getenv("STUDYNAV_LIVE_BUDGET_PATH") else None,
        )
