"""Run one strategy across many symbols: who has a signal right now, and how robust is it?"""
from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..market_data import BarStore, closed_bars
from . import StrategyConfig
from .backtest import run_backtest
from .refs import load_refs
from .signals import HISTORY_BARS, reference_levels

log = logging.getLogger(__name__)

MAX_SYMBOLS = 60


def _scan_one(store: BarStore, cfg: StrategyConfig, sym: str, commission_bps: float, slippage_bps: float,
              now: datetime | None, refs: dict | None = None) -> dict:
    df = store.get(sym, cfg.interval, HISTORY_BARS)
    bars = closed_bars(df, cfg.interval, now)
    if len(bars) < 30:
        return {"symbol": sym, "error": "数据不足"}
    res = run_backtest(bars, cfg, commission_bps, slippage_bps, refs=refs)
    m = res.metrics
    close = float(bars["close"].iloc[-1])
    prev = float(bars["close"].iloc[-2])
    pending = [{"action": a, "reason": r, **reference_levels(bars, cfg, a)} for a, r in res.pending]
    return {
        "symbol": sym, "close": round(close, 4), "change_pct": round((close / prev - 1) * 100, 2),
        "bar_ts": int(bars.index[-1].timestamp()), "position": res.position,
        "entry_price": round(res.entry_price, 4) if res.entry_price else None,
        "unrealized_pct": round((close / res.entry_price - 1) * 100 * res.position, 2) if res.position and res.entry_price else None,
        "pending": pending,
        "total_return_pct": m["total_return_pct"], "buy_hold_pct": m["buy_hold_pct"],
        "max_drawdown_pct": m["max_drawdown_pct"], "win_rate_pct": m["win_rate_pct"], "trades": m["trades"],
        "sharpe": m["sharpe"], "profit_factor": m["profit_factor"],
    }


def scan(store: BarStore, cfg: StrategyConfig, symbols: list[str], commission_bps: float = 5,
         slippage_bps: float = 5, fetch: bool = True, now: datetime | None = None) -> dict:
    symbols = list(dict.fromkeys(s.upper() for s in symbols if s))[:MAX_SYMBOLS]
    if fetch:
        # Pull data concurrently; providers are I/O bound. The store throttles repeat pulls.
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda s: store.maybe_update(s, cfg.interval, min_gap_s=300), symbols))
    refs = load_refs(store, cfg, HISTORY_BARS, update=fetch)
    rows = []
    for sym in symbols:
        try:
            rows.append(_scan_one(store, cfg, sym, commission_bps, slippage_bps, now, refs))
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not break the scan
            log.warning("scan %s failed: %s", sym, exc)
            rows.append({"symbol": sym, "error": str(exc)[:200]})

    ok = [r for r in rows if "error" not in r]
    # Sort: fresh entry signals first, then open positions, then by backtest return
    def key(r):
        entry = any(p["action"] in ("BUY", "SHORT") for p in r.get("pending", []))
        return (0 if entry else 1 if r.get("position") else 2, -(r.get("total_return_pct") or -1e9))
    rows.sort(key=lambda r: (1, 0) if "error" in r else (0, *key(r)))

    summary = {"symbols": len(symbols), "ok": len(ok)}
    if ok:
        rets = [r["total_return_pct"] for r in ok]
        summary.update({
            "median_return_pct": round(statistics.median(rets), 2),
            "mean_return_pct": round(statistics.fmean(rets), 2),
            "profitable_pct": round(sum(x > 0 for x in rets) / len(ok) * 100, 1),
            "beat_buy_hold_pct": round(sum(r["total_return_pct"] > r["buy_hold_pct"] for r in ok) / len(ok) * 100, 1),
            "median_max_drawdown_pct": round(statistics.median(r["max_drawdown_pct"] for r in ok), 2),
            "total_trades": sum(r["trades"] for r in ok),
            "entry_signals": sum(any(p["action"] in ("BUY", "SHORT") for p in r["pending"]) for r in ok),
            "exit_signals": sum(any(p["action"] in ("SELL", "COVER") for p in r["pending"]) for r in ok),
            "in_position": sum(1 for r in ok if r["position"]),
        })
    return {"interval": cfg.interval, "strategy": cfg.name, "summary": summary, "rows": rows}
