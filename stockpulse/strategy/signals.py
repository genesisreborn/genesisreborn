"""Live signal generation: run each enabled strategy on fresh closed bars and record new signals."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import pandas as pd

from .. import indicators as ind
from ..config import Settings
from ..db import Database
from ..market_data import BarStore, closed_bars
from . import StrategyConfig
from .backtest import ACTION_ZH, REASON_ZH, run_backtest

log = logging.getLogger(__name__)

HISTORY_BARS = 1500  # enough warm-up for 200-period indicators on any interval


def reference_levels(df: pd.DataFrame, cfg: StrategyConfig, action: str) -> dict:
    """Indicative stop / target for an entry at the last close (actual fill is next open)."""
    if action not in ("BUY", "SHORT") or df.empty:
        return {}
    px = float(df["close"].iloc[-1])
    d = 1 if action == "BUY" else -1
    stops = []
    if cfg.stop_loss_pct > 0:
        stops.append(px * (1 - d * cfg.stop_loss_pct / 100))
    if cfg.atr_stop_mult > 0:
        a = ind.atr(df, 14).iloc[-1]
        if not math.isnan(a):
            stops.append(px - d * cfg.atr_stop_mult * float(a))
    out = {}
    if stops:
        out["stop"] = round(max(stops) if d == 1 else min(stops), 4)
    if cfg.take_profit_pct > 0:
        out["target"] = round(px * (1 + d * cfg.take_profit_pct / 100), 4)
    return out


def evaluate(cfg: StrategyConfig, df: pd.DataFrame, now: datetime | None = None) -> dict:
    """Run the strategy over closed bars; return state and the signals produced by the last closed bar."""
    bars = closed_bars(df, cfg.interval, now)
    if len(bars) < 2:
        return {"bars": len(bars), "signals": [], "position": 0}
    res = run_backtest(bars, cfg)
    last_ts = bars.index[-1]
    last_close = float(bars["close"].iloc[-1])
    signals = []
    # Stops / targets hit during the last closed bar
    for e in res.events:
        if e.time == last_ts and e.reason in ("stop", "take_profit"):
            signals.append({"action": e.action, "reason": e.reason, "price": round(e.price, 4)})
    # Orders decided on the last close, to be executed at the next open
    for action, reason in res.pending:
        signals.append({"action": action, "reason": reason, "price": round(last_close, 4),
                        **reference_levels(bars, cfg, action)})
    unreal = None
    if res.position != 0 and res.entry_price:
        unreal = round((last_close / res.entry_price - 1) * 100 * res.position, 2)
    return {
        "bars": len(bars), "bar_ts": int(last_ts.timestamp()), "close": last_close,
        "position": res.position, "entry_price": res.entry_price,
        "entry_time": int(res.entry_time.timestamp()) if res.entry_time is not None else None,
        "stop": res.stop, "target": res.target, "unrealized_pct": unreal,
        "signals": signals, "metrics": res.metrics,
    }


class SignalEngine:
    def __init__(self, db: Database, settings: Settings, store: BarStore | None = None):
        self.db = db
        self.settings = settings
        self.store = store or BarStore(db, settings)

    def strategies(self) -> list[StrategyConfig]:
        return [StrategyConfig.from_dict(d) for d in self.db.list_strategies(enabled_only=True)]

    def run(self, update: bool = True, now: datetime | None = None) -> list[dict]:
        """Update bars and evaluate every enabled strategy. Returns newly recorded signals."""
        updated: set[tuple[str, str]] = set()
        new: list[dict] = []
        for cfg in self.strategies():
            for sym in cfg.symbols:
                key = (sym, cfg.interval)
                if update and key not in updated:
                    self.store.update(sym, cfg.interval)
                    updated.add(key)
                df = self.store.get(sym, cfg.interval, HISTORY_BARS, refresh_if_empty=not update)
                try:
                    state = evaluate(cfg, df, now)
                except Exception as exc:  # noqa: BLE001 - one bad strategy must not stop the rest
                    log.warning("strategy %s on %s failed: %s", cfg.name, sym, exc)
                    continue
                for s in state["signals"]:
                    sig = {"strategy_id": cfg.id, "strategy": cfg.name, "symbol": sym, "interval": cfg.interval,
                           "bar_ts": state["bar_ts"], **s}
                    sid = self.db.add_signal(sig)
                    if sid:
                        new.append({**sig, "id": sid, "notify": cfg.notify})
        if new:
            log.info("new signals: %s", [(s["symbol"], s["action"], s["strategy"]) for s in new])
        return new

    def positions(self, now: datetime | None = None) -> list[dict]:
        """Current simulated position of every enabled strategy x symbol (no data fetch)."""
        out = []
        for cfg in self.strategies():
            for sym in cfg.symbols:
                df = self.store.get(sym, cfg.interval, HISTORY_BARS, refresh_if_empty=False)
                if df.empty:
                    continue
                try:
                    st = evaluate(cfg, df, now)
                except Exception as exc:  # noqa: BLE001
                    log.warning("positions: %s on %s failed: %s", cfg.name, sym, exc)
                    continue
                st.pop("metrics", None)
                out.append({"strategy_id": cfg.id, "strategy": cfg.name, "symbol": sym, "interval": cfg.interval, **st})
        return out


def describe_signal(sig: dict) -> str:
    """Plain-language one-liner, e.g. '买入做多 AAPL @ 231.50（策略信号）'."""
    return f"{ACTION_ZH.get(sig['action'], sig['action'])} {sig['symbol']} @ {sig['price']}（{REASON_ZH.get(sig['reason'], sig['reason'])}）"


def bar_time_str(ts: int, interval: str) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d") if interval in ("1d", "1wk") else dt.strftime("%Y-%m-%d %H:%M UTC")
