"""Price / volume features for candidate tickers (Yahoo Finance via yfinance)."""
from __future__ import annotations

import logging
import math
import random
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


@dataclass
class Quote:
    ticker: str
    price: float | None = None
    change_1d: float | None = None     # percent
    change_5d: float | None = None     # percent
    volume_ratio: float | None = None  # last volume / 20d average volume
    above_ma20: bool | None = None
    spark: list[float] | None = None   # last ~30 closes for the dashboard sparkline

    def to_dict(self) -> dict:
        return asdict(self)


def _pct(a: float, b: float) -> float | None:
    if b in (0, None) or a is None or math.isnan(a) or math.isnan(b):
        return None
    return round((a / b - 1) * 100, 2)


def quotes_from_frame(tickers: list[str], df) -> dict[str, Quote]:
    """Build quotes from a yfinance multi-ticker daily OHLCV DataFrame."""
    out: dict[str, Quote] = {}
    for t in tickers:
        try:
            sub = df[t] if len(tickers) > 1 else df
            sub = sub.dropna(subset=["Close"])
            if len(sub) < 2:
                continue
            close = sub["Close"].astype(float).tolist()
            vol = sub["Volume"].astype(float).tolist()
            avg20 = sum(vol[-21:-1]) / max(1, len(vol[-21:-1]))
            ma20 = sum(close[-20:]) / len(close[-20:])
            out[t] = Quote(
                ticker=t, price=round(close[-1], 2),
                change_1d=_pct(close[-1], close[-2]),
                change_5d=_pct(close[-1], close[-6]) if len(close) >= 6 else None,
                volume_ratio=round(vol[-1] / avg20, 2) if avg20 else None,
                above_ma20=close[-1] > ma20,
                spark=[round(c, 2) for c in close[-30:]],
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("quote parse failed for %s: %s", t, exc)
    return out


def fetch_quotes(tickers: list[str]) -> dict[str, Quote]:
    if not tickers:
        return {}
    import yfinance as yf

    try:
        df = yf.download(tickers, period="3mo", interval="1d", group_by="ticker",
                         auto_adjust=True, progress=False, threads=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance download failed: %s", exc)
        return {}
    if df is None or df.empty:
        return {}
    return quotes_from_frame(tickers, df)


def demo_quotes(tickers: list[str], seed: int = 7) -> dict[str, Quote]:
    out = {}
    for t in tickers:
        rng = random.Random(f"{t}-{seed}")
        price = rng.uniform(10, 600)
        drift = rng.uniform(-0.01, 0.02)
        closes = [price]
        for _ in range(29):
            closes.append(closes[-1] * (1 + drift + rng.gauss(0, 0.025)))
        out[t] = Quote(
            ticker=t, price=round(closes[-1], 2), change_1d=_pct(closes[-1], closes[-2]),
            change_5d=_pct(closes[-1], closes[-6]), volume_ratio=round(rng.uniform(0.6, 4.0), 2),
            above_ma20=closes[-1] > sum(closes[-20:]) / 20, spark=[round(c, 2) for c in closes],
        )
    return out
