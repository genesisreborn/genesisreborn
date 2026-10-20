"""Real-holdings portfolio tracker: you record buys / sells / dividends from any broker,
it keeps positions at average cost, realized and unrealized P&L, allocation, a daily value
history and a "same cash flows into SPY" benchmark. Notes on each transaction double as a
trading journal.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from .db import Database, iso, utcnow
from .market_data import BarStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,              -- trade time (unix seconds)
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,               -- BUY | SELL | DIV (dividend: price = total cash received)
    qty REAL NOT NULL DEFAULT 0,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    note TEXT, tags TEXT,
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS portfolio_tx_symbol ON portfolio_tx(symbol, ts);
"""

SIDES = ("BUY", "SELL", "DIV")
BENCHMARK = "SPY"
EPS = 1e-9


class PortfolioError(ValueError):
    pass


def replay(txs: list[dict]) -> dict:
    """Average-cost bookkeeping over transactions sorted by time.

    Returns {symbol: {qty, cost, realized, dividends, fees, first_ts}} and raises
    PortfolioError if a sell exceeds the shares held at that moment.
    """
    book: dict[str, dict] = {}
    for t in sorted(txs, key=lambda x: (x["ts"], x["id"] or 0)):
        b = book.setdefault(t["symbol"], {"qty": 0.0, "cost": 0.0, "realized": 0.0, "dividends": 0.0,
                                          "fees": 0.0, "first_ts": t["ts"]})
        fee = float(t.get("fee") or 0)
        b["fees"] += fee
        if t["side"] == "BUY":
            b["qty"] += t["qty"]
            b["cost"] += t["qty"] * t["price"] + fee
        elif t["side"] == "SELL":
            if t["qty"] > b["qty"] + EPS:
                day = datetime.fromtimestamp(t["ts"], timezone.utc).strftime("%Y-%m-%d")
                raise PortfolioError(f"{t['symbol']} 在 {day} 卖出 {t['qty']:g} 股，但当时只持有 {b['qty']:g} 股")
            avg = b["cost"] / b["qty"] if b["qty"] > EPS else 0.0
            b["realized"] += t["qty"] * (t["price"] - avg) - fee
            b["cost"] -= avg * t["qty"]
            b["qty"] -= t["qty"]
            if b["qty"] < EPS:
                b["qty"], b["cost"] = 0.0, 0.0
        else:  # DIV
            b["dividends"] += t["price"] - fee
    return book


class Portfolio:
    def __init__(self, db: Database, store: BarStore):
        self.db = db
        self.store = store
        with db.tx() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------ transactions
    def transactions(self, symbol: str | None = None) -> list[dict]:
        if symbol:
            rows = self.db.query("SELECT * FROM portfolio_tx WHERE symbol=? ORDER BY ts DESC, id DESC", (symbol.upper(),))
        else:
            rows = self.db.query("SELECT * FROM portfolio_tx ORDER BY ts DESC, id DESC")
        return [dict(r) for r in rows]

    def add(self, symbol: str, side: str, qty: float, price: float, ts: int | None = None, fee: float = 0,
            note: str = "", tags: str = "") -> dict:
        symbol = (symbol or "").strip().upper()
        side = (side or "").strip().upper()
        qty, price, fee = float(qty or 0), float(price), float(fee or 0)
        ts = int(ts or datetime.now(timezone.utc).timestamp())
        if not symbol or len(symbol) > 20:
            raise PortfolioError("代码无效")
        if side not in SIDES:
            raise PortfolioError("类型必须是 BUY / SELL / DIV")
        if not all(math.isfinite(v) for v in (qty, price, fee)) or price < 0 or fee < 0:
            raise PortfolioError("数量、价格、手续费必须是非负数")
        if side in ("BUY", "SELL") and (qty <= 0 or price <= 0):
            raise PortfolioError("买卖的数量和价格必须大于 0")
        if side == "DIV" and price <= 0:
            raise PortfolioError("分红金额必须大于 0")
        if not 0 < ts < 4_102_444_800:   # before 2100
            raise PortfolioError("时间无效")
        tx = {"id": None, "ts": ts, "symbol": symbol, "side": side, "qty": qty if side != "DIV" else 0.0,
              "price": price, "fee": fee, "note": (note or "")[:2000], "tags": (tags or "")[:200]}
        replay(self.transactions(symbol) + [tx])   # validates sells against holdings at that time
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO portfolio_tx(ts, symbol, side, qty, price, fee, note, tags, created)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts, symbol, side, tx["qty"], price, fee, tx["note"], tx["tags"], iso(utcnow())))
            tx["id"] = cur.lastrowid
        return tx

    def update_note(self, tx_id: int, note: str, tags: str = "") -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE portfolio_tx SET note=?, tags=? WHERE id=?",
                             ((note or "")[:2000], (tags or "")[:200], tx_id)).rowcount > 0

    def delete(self, tx_id: int) -> bool:
        row = self.db.query("SELECT symbol FROM portfolio_tx WHERE id=?", (tx_id,))
        if not row:
            return False
        rest = [t for t in self.transactions(row[0]["symbol"]) if t["id"] != tx_id]
        replay(rest)   # deleting a buy must not leave a later sell uncovered
        with self.db.tx() as c:
            return c.execute("DELETE FROM portfolio_tx WHERE id=?", (tx_id,)).rowcount > 0

    # ------------------------------------------------------------ valuation
    def _bars(self, symbol: str, update: bool, limit: int = 1500) -> pd.DataFrame:
        if update:
            self.store.maybe_update(symbol, "1d", min_gap_s=300)
        return self.store.get(symbol, "1d", limit, refresh_if_empty=update)

    def summary(self, update: bool = True, history: bool = True) -> dict:
        txs = self.transactions()
        book = replay(txs)
        rows, total_mv, total_cost, day_chg = [], 0.0, 0.0, 0.0
        bars: dict[str, pd.DataFrame] = {}
        for sym, b in book.items():
            if b["qty"] <= EPS:
                continue
            df = self._bars(sym, update)
            bars[sym] = df
            last = float(df["close"].iloc[-1]) if len(df) else None
            prev = float(df["close"].iloc[-2]) if len(df) > 1 else last
            mv = b["qty"] * last if last is not None else None
            row = {"symbol": sym, "qty": round(b["qty"], 6), "avg_cost": b["cost"] / b["qty"], "cost": b["cost"],
                   "last": last, "market_value": mv,
                   "unrealized": mv - b["cost"] if mv is not None else None,
                   "unrealized_pct": (mv / b["cost"] - 1) * 100 if mv is not None and b["cost"] > 0 else None,
                   "day_change": b["qty"] * (last - prev) if last is not None else None,
                   "day_change_pct": (last / prev - 1) * 100 if last and prev else None,
                   "realized": b["realized"], "dividends": b["dividends"]}
            rows.append(row)
            total_cost += b["cost"]
            total_mv += mv or 0
            day_chg += row["day_change"] or 0
        for r in rows:
            r["weight_pct"] = (r["market_value"] / total_mv * 100) if total_mv and r["market_value"] else 0.0
        rows.sort(key=lambda r: -(r["market_value"] or 0))
        realized = sum(b["realized"] for b in book.values())
        dividends = sum(b["dividends"] for b in book.values())
        closed = [{"symbol": s, "realized": b["realized"], "dividends": b["dividends"]}
                  for s, b in book.items() if b["qty"] <= EPS and (b["realized"] or b["dividends"])]
        out = {
            "positions": rows, "closed": closed,
            "totals": {"market_value": total_mv, "cost": total_cost, "unrealized": total_mv - total_cost,
                       "unrealized_pct": (total_mv / total_cost - 1) * 100 if total_cost else None,
                       "realized": realized, "dividends": dividends,
                       "total_pnl": total_mv - total_cost + realized + dividends,
                       "day_change": day_chg,
                       "day_change_pct": day_chg / (total_mv - day_chg) * 100 if total_mv - day_chg else None,
                       "fees": sum(b["fees"] for b in book.values())},
            "transactions": txs[:500],
        }
        if history and txs:
            out["history"] = self._history(txs, bars, update)
        return out

    def _history(self, txs: list[dict], bars: dict[str, pd.DataFrame], update: bool) -> dict:
        """Daily market value and cost basis since the first trade, plus the same cash flows put into SPY."""
        start = min(t["ts"] for t in txs)
        days = max(30, int((datetime.now(timezone.utc).timestamp() - start) / 86400 * 0.75) + 30)
        limit = min(days, 5000)
        syms = {t["symbol"] for t in txs}
        closes = {}
        for sym in syms | {BENCHMARK}:
            df = bars.get(sym)
            if df is None or len(df) < limit:
                df = self._bars(sym, update, limit)
            if len(df):
                closes[sym] = df["close"]
        if BENCHMARK not in closes:
            return {"points": []}
        axis = closes[BENCHMARK].index
        axis = axis[axis >= pd.Timestamp(start, unit="s", tz="UTC").normalize()]
        if not len(axis):
            return {"points": []}
        aligned = {s: c.reindex(axis, method="ffill") for s, c in closes.items()}
        ordered = sorted(txs, key=lambda x: (x["ts"], x["id"]))
        qty: dict[str, float] = {}
        cost: dict[str, float] = {}
        spy_units, i, points = 0.0, 0, []
        for day in axis:
            day_end = int(day.timestamp()) + 86400
            while i < len(ordered) and ordered[i]["ts"] < day_end:
                t = ordered[i]
                s = t["symbol"]
                spy_px = float(aligned[BENCHMARK].loc[day])
                if t["side"] == "BUY":
                    amount = t["qty"] * t["price"] + (t["fee"] or 0)
                    qty[s] = qty.get(s, 0) + t["qty"]
                    cost[s] = cost.get(s, 0) + amount
                    spy_units += amount / spy_px
                elif t["side"] == "SELL":
                    avg = cost.get(s, 0) / qty[s] if qty.get(s) else 0
                    proceeds = t["qty"] * t["price"] - (t["fee"] or 0)
                    qty[s] = qty.get(s, 0) - t["qty"]
                    cost[s] = cost.get(s, 0) - avg * t["qty"]
                    spy_units -= proceeds / spy_px
                i += 1
            value = sum(q * float(aligned[s].loc[day]) for s, q in qty.items()
                        if q > EPS and s in aligned and not math.isnan(aligned[s].loc[day]))
            points.append({"time": int(day.timestamp()), "value": round(value, 2),
                           "cost": round(sum(c for s, c in cost.items() if qty.get(s, 0) > EPS), 2),
                           "benchmark": round(spy_units * float(aligned[BENCHMARK].loc[day]), 2)})
        return {"points": points, "benchmark": BENCHMARK}
