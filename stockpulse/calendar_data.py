"""Economic events and earnings calendar (Yahoo Finance via yfinance.Calendars)."""
from __future__ import annotations

import json
import math
import random
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from .config import Settings
from .db import Database

CACHE_HOURS = 3


def _f(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _ts(v) -> int | None:
    try:
        t = pd.Timestamp(v)
        if pd.isna(t):
            return None
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return int(t.timestamp())
    except (TypeError, ValueError):
        return None


def economic_rows(df: pd.DataFrame | None) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    for name, r in df.iterrows():
        out.append({"time": _ts(r.get("Event Time")), "event": str(name), "region": r.get("Region"),
                    "period": r.get("For") if isinstance(r.get("For"), str) else None,
                    "actual": _f(r.get("Actual")), "expected": _f(r.get("Expected")), "last": _f(r.get("Last")),
                    "revised": _f(r.get("Revised"))})
    return sorted([x for x in out if x["time"]], key=lambda x: x["time"])


def earnings_rows(df: pd.DataFrame | None) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    for sym, r in df.iterrows():
        out.append({"time": _ts(r.get("Event Start Date")), "symbol": str(sym), "company": r.get("Company"),
                    "market_cap": _f(r.get("Marketcap")), "timing": r.get("Timing") if isinstance(r.get("Timing"), str) else None,
                    "estimate": _f(r.get("EPS Estimate")), "actual": _f(r.get("Reported EPS")),
                    "surprise_pct": _f(r.get("Surprise(%)"))})
    return sorted([x for x in out if x["time"]], key=lambda x: (x["time"], -(x["market_cap"] or 0)))


def fetch_yahoo(start: date, end: date) -> dict:
    import yfinance as yf

    cal = yf.Calendars(start=start, end=end)
    errors: list[str] = []
    econ: list[dict] = []
    earn: list[dict] = []
    try:
        econ = economic_rows(cal.get_economic_events_calendar(limit=100))
    except Exception as exc:  # noqa: BLE001 - Yahoo fails in many ways
        errors.append(f"经济日历：{exc}")
    try:
        earn = earnings_rows(cal.get_earnings_calendar(market_cap=2e9, filter_most_active=False, limit=100))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"财报日历：{exc}")
    if errors and not econ and not earn:
        raise RuntimeError("；".join(errors))
    return {"economic": econ, "earnings": earn, "errors": errors}


DEMO_EVENTS = [("CPI 消费者物价指数 (同比)", "US", 3.1), ("非农就业人数", "US", 180), ("失业率", "US", 4.1),
               ("FOMC 利率决议", "US", 4.5), ("零售销售 (环比)", "US", 0.3), ("ISM 制造业 PMI", "US", 49.5),
               ("GDP (季调年化)", "US", 2.4), ("PCE 物价指数 (同比)", "US", 2.7), ("初请失业金人数", "US", 225),
               ("中国 CPI (同比)", "CN", 0.4), ("欧央行利率决议", "EU", 3.0), ("日本央行利率决议", "JP", 0.5)]


def demo_calendar(start: date, end: date, symbols: list[str]) -> dict:
    rng = random.Random(f"cal-{start}")
    days = max((end - start).days, 1)
    econ = []
    for name, region, base in DEMO_EVENTS:
        d = start + timedelta(days=rng.randrange(days))
        t = datetime(d.year, d.month, d.day, 12, 30, tzinfo=timezone.utc)
        past = t < datetime.now(timezone.utc)
        exp = round(base * rng.uniform(0.95, 1.05), 2)
        econ.append({"time": int(t.timestamp()), "event": name, "region": region, "period": None,
                     "actual": round(exp * rng.uniform(0.9, 1.1), 2) if past else None, "expected": exp,
                     "last": round(base * rng.uniform(0.95, 1.05), 2), "revised": None})
    earn = []
    for s in list(dict.fromkeys(symbols + ["JPM", "NFLX", "TSM", "ASML", "GOOGL", "META", "INTC", "IBM"]))[:20]:
        d = start + timedelta(days=rng.randrange(days))
        t = datetime(d.year, d.month, d.day, 20, 5, tzinfo=timezone.utc)
        est = round(rng.uniform(0.3, 4), 2)
        past = t < datetime.now(timezone.utc)
        act = round(est * rng.uniform(0.9, 1.15), 2) if past else None
        earn.append({"time": int(t.timestamp()), "symbol": s, "company": f"{s} (演示)", "market_cap": rng.uniform(5e9, 3e12),
                     "timing": rng.choice(["BMO", "AMC"]), "estimate": est, "actual": act,
                     "surprise_pct": round((act / est - 1) * 100, 1) if act else None})
    return {"economic": sorted(econ, key=lambda x: x["time"]),
            "earnings": sorted(earn, key=lambda x: (x["time"], -(x["market_cap"] or 0))), "errors": []}


def get_calendar(db: Database, settings: Settings, start: date, days: int = 7, symbols: list[str] | None = None) -> dict:
    days = max(1, min(int(days), 31))
    end = start + timedelta(days=days)
    key = f"cal:{start.isoformat()}:{days}"
    raw = db.get_kv(key)
    data = None
    if raw:
        cached = json.loads(raw)
        if time.time() - cached.pop("_ts", 0) < CACHE_HOURS * 3600:
            data = cached
    if data is None:
        data = demo_calendar(start, end, symbols or []) if settings.demo_mode else fetch_yahoo(start, end)
        db.set_kv(key, json.dumps({**data, "_ts": time.time()}, default=str))
    watch = {s.upper() for s in symbols or []}
    for e in data["earnings"]:
        e["watched"] = e["symbol"].upper() in watch
    return {**data, "start": start.isoformat(), "end": end.isoformat()}
