"""Bar-by-bar backtester.

Execution model (no look-ahead):
- Rules are evaluated on each bar's close; orders fill at the NEXT bar's open (+slippage).
- Stop-loss / take-profit are checked intrabar against high/low; a gap through the
  level fills at the open. If both are touched in one bar, the stop is assumed first.
- Commission and slippage are charged on every fill.

The same simulation drives live signals: whatever order is pending after the last
closed bar is the signal for the next session.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import indicators as ind
from . import StrategyConfig
from .dsl import Evaluator

BARS_PER_YEAR = {"1m": 98280, "5m": 19656, "15m": 6552, "30m": 3276, "1h": 1638, "1d": 252, "1wk": 52}
ACTION_ZH = {"BUY": "买入做多", "SELL": "卖出平多", "SHORT": "卖空", "COVER": "买回平空"}
REASON_ZH = {"signal": "策略信号", "stop": "止损", "take_profit": "止盈", "reverse": "反手"}


@dataclass
class Event:
    time: pd.Timestamp
    action: str          # BUY | SELL | SHORT | COVER
    reason: str          # signal | stop | take_profit | reverse
    price: float

    def to_dict(self) -> dict:
        return {"time": int(self.time.timestamp()), "action": self.action, "reason": self.reason,
                "price": round(self.price, 4)}


@dataclass
class Trade:
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl_pct: float = 0.0
    bars: int = 0

    def to_dict(self) -> dict:
        return {"side": self.side, "entry_time": int(self.entry_time.timestamp()),
                "entry_price": round(self.entry_price, 4),
                "exit_time": int(self.exit_time.timestamp()) if self.exit_time is not None else None,
                "exit_price": round(self.exit_price, 4) if self.exit_price is not None else None,
                "exit_reason": self.exit_reason, "pnl_pct": round(self.pnl_pct, 2), "bars": self.bars}


@dataclass
class Result:
    events: list[Event] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series | None = None
    pending: list[tuple[str, str]] = field(default_factory=list)   # orders for the next bar: (action, reason)
    position: int = 0
    entry_price: float | None = None
    entry_time: pd.Timestamp | None = None
    stop: float | None = None
    target: float | None = None
    metrics: dict = field(default_factory=dict)

    def to_dict(self, include_equity: bool = True) -> dict:
        d = {
            "metrics": self.metrics,
            "events": [e.to_dict() for e in self.events],
            "trades": [t.to_dict() for t in self.trades],
            "pending": [{"action": a, "reason": r} for a, r in self.pending],
            "position": self.position,
            "entry_price": self.entry_price, "entry_time": int(self.entry_time.timestamp()) if self.entry_time is not None else None,
            "stop": self.stop, "target": self.target,
        }
        if include_equity and self.equity is not None:
            d["equity"] = [{"time": int(t.timestamp()), "value": round(float(v), 2)} for t, v in self.equity.items()]
        return d


def position_size(equity: float, price: float, stop: float | None, risk_pct: float, max_pct: float) -> float:
    """Units to buy. With ``risk_pct`` > 0 and a stop, size so that hitting the stop loses about
    ``risk_pct`` % of equity; never exceed ``max_pct`` % of equity in one position."""
    cap = equity * max_pct / 100 / price
    if risk_pct > 0 and stop is not None and abs(price - stop) > 1e-12:
        return min(equity * risk_pct / 100 / abs(price - stop), cap)
    return cap


def compute_rules(df: pd.DataFrame, cfg: StrategyConfig, refs: dict | None = None) -> dict[str, np.ndarray]:
    ev = Evaluator(df, cfg.params, refs=refs)
    out = {}
    for key in ("long_entry", "long_exit", "short_entry", "short_exit"):
        expr = getattr(cfg, key).strip()
        out[key] = ev.eval(expr).to_numpy(dtype=bool) if expr else np.zeros(len(df), dtype=bool)
    return out


def run_backtest(df: pd.DataFrame, cfg: StrategyConfig, commission_bps: float = 5, slippage_bps: float = 5,
                 initial_capital: float = 10_000.0, refs: dict | None = None) -> Result:
    res = Result()
    n = len(df)
    if n < 2:
        res.metrics = summarize(res, df, cfg, initial_capital)
        return res
    rules = compute_rules(df, cfg, refs)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = ind.atr(df, 14).to_numpy() if cfg.atr_stop_mult > 0 else None
    times = df.index
    fee = commission_bps / 1e4
    slip = slippage_bps / 1e4
    frac = cfg.position_pct / 100

    cash = initial_capital
    units = 0.0           # signed: >0 long, <0 short
    pos = 0
    entry_px = 0.0
    entry_i = 0
    stop = target = None
    trade: Trade | None = None
    equity = np.empty(n)
    pending: list[tuple[str, str]] = []

    def fill(i: int, action: str, reason: str, raw_px: float) -> None:
        nonlocal cash, units, pos, entry_px, entry_i, stop, target, trade
        buy = action in ("BUY", "COVER")
        px = raw_px * (1 + slip if buy else 1 - slip)
        if action in ("BUY", "SHORT"):
            d = 1 if action == "BUY" else -1
            base = atr[i - 1] if atr is not None and i > 0 and not math.isnan(atr[i - 1]) else None
            sl, tp = cfg.stop_loss_pct / 100, cfg.take_profit_pct / 100
            stops = []
            if sl > 0:
                stops.append(px * (1 - d * sl))
            if base is not None:
                stops.append(px - d * cfg.atr_stop_mult * base)
            stop = (max(stops) if d == 1 else min(stops)) if stops else None
            target = px * (1 + d * tp) if tp > 0 else None

            eq = cash  # flat -> all equity is cash
            qty = position_size(eq, px, stop, cfg.risk_pct, frac * 100)
            notional = qty * px
            cash -= notional * fee
            if action == "BUY":
                cash -= qty * px
                units, pos = qty, 1
            else:
                cash += qty * px
                units, pos = -qty, -1
            entry_px, entry_i = px, i
            trade = Trade(side="long" if pos == 1 else "short", entry_time=times[i], entry_price=px)
        else:  # SELL / COVER close the position
            cash += units * px
            cash -= abs(units) * px * fee
            if trade is not None:
                trade.exit_time, trade.exit_price, trade.exit_reason = times[i], px, reason
                gross = (px / trade.entry_price - 1) * (1 if trade.side == "long" else -1)
                trade.pnl_pct = (gross - 2 * fee) * 100
                trade.bars = i - entry_i
                res.trades.append(trade)
            units, pos, trade, stop, target = 0.0, 0, None, None, None
        res.events.append(Event(times[i], action, reason, px))

    for i in range(n):
        # 1) execute orders decided at the previous close
        for action, reason in pending:
            fill(i, action, reason, o[i])
        pending = []

        # 2) intrabar stop / take-profit
        if pos != 0 and (stop is not None or target is not None):
            exit_action = "SELL" if pos == 1 else "COVER"
            if pos == 1:
                if stop is not None and l[i] <= stop:
                    fill(i, exit_action, "stop", min(o[i], stop))
                elif target is not None and h[i] >= target:
                    fill(i, exit_action, "take_profit", max(o[i], target))
            else:
                if stop is not None and h[i] >= stop:
                    fill(i, exit_action, "stop", max(o[i], stop))
                elif target is not None and l[i] <= target:
                    fill(i, exit_action, "take_profit", min(o[i], target))

        # 3) decide on this bar's close
        le, lx, se, sx = (rules[k][i] for k in ("long_entry", "long_exit", "short_entry", "short_exit"))
        if pos == 0:
            if le:
                pending = [("BUY", "signal")]
            elif se:
                pending = [("SHORT", "signal")]
        elif pos == 1:
            if se:
                pending = [("SELL", "reverse"), ("SHORT", "signal")]
            elif lx:
                pending = [("SELL", "signal")]
        elif pos == -1:
            if le:
                pending = [("COVER", "reverse"), ("BUY", "signal")]
            elif sx:
                pending = [("COVER", "signal")]

        equity[i] = cash + units * c[i]

    res.equity = pd.Series(equity, index=times)
    res.pending = pending
    res.position = pos
    if pos != 0 and trade is not None:
        res.entry_price, res.entry_time = trade.entry_price, trade.entry_time
        res.stop, res.target = stop, target
        # mark the open trade for reporting (not counted in closed-trade stats)
        open_trade = Trade(trade.side, trade.entry_time, trade.entry_price, exit_reason="open",
                           pnl_pct=((c[-1] / trade.entry_price - 1) * pos) * 100, bars=n - entry_i)
        res.trades.append(open_trade)
    res.metrics = summarize(res, df, cfg, initial_capital)
    return res


def summarize(res: Result, df: pd.DataFrame, cfg: StrategyConfig, initial: float) -> dict:
    closed = [t for t in res.trades if t.exit_reason != "open"]
    eq = res.equity if res.equity is not None else pd.Series([initial])
    final = float(eq.iloc[-1])
    rets = eq.pct_change().dropna()
    bpy = BARS_PER_YEAR.get(cfg.interval, 252)
    years = max(len(eq) / bpy, 1e-9)
    peak = eq.cummax()
    dd = float(((eq / peak) - 1).min() * 100) if len(eq) else 0.0
    sharpe = float(rets.mean() / rets.std() * math.sqrt(bpy)) if len(rets) > 1 and rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    sortino = float(rets.mean() / downside.std() * math.sqrt(bpy)) if len(downside) > 1 and downside.std() > 0 else 0.0
    wins = [t.pnl_pct for t in closed if t.pnl_pct > 0]
    losses = [t.pnl_pct for t in closed if t.pnl_pct <= 0]
    total = (final / initial - 1) * 100
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if final > 0 and years >= 0.25 else None
    bh = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100 if len(df) > 1 else 0.0
    in_market = sum(t.bars for t in res.trades)
    return {
        "total_return_pct": round(total, 2),
        "cagr_pct": round(cagr, 2) if cagr is not None else None,
        "buy_hold_pct": round(bh, 2),
        "max_drawdown_pct": round(dd, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "exposure_pct": round(in_market / max(len(df), 1) * 100, 1),
        "final_equity": round(final, 2),
        "bars": len(df),
        "start": int(df.index[0].timestamp()) if len(df) else None,
        "end": int(df.index[-1].timestamp()) if len(df) else None,
    }
