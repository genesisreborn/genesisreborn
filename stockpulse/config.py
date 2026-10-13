"""Runtime configuration loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_ids: list[str] = field(default_factory=lambda: _list("TELEGRAM_CHAT_IDS"))

    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))

    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Shanghai"))
    refresh_minutes: int = field(default_factory=lambda: int(os.getenv("REFRESH_MINUTES", "15")))
    daily_report_times: list[str] = field(default_factory=lambda: _list("DAILY_REPORT_TIMES", "08:30,21:00"))
    alert_top_n: int = field(default_factory=lambda: int(os.getenv("ALERT_TOP_N", "5")))
    alert_min_score: float = field(default_factory=lambda: float(os.getenv("ALERT_MIN_SCORE", "70")))

    # Claude is called only when a report is generated; this blocks repeated
    # manual triggers inside the window (scheduled reports always run).
    report_cooldown_minutes: int = field(default_factory=lambda: int(os.getenv("REPORT_COOLDOWN_MINUTES", "30")))
    # If set, POST /api/report requires "Authorization: Bearer <token>" or ?token=
    report_api_token: str = field(default_factory=lambda: os.getenv("REPORT_API_TOKEN", ""))
    reports_dir: str = field(default_factory=lambda: os.getenv("REPORTS_DIR", "data/reports"))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/stockpulse.db"))
    watchlist: list[str] = field(default_factory=lambda: [t.upper() for t in _list("WATCHLIST", "AAPL,NVDA,TSLA,MSFT,AMZN")])
    enable_social: bool = field(default_factory=lambda: _bool("ENABLE_SOCIAL", True))
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))

    # Analysis knobs
    lookback_hours: int = 24
    baseline_days: int = 7
    max_candidates: int = 40
    top_n: int = 15

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key) and not self.demo_mode


settings = Settings()
