"""Background jobs.

- refresh (every REFRESH_MINUTES): news + quotes + scoring, rule-based views. No Claude.
- daily report (DAILY_REPORT_TIMES): one Claude call -> report -> Telegram.
- signals (every SIGNAL_REFRESH_MINUTES, only if strategies are enabled): pull new bars,
  run enabled strategies, push new trade signals to Telegram.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .db import Database
from .pipeline import run_cycle
from .report import generate_report
from .brief import build_brief
from .broker import BrokerExecutor
from .portfolio import Portfolio
from .realtime import RealtimeMonitor
from .strategy.alerts import AlertEngine
from .strategy.paper import PaperAccount
from .strategy.signals import SignalEngine
from .telegram_bot import TelegramNotifier, format_portfolio

log = logging.getLogger(__name__)


class Engine:
    """Owns the DB, notifier and scheduler; shared by the CLI and the web app."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.notifier = TelegramNotifier(settings, self.db)
        self.signals = SignalEngine(self.db, settings)
        self.alerts = AlertEngine(self.db, self.signals.store)
        self.paper = PaperAccount(self.db, self.signals.store, settings)
        self.broker = BrokerExecutor(self.db, settings)
        self.realtime = RealtimeMonitor(self.db, settings, on_fire=self._realtime_fired)
        self.portfolio = Portfolio(self.db, self.signals.store)
        self.notifier.brief_fn = lambda: self.brief()["text"]
        self.notifier.portfolio_fn = lambda: format_portfolio(self.portfolio.summary(history=False))
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

    def report(self, force: bool = False, notify: bool = False) -> dict:
        """Generate an AI report (one Claude call); optionally push it to Telegram."""
        snap = generate_report(self.db, self.settings, force=force)
        if notify and self.settings.telegram_enabled:
            n = self.notifier.send_report(snap)
            log.info("report #%s sent to %d chat(s)", snap["report_id"], n)
        return snap

    def brief(self, update: bool = True) -> dict:
        return build_brief(self.db, self.settings, self.signals.store, self.portfolio, update=update)

    def _scheduled_brief(self) -> None:
        try:
            if self.settings.telegram_enabled:
                if not self.settings.telegram_chat_ids:
                    log.info("brief skipped: set TELEGRAM_CHAT_IDS (it contains your holdings)")
                    return
                self.notifier.send_private(self.brief()["text"])
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scheduled brief failed")

    def _scheduled_report(self) -> None:
        try:
            self.report(force=True, notify=True)
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scheduled report failed")

    def run_signals(self) -> list[dict]:
        new = self.signals.run()
        tg = self.settings.telegram_enabled
        for sig in new:
            # signals are already stored (and deduped) at this point; one failure must not drop the rest
            try:
                if tg and sig.get("notify", True):
                    self.notifier.send_signal(sig)
                if sig.get("execution") == "paper":
                    self.paper.submit(sig)
                elif sig.get("execution") == "broker":
                    rec = self.broker.execute(sig, sig.get("symbols") or [], sig.get("alloc_pct", 10))
                    if tg and not (rec["status"] == "skipped" and "重复提交" in rec["detail"]):
                        self.notifier.send_execution(rec)
            except Exception:  # noqa: BLE001
                log.exception("handling signal %s failed", sig.get("id"))
        # fill pending paper orders / check paper stops on every run
        for ev in self.paper.process():
            if tg:
                self.notifier.send_paper_event(ev)
        return new

    def _realtime_fired(self, alert: dict) -> None:
        if self.settings.telegram_enabled:
            self.notifier.send_realtime_alert(alert)

    def run_alerts(self) -> list[dict]:
        fired = self.alerts.run()
        if self.settings.telegram_enabled:
            for a in fired:
                self.notifier.send_alert(a)
        return fired

    def _scheduled_signals(self) -> None:
        try:
            for sym in self.db.get_watchlist(self.settings.watchlist):  # keep watchlist quotes fresh
                self.signals.store.maybe_update(sym, "1d", min_gap_s=60)
            if self.db.list_strategies(enabled_only=True) or self.db.query(
                    "SELECT 1 FROM paper_orders WHERE status='pending' UNION SELECT 1 FROM paper_positions LIMIT 1"):
                self.run_signals()
            if self.db.list_alerts(active_only=True):
                self.run_alerts()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("signal run failed")

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scheduled refresh failed")

    def start(self) -> None:
        s = self.settings
        for hhmm in s.brief_times:
            h, m = (int(x) for x in hhmm.split(":"))
            self.scheduler.add_job(self._scheduled_brief, CronTrigger(day_of_week="mon-fri", hour=h, minute=m,
                                                                      timezone=s.timezone),
                                   id=f"brief-{hhmm}", max_instances=1, coalesce=True)
        self.scheduler.add_job(self._safe_refresh, "interval", minutes=s.refresh_minutes, id="refresh",
                               next_run_time=datetime.now(ZoneInfo(s.timezone)) + timedelta(seconds=2),
                               max_instances=1, coalesce=True)
        for hhmm in s.daily_report_times:
            h, m = (int(x) for x in hhmm.split(":"))
            self.scheduler.add_job(self._scheduled_report, CronTrigger(hour=h, minute=m, timezone=s.timezone),
                                   id=f"daily-{hhmm}", max_instances=1, coalesce=True)
        self.scheduler.add_job(self._scheduled_signals, "interval", minutes=s.signal_refresh_minutes, id="signals",
                               next_run_time=datetime.now(ZoneInfo(s.timezone)) + timedelta(seconds=20),
                               max_instances=1, coalesce=True)
        self.scheduler.start()
        if s.telegram_enabled:
            self.notifier.start_polling()
        self.realtime.start()
        log.info("scheduler started: refresh every %d min, reports at %s (%s), telegram=%s, claude=%s, demo=%s",
                 s.refresh_minutes, s.daily_report_times, s.timezone, s.telegram_enabled, s.claude_enabled,
                 s.demo_mode)

    def shutdown(self) -> None:
        self.notifier.stop()
        self.realtime.stop()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
