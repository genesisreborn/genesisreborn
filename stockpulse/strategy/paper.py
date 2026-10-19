"""Paper trading account: strategy signals become simulated orders with realistic fills.

- Orders are created from live signals and filled at the open of the bar AFTER the
  signal bar (+ slippage, + commission), exactly like the backtester.
- Stops / targets are checked against later closed bars' high / low; a gap through
  the level fills at the open.
- One position per strategy x symbol. Long and short are supported.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import pandas as pd

from .. import indicators as ind
from ..config import Settings
from ..db import Database, iso, utcnow
from ..market_data import BarStore, closed_bars
from . import StrategyConfig
from .backtest import position_size

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER UNIQUE,
    strategy_id INTEGER, strategy TEXT, symbol TEXT NOT NULL, interval TEXT NOT NULL,
    action TEXT NOT NULL, reason TEXT, signal_bar_ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | filled | rejected | cancelled
    qty REAL, fill_price REAL, fill_ts INTEGER, note TEXT, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER, strategy TEXT, symbol TEXT NOT NULL, interval TEXT NOT NULL,
    qty REAL NOT NULL, entry_price REAL NOT NULL, entry_ts INTEGER NOT NULL,
    stop REAL, target REAL, checked_ts INTEGER NOT NULL,
    UNIQUE (strategy_id, symbol)
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER, strategy TEXT, symbol TEXT, side TEXT, qty REAL,
    entry_price REAL, entry_ts INTEGER, exit_price REAL, exit_ts INTEGER,
    pnl REAL, pnl_pct REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_equity (
    day TEXT PRIMARY KEY, equity REAL NOT NULL
);
"""


class PaperAccount:
    def __init__(self, db: Database, store: BarStore, settings: Settings):
        self.db = db
        self.store = store
        self.settings = settings
        with db.tx() as c:
            c.executescript(SCHEMA)
        if db.get_kv("paper_cash") is None:
            self.reset(settings.paper_capital)

    # ------------------------------------------------------------ account
    def reset(self, capital: float | None = None) -> None:
        capital = float(capital or self.settings.paper_capital)
        if not math.isfinite(capital) or capital <= 0:
            raise ValueError("初始资金必须是正数")
        with self.db.tx() as c:
            for t in ("paper_orders", "paper_positions", "paper_trades", "paper_equity"):
                c.execute(f"DELETE FROM {t}")
        self.db.set_kv("paper_cash", str(capital))
        self.db.set_kv("paper_initial", str(capital))
        self.db.set_kv("paper_started", iso(utcnow()))

    @property
    def cash(self) -> float:
        return float(self.db.get_kv("paper_cash", "0"))

    def _set_cash(self, v: float) -> None:
        self.db.set_kv("paper_cash", repr(float(v)))

    def last_price(self, symbol: str, interval: str) -> float | None:
        df = self.store.get(symbol, interval, 2, refresh_if_empty=False)
        return float(df["close"].iloc[-1]) if len(df) else None

    def positions(self) -> list[dict]:
        out = []
        for r in self.db.query("SELECT * FROM paper_positions ORDER BY id"):
            p = dict(r)
            px = self.last_price(p["symbol"], p["interval"]) or p["entry_price"]
            p["last_price"] = round(px, 4)
            p["market_value"] = round(p["qty"] * px, 2)
            p["unrealized"] = round((px - p["entry_price"]) * p["qty"], 2)
            p["unrealized_pct"] = round((px / p["entry_price"] - 1) * 100 * (1 if p["qty"] > 0 else -1), 2)
            out.append(p)
        return out

    def equity(self, positions: list[dict] | None = None) -> float:
        positions = self.positions() if positions is None else positions
        return self.cash + sum(p["market_value"] for p in positions)

    # ------------------------------------------------------------ orders
    def submit(self, sig: dict) -> dict | None:
        """Queue an order for a live signal. Stop/target signals are ignored: the account runs its own stops."""
        if sig.get("reason") in ("stop", "take_profit"):
            return None
        pos = self._position(sig["strategy_id"], sig["symbol"])
        action = sig["action"]
        if action in ("BUY", "SHORT") and pos is not None:
            # A reversal arrives as COVER/SELL + entry; the exit is queued first, so allow it then.
            pending_exit = self.db.query(
                "SELECT 1 FROM paper_orders WHERE strategy_id=? AND symbol=? AND status='pending' AND action IN ('SELL','COVER')",
                (sig["strategy_id"], sig["symbol"]))
            if not pending_exit:
                return None
        if action == "SELL" and (pos is None or pos["qty"] <= 0):
            return None
        if action == "COVER" and (pos is None or pos["qty"] >= 0):
            return None
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO paper_orders(signal_id, strategy_id, strategy, symbol, interval, action, reason,"
                " signal_bar_ts, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (sig.get("id"), sig["strategy_id"], sig["strategy"], sig["symbol"], sig["interval"], action,
                 sig.get("reason", "signal"), sig["bar_ts"], iso(utcnow())))
            if not cur.rowcount:
                return None
            oid = cur.lastrowid
        return dict(self.db.query("SELECT * FROM paper_orders WHERE id=?", (oid,))[0])

    def _position(self, strategy_id, symbol) -> dict | None:
        rows = self.db.query("SELECT * FROM paper_positions WHERE strategy_id=? AND symbol=?", (strategy_id, symbol))
        return dict(rows[0]) if rows else None

    def _cfg(self, strategy_id) -> StrategyConfig | None:
        raw = self.db.get_strategy(strategy_id) if strategy_id else None
        return StrategyConfig.from_dict(raw) if raw else None

    def process(self, now: datetime | None = None, update: bool = True) -> list[dict]:
        """Fill pending orders whose next bar has opened; check stops. Returns fill events."""
        events: list[dict] = []
        orders = [dict(r) for r in self.db.query("SELECT * FROM paper_orders WHERE status='pending' ORDER BY id")]
        if update:
            needed = {(o["symbol"], o["interval"]) for o in orders}
            needed |= {(r["symbol"], r["interval"]) for r in self.db.query("SELECT symbol, interval FROM paper_positions")}
            for sym, iv in needed:
                self.store.maybe_update(sym, iv, min_gap_s=60)
        for o in orders:
            df = self.store.get(o["symbol"], o["interval"], 300, refresh_if_empty=False)
            after = df[df.index > pd.Timestamp(o["signal_bar_ts"], unit="s", tz="UTC")]
            if after.empty:
                continue
            bar_ts, open_px = after.index[0], float(after["open"].iloc[0])
            ev = self._fill(o, open_px, int(bar_ts.timestamp()), df[df.index <= pd.Timestamp(o["signal_bar_ts"], unit="s", tz="UTC")])
            if ev:
                events.append(ev)
        events += self._check_stops(now)
        self._record_equity()
        return events

    def _fill(self, o: dict, raw_px: float, bar_ts: int, history: pd.DataFrame) -> dict | None:
        slip = self.settings.slippage_bps / 1e4
        fee = self.settings.commission_bps / 1e4
        buy = o["action"] in ("BUY", "COVER")
        px = raw_px * (1 + slip if buy else 1 - slip)
        pos = self._position(o["strategy_id"], o["symbol"])

        if o["action"] in ("SELL", "COVER"):
            if pos is None:
                return self._reject(o, "没有对应持仓")
            return self._close(pos, px, bar_ts, "signal", order=o)

        if pos is not None:
            return self._reject(o, "已有持仓")
        cfg = self._cfg(o["strategy_id"])
        d = 1 if o["action"] == "BUY" else -1
        stop, target = self._levels(cfg, px, d, history)
        eq = self.equity()
        qty = position_size(eq, px, stop, cfg.risk_pct if cfg else 0, cfg.alloc_pct if cfg else 10)
        notional = qty * px
        if o["action"] == "BUY":
            notional = min(notional, self.cash / (1 + fee))
        if notional <= 1:
            return self._reject(o, "现金不足")
        qty = notional / px
        signed = qty * d
        cash = self.cash - signed * px - abs(qty) * px * fee
        self._set_cash(cash)
        with self.db.tx() as c:
            c.execute("INSERT INTO paper_positions(strategy_id, strategy, symbol, interval, qty, entry_price, entry_ts,"
                      " stop, target, checked_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (o["strategy_id"], o["strategy"], o["symbol"], o["interval"], signed, px, bar_ts, stop, target,
                       bar_ts - 1))
            c.execute("UPDATE paper_orders SET status='filled', qty=?, fill_price=?, fill_ts=? WHERE id=?",
                      (signed, px, bar_ts, o["id"]))
        return {"type": "fill", "symbol": o["symbol"], "action": o["action"], "qty": round(qty, 4), "price": round(px, 4),
                "strategy": o["strategy"], "stop": stop, "target": target}

    @staticmethod
    def _levels(cfg: StrategyConfig | None, px: float, d: int, history: pd.DataFrame) -> tuple[float | None, float | None]:
        if cfg is None:
            return None, None
        stops = []
        if cfg.stop_loss_pct > 0:
            stops.append(px * (1 - d * cfg.stop_loss_pct / 100))
        if cfg.atr_stop_mult > 0 and len(history) > 15:
            a = ind.atr(history, 14).iloc[-1]
            if not math.isnan(a):
                stops.append(px - d * cfg.atr_stop_mult * float(a))
        stop = (max(stops) if d == 1 else min(stops)) if stops else None
        target = px * (1 + d * cfg.take_profit_pct / 100) if cfg.take_profit_pct > 0 else None
        return (round(stop, 4) if stop else None), (round(target, 4) if target else None)

    def _reject(self, o: dict, note: str) -> dict:
        with self.db.tx() as c:
            c.execute("UPDATE paper_orders SET status='rejected', note=? WHERE id=?", (note, o["id"]))
        return {"type": "rejected", "symbol": o["symbol"], "action": o["action"], "note": note, "strategy": o["strategy"]}

    def _close(self, pos: dict, px: float, bar_ts: int, reason: str, order: dict | None = None) -> dict:
        fee = self.settings.commission_bps / 1e4
        qty = pos["qty"]
        self._set_cash(self.cash + qty * px - abs(qty) * px * fee)
        entry_fee = abs(qty) * pos["entry_price"] * fee
        pnl = (px - pos["entry_price"]) * qty - abs(qty) * px * fee - entry_fee
        pnl_pct = pnl / (abs(qty) * pos["entry_price"]) * 100
        side = "long" if qty > 0 else "short"
        with self.db.tx() as c:
            c.execute("DELETE FROM paper_positions WHERE id=?", (pos["id"],))
            c.execute("INSERT INTO paper_trades(strategy_id, strategy, symbol, side, qty, entry_price, entry_ts, exit_price,"
                      " exit_ts, pnl, pnl_pct, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (pos["strategy_id"], pos["strategy"], pos["symbol"], side, abs(qty), pos["entry_price"],
                       pos["entry_ts"], px, bar_ts, pnl, pnl_pct, reason))
            if order:
                c.execute("UPDATE paper_orders SET status='filled', qty=?, fill_price=?, fill_ts=? WHERE id=?",
                          (-qty, px, bar_ts, order["id"]))
        return {"type": "close", "symbol": pos["symbol"], "action": "SELL" if qty > 0 else "COVER", "reason": reason,
                "qty": round(abs(qty), 4), "price": round(px, 4), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                "strategy": pos["strategy"]}

    def _check_stops(self, now: datetime | None) -> list[dict]:
        events = []
        for pos in [dict(r) for r in self.db.query("SELECT * FROM paper_positions")]:
            if pos["stop"] is None and pos["target"] is None:
                continue
            bars = closed_bars(self.store.get(pos["symbol"], pos["interval"], 300, refresh_if_empty=False),
                               pos["interval"], now)
            bars = bars[bars.index > pd.Timestamp(pos["checked_ts"], unit="s", tz="UTC")]
            closed = None
            for ts, b in bars.iterrows():
                t = int(ts.timestamp())
                long = pos["qty"] > 0
                if pos["stop"] is not None and ((long and b.low <= pos["stop"]) or (not long and b.high >= pos["stop"])):
                    px = min(b.open, pos["stop"]) if long else max(b.open, pos["stop"])
                    closed = self._close(pos, px, t, "stop")
                elif pos["target"] is not None and ((long and b.high >= pos["target"]) or (not long and b.low <= pos["target"])):
                    px = max(b.open, pos["target"]) if long else min(b.open, pos["target"])
                    closed = self._close(pos, px, t, "take_profit")
                if closed:
                    events.append(closed)
                    break
            if not closed and len(bars):
                with self.db.tx() as c:
                    c.execute("UPDATE paper_positions SET checked_ts=? WHERE id=?", (int(bars.index[-1].timestamp()), pos["id"]))
        return events

    def _record_equity(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.db.tx() as c:
            c.execute("INSERT INTO paper_equity(day, equity) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET equity=excluded.equity",
                      (day, round(self.equity(), 2)))

    # ------------------------------------------------------------ reporting
    def summary(self) -> dict:
        positions = self.positions()
        equity = self.equity(positions)
        initial = float(self.db.get_kv("paper_initial", str(self.settings.paper_capital)))
        trades = [dict(r) for r in self.db.query("SELECT * FROM paper_trades ORDER BY id DESC LIMIT 200")]
        wins = [t for t in trades if t["pnl"] > 0]
        realized = sum(t["pnl"] for t in trades)
        curve = [dict(r) for r in self.db.query("SELECT day, equity FROM paper_equity ORDER BY day")]
        return {
            "initial": initial, "cash": round(self.cash, 2), "equity": round(equity, 2),
            "return_pct": round((equity / initial - 1) * 100, 2) if initial else 0,
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(sum(p["unrealized"] for p in positions), 2),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "trades_count": len(trades), "started": self.db.get_kv("paper_started"),
            "positions": positions, "trades": trades[:50],
            "orders": [dict(r) for r in self.db.query("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 50")],
            "equity_curve": curve,
        }
