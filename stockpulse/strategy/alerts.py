"""TradingView-style alerts: a rule-language condition on one symbol, checked on closed bars."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from ..db import Database
from ..market_data import INTERVALS, BarStore, closed_bars
from . import SYMBOL_RE
from .dsl import DSLError, Evaluator, validate

log = logging.getLogger(__name__)

ALERT_BARS = 400


@dataclass
class Alert:
    symbol: str
    condition: str
    interval: str = "1d"
    mode: str = "once"           # once: fire then deactivate | every_bar: fire on every closed bar it holds
    note: str = ""
    level: float | None = None   # price level for "price crosses" alerts (drawn on the chart)
    kind: str = "custom"         # cross_up | cross_down | above | below | ... | custom
    realtime: bool = False       # price-level alerts only: check live prices instead of closed bars
    active: bool = True
    id: int | None = None
    last_bar_ts: int | None = None
    last_triggered: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Alert":
        known = set(cls.__dataclass_fields__)
        a = cls(**{k: v for k, v in d.items() if k in known})
        a.symbol = (a.symbol or "").strip().upper()
        a.condition = (a.condition or "").strip()
        a.note = (a.note or "").strip()[:200]
        a.level = float(a.level) if a.level not in (None, "") else None
        return a

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        errors = []
        if not SYMBOL_RE.match(self.symbol):
            errors.append("代码格式不对")
        if self.interval not in INTERVALS:
            errors.append(f"周期必须是 {', '.join(INTERVALS)} 之一")
        if self.mode not in ("once", "every_bar"):
            errors.append("触发方式必须是 once 或 every_bar")
        if self.realtime and (self.kind not in ("cross_up", "cross_down", "above", "below") or self.level is None):
            errors.append("只有价格类提醒（上穿/下穿/高于/低于）可以设为盘中实时")
        try:
            validate(self.condition)
        except DSLError as exc:
            errors.append(f"条件：{exc}")
        return errors


class AlertEngine:
    def __init__(self, db: Database, store: BarStore):
        self.db = db
        self.store = store

    def run(self, update: bool = True, now: datetime | None = None) -> list[dict]:
        """Check every active alert on its latest closed bar; returns the alerts that fired."""
        fired = []
        for raw in self.db.list_alerts(active_only=True):
            a = Alert.from_dict(raw)
            if a.realtime:
                continue  # handled by the real-time monitor
            if update:
                self.store.maybe_update(a.symbol, a.interval, min_gap_s=30)
            bars = closed_bars(self.store.get(a.symbol, a.interval, ALERT_BARS, refresh_if_empty=not update),
                               a.interval, now)
            if len(bars) < 2:
                continue
            bar_ts = int(bars.index[-1].timestamp())
            if a.last_bar_ts == bar_ts:
                continue  # already evaluated this bar
            try:
                hit = bool(Evaluator(bars).eval(a.condition).iloc[-1])
            except DSLError as exc:
                log.warning("alert #%s invalid: %s", a.id, exc)
                continue
            if hit:
                event = {**a.to_dict(), "bar_ts": bar_ts, "close": round(float(bars["close"].iloc[-1]), 4)}
                self.db.mark_alert(a.id, bar_ts, triggered=True, deactivate=a.mode == "once")
                fired.append(event)
            else:
                self.db.mark_alert(a.id, bar_ts, triggered=False, deactivate=False)
        return fired
