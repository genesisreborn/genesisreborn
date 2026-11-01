"""Option chains (Yahoo Finance via yfinance): calls/puts by strike, put/call ratios, max pain, ATM IV."""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Settings
from .db import Database

CACHE_MINUTES = 15
FIELDS = {"strike": "strike", "lastPrice": "last", "bid": "bid", "ask": "ask", "change": "change",
          "percentChange": "change_pct", "volume": "volume", "openInterest": "oi",
          "impliedVolatility": "iv", "inTheMoney": "itm"}


def _rows(df: pd.DataFrame | None) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        row = {}
        for src, dst in FIELDS.items():
            v = r.get(src)
            if dst == "itm":
                row[dst] = bool(v)
                continue
            try:
                f = float(v)
                row[dst] = None if math.isnan(f) else f
            except (TypeError, ValueError):
                row[dst] = None
        out.append(row)
    return sorted(out, key=lambda x: x["strike"] or 0)


def summarize(calls: list[dict], puts: list[dict], spot: float | None) -> dict:
    s = lambda rows, k: sum(r[k] or 0 for r in rows)  # noqa: E731
    cv, pv, coi, poi = s(calls, "volume"), s(puts, "volume"), s(calls, "oi"), s(puts, "oi")
    strikes = sorted({r["strike"] for r in calls + puts if r["strike"] is not None})
    max_pain = None
    if strikes and (coi or poi):
        def pain(k: float) -> float:
            return (sum(max(0.0, k - r["strike"]) * (r["oi"] or 0) for r in calls)
                    + sum(max(0.0, r["strike"] - k) * (r["oi"] or 0) for r in puts))
        max_pain = min(strikes, key=pain)
    atm_iv = None
    if spot and strikes:
        atm = min(strikes, key=lambda k: abs(k - spot))
        ivs = [r["iv"] for r in calls + puts if r["strike"] == atm and r["iv"]]
        atm_iv = sum(ivs) / len(ivs) if ivs else None
    return {"call_volume": cv, "put_volume": pv, "call_oi": coi, "put_oi": poi,
            "pc_volume": round(pv / cv, 3) if cv else None, "pc_oi": round(poi / coi, 3) if coi else None,
            "max_pain": max_pain, "atm_iv": atm_iv}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def demo_chain(symbol: str, expiry: str | None) -> dict:
    from .market_data import demo_bars

    spot = float(demo_bars(symbol.upper(), "1d")["close"].iloc[-1])
    today = datetime.now(timezone.utc).date()
    fridays = [today + timedelta(days=(4 - today.weekday()) % 7 + 7 * i) for i in range(8)]
    exps = [d.isoformat() for d in fridays] + [(today + timedelta(days=d)).isoformat() for d in (90, 180, 365)]
    expiry = expiry if expiry in exps else exps[0]
    t = max((datetime.fromisoformat(expiry).date() - today).days, 1) / 365
    rng = random.Random(f"opt-{symbol}-{expiry}")
    step = 10 ** math.floor(math.log10(spot)) / 4 if spot > 20 else 0.5
    calls, puts = [], []
    for i in range(-15, 16):
        k = round(round(spot / step) * step + i * step, 2)
        if k <= 0:
            continue
        iv = 0.35 + 0.25 * abs(math.log(k / spot)) + rng.uniform(-0.02, 0.02)
        d1 = (math.log(spot / k) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
        d2 = d1 - iv * math.sqrt(t)
        c = spot * _norm_cdf(d1) - k * _norm_cdf(d2)
        p = c - spot + k
        for rows, price, itm in ((calls, c, k < spot), (puts, p, k > spot)):
            price = max(price, 0.01)
            near = math.exp(-abs(math.log(k / spot)) * 8)
            rows.append({"strike": k, "last": round(price, 2), "bid": round(price * 0.98, 2), "ask": round(price * 1.02, 2),
                         "change": round(price * rng.uniform(-0.2, 0.2), 2), "change_pct": round(rng.uniform(-20, 20), 2),
                         "volume": float(int(rng.uniform(50, 5000) * near)), "oi": float(int(rng.uniform(500, 30000) * near)),
                         "iv": round(iv, 4), "itm": itm})
    return {"symbol": symbol.upper(), "spot": spot, "expirations": exps, "expiry": expiry, "calls": calls, "puts": puts,
            "summary": summarize(calls, puts, spot), "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def fetch_chain(symbol: str, expiry: str | None) -> dict:
    import yfinance as yf

    t = yf.Ticker(symbol)
    exps = list(t.options or [])
    if not exps:
        raise LookupError(f"{symbol} 没有期权")
    expiry = expiry if expiry in exps else exps[0]
    ch = t.option_chain(expiry)
    spot = None
    try:
        spot = float((ch.underlying or {}).get("regularMarketPrice"))
    except (TypeError, ValueError, AttributeError):
        pass
    calls, puts = _rows(ch.calls), _rows(ch.puts)
    return {"symbol": symbol.upper(), "spot": spot, "expirations": exps, "expiry": expiry, "calls": calls, "puts": puts,
            "summary": summarize(calls, puts, spot), "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def get_chain(db: Database, settings: Settings, symbol: str, expiry: str | None = None) -> dict:
    symbol = symbol.upper().strip()
    key = f"opt:{symbol}:{expiry or ''}"
    raw = db.get_kv(key)
    if raw:
        cached = json.loads(raw)
        if time.time() - cached.pop("_ts", 0) < CACHE_MINUTES * 60:
            return cached
    data = demo_chain(symbol, expiry) if settings.demo_mode else fetch_chain(symbol, expiry)
    db.set_kv(key, json.dumps({**data, "_ts": time.time()}))
    return data
