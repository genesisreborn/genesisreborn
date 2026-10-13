"""Background jobs: periodic refresh, intraday alerts, daily Telegram briefs."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .db import Database
from .pipeline import run_cycle
from .telegram_bot import TelegramNotifier

log = logging.getLogger(__name__)


class Engine:
    """Owns the DB, notifier and scheduler; shared by the CLI and the web app."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.notifier = TelegramNotifier(settings, self.db)
        self.scheduler = BackgroundScheduler(timezone=settings.timezone)

    def refresh(self) -> dict:
        try:
            snap = run_cycle(self.db, self.settings)
        except Exception:
            log.exception("refresh cycle failed")
            raise
        if self.settings.telegram_enabled:
            fired = self.notifier.check_alerts(snap)
            if fired:
                log.info("alerts sent for %s", fired)
        return snap

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            pass

    def start(self) -> None:
        s = self.settings
        self.scheduler.add_job(self._safe_refresh, "interval", minutes=s.refresh_minutes, id="refresh",
                               next_run_time=datetime.now(ZoneInfo(s.timezone)) + timedelta(seconds=2),
                               max_instances=1, coalesce=True)
        for hhmm in s.daily_report_times:
            h, m = (int(x) for x in hhmm.split(":"))
            self.scheduler.add_job(self.notifier.send_daily_report, CronTrigger(hour=h, minute=m, timezone=s.timezone),
                                   id=f"daily-{hhmm}", max_instances=1, coalesce=True)
        self.scheduler.start()
        if s.telegram_enabled:
            self.notifier.start_polling()
        log.info("scheduler started: refresh every %d min, reports at %s (%s), telegram=%s, claude=%s, demo=%s",
                 s.refresh_minutes, s.daily_report_times, s.timezone, s.telegram_enabled, s.claude_enabled,
                 s.demo_mode)

    def shutdown(self) -> None:
        self.notifier.stop()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
