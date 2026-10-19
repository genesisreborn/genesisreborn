"""OHLCV bars: fetch (yfinance or Massive/Polygon), cache in SQLite, serve as DataFrames."""
from __future__ import annotations

import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

# Max age of the newest cached bar for which an incremental pull (shortest of the yfinance window
# below and Massive's 5 / 40 days) still reaches back far enough to leave no hole.
INCREMENTAL_SAFE = {"1m": timedelta(hours=20), "5m": timedelta(days=4), "15m": timedelta(days=4),
                    "30m": timedelta(days=4), "1h": timedelta(days=4), "1d": timedelta(days=25),
                    "1wk": timedelta(days=35)}

# interval -> (bar length, yfinance max lookback, yfinance incremental lookback)
INTERVALS: dict[str, tuple[timedelta, str, str]] = {
    "1m": (timedelta(minutes=1), "7d", "1d"),
    "5m": (timedelta(minutes=5), "60d", "5d"),
    "15m": (timedelta(minutes=15), "60d", "5d"),
    "30m": (timedelta(minutes=30), "60d", "5d"),
    "1h": (timedelta(hours=1), "730d", "10d"),
    "1d": (timedelta(days=1), "10y", "1mo"),
    "1wk": (timedelta(weeks=1), "max", "3mo"),
}
# Massive/Polygon aggregate (multiplier, timespan) and default history in days
MASSIVE_SPANS = {"1m": (1, "minute", 7), "5m": (5, "minute", 60), "15m": (15, "minute", 60),
                 "30m": (30, "minute", 60), "1h": (1, "hour", 730), "1d": (1, "day", 3650),
                 "1wk": (1, "week", 3650 * 2)}

COLUMNS = ["open", "high", "low", "close", "volume"]


def bar_length(interval: str) -> timedelta:
    if interval not in INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}; use one of {list(INTERVALS)}")
    return INTERVALS[interval][0]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="ts"), dtype=float)


# ---------------------------------------------------------------- providers
def fetch_yfinance(symbol: str, interval: str, incremental: bool) -> pd.DataFrame:
    import yfinance as yf

    _, full, inc = INTERVALS[interval]
    df = yf.Ticker(symbol).history(period=inc if incremental else full, interval=interval,
                                   auto_adjust=True, actions=False)
    if df is None or df.empty:
        return _empty()
    df = df.rename(columns=str.lower)[COLUMNS]
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
    df.index.name = "ts"
    return df.dropna(subset=["close"])


def fetch_massive(symbol: str, interval: str, incremental: bool, settings: Settings,
                  client: httpx.Client | None = None) -> pd.DataFrame:
    mult, span, days = MASSIVE_SPANS[interval]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=5 if incremental and span in ("minute", "hour") else 40 if incremental else days)
    url = f"{settings.massive_base_url}/v2/aggs/ticker/{symbol.upper()}/range/{mult}/{span}/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": settings.massive_api_key}
    rows: list[dict] = []
    own = client is None
    client = client or httpx.Client(timeout=30)
    next_url: httpx.URL | None = httpx.URL(url, params=params)
    try:
        for _ in range(200):  # hard cap on pages
            if next_url is None:
                break
            r = client.get(next_url)
            for wait in (2, 5, 13):   # rate limited (free tier: 5 calls/min): back off a few times
                if r.status_code != 429:
                    break
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(float(ra), 60) if ra.isdigit() else wait)
                r = client.get(next_url)
            r.raise_for_status()
            data = r.json()
            rows.extend(data.get("results") or [])
            # next_url keeps its own cursor query; only the key has to be re-added
            nxt = data.get("next_url")
            next_url = httpx.URL(nxt).copy_merge_params({"apiKey": settings.massive_api_key}) if nxt else None
    finally:
        if own:
            client.close()
    if not rows:
        return _empty()
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df.index.name = "ts"
    return df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[COLUMNS]


def demo_bars(symbol: str, interval: str, n: int = 600, end: datetime | None = None) -> pd.DataFrame:
    """Deterministic random-walk bars with regimes, for offline use and tests."""
    step = bar_length(interval)
    end = end or datetime.now(timezone.utc)
    # align to the bar grid so repeated calls produce identical timestamps
    epoch = int(end.timestamp()) // int(step.total_seconds()) * int(step.total_seconds())
    end = datetime.fromtimestamp(epoch, tz=timezone.utc)
    rng = random.Random(f"{symbol}-{interval}")
    price = rng.uniform(20, 400)
    rows, idx = [], []
    drift = 0.0
    for i in range(n):
        if i % 80 == 0:
            drift = rng.uniform(-0.004, 0.006)
        vol = 0.018 * math.sqrt(step / timedelta(days=1)) if step < timedelta(days=1) else 0.018
        o = price
        c = max(1.0, o * (1 + drift * (step / timedelta(days=1) if step < timedelta(days=1) else 1) + rng.gauss(0, vol)))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        rows.append((o, h, lo, c, float(rng.randint(500_000, 5_000_000))))
        idx.append(end - step * (n - 1 - i))
        price = c
    df = pd.DataFrame(rows, columns=COLUMNS, index=pd.DatetimeIndex(idx, name="ts"))
    return df


# ---------------------------------------------------------------- cache
class BarStore:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._last_pull: dict[tuple[str, str], float] = {}

    def maybe_update(self, symbol: str, interval: str, min_gap_s: float = 60) -> bool:
        """Pull new bars if the cache may be stale; throttled per symbol/interval."""
        key = (symbol.upper(), interval)
        now = time.monotonic()
        if now - self._last_pull.get(key, -1e9) < min_gap_s:
            return False
        last = self.last_ts(*key)
        if last is not None:
            age = datetime.now(timezone.utc).timestamp() - last
            if age < bar_length(interval).total_seconds() + 60 and interval not in ("1d", "1wk"):
                return False
        self._last_pull[key] = now
        self.update(*key)
        return True

    @property
    def provider(self) -> str:
        if self.settings.demo_mode:
            return "demo"
        return "massive" if self.settings.massive_api_key else "yfinance"

    def _fetch(self, symbol: str, interval: str, incremental: bool) -> pd.DataFrame:
        p = self.provider
        if p == "demo":
            return demo_bars(symbol, interval)
        if p == "massive":
            return fetch_massive(symbol, interval, incremental, self.settings)
        return fetch_yfinance(symbol, interval, incremental)

    def save(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        rows = [(symbol, interval, int(ts.timestamp()), float(r.open), float(r.high), float(r.low), float(r.close),
                 float(r.volume or 0)) for ts, r in df.iterrows()]
        with self.db.tx() as c:
            c.executemany(
                "INSERT INTO bars(symbol, interval, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(symbol, interval, ts) DO UPDATE SET open=excluded.open, high=excluded.high,"
                " low=excluded.low, close=excluded.close, volume=excluded.volume", rows)
        return len(rows)

    def last_ts(self, symbol: str, interval: str) -> int | None:
        r = self.db.query("SELECT MAX(ts) AS m FROM bars WHERE symbol=? AND interval=?", (symbol, interval))
        return r[0]["m"] if r and r[0]["m"] is not None else None

    def update(self, symbol: str, interval: str) -> int:
        """Fetch new bars (full history first time, recent window afterwards) and upsert."""
        symbol = symbol.upper()
        bar_length(interval)
        last = self.last_ts(symbol, interval)
        # incremental pulls only cover a short recent window; after a longer gap re-fetch everything
        incremental = last is not None and (
            datetime.now(timezone.utc).timestamp() - last < INCREMENTAL_SAFE[interval].total_seconds())
        self._last_pull[(symbol, interval)] = time.monotonic()
        try:
            df = self._fetch(symbol, interval, incremental)
        except Exception as exc:  # noqa: BLE001 - network providers fail in many ways
            log.warning("bars %s %s fetch failed: %s", symbol, interval, exc)
            return 0
        return self.save(symbol, interval, df)

    def get(self, symbol: str, interval: str, limit: int = 1000, refresh_if_empty: bool = True) -> pd.DataFrame:
        symbol = symbol.upper()
        rows = self.db.query(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? AND interval=?"
            " ORDER BY ts DESC LIMIT ?", (symbol, interval, int(limit)))
        if not rows and refresh_if_empty:
            self.update(symbol, interval)
            return self.get(symbol, interval, limit, refresh_if_empty=False)
        if not rows:
            return _empty()
        df = pd.DataFrame([dict(r) for r in reversed(rows)])
        df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True)
        df.index.name = "ts"
        return df.astype(float)


def closed_bars(df: pd.DataFrame, interval: str, now: datetime | None = None) -> pd.DataFrame:
    """Drop the still-forming last bar, so signals are only taken on completed bars."""
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    last = df.index[-1].to_pydatetime()
    if interval in ("1d", "1wk"):
        # US equities: the daily bar is final after the 16:00 New York close (+15 min buffer).
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        session_day = last.astimezone(ny).date()
        if interval == "1wk":
            # Friday of that week, whether the bar is stamped Monday (Yahoo) or Sunday (Massive/Polygon)
            session_day = session_day + timedelta(days=(4 - session_day.weekday()) % 7)
        close_at = datetime(session_day.year, session_day.month, session_day.day, 16, 15, tzinfo=ny)
        is_closed = now >= close_at
    else:
        is_closed = now >= last + bar_length(interval)
    return df if is_closed else df.iloc[:-1]
