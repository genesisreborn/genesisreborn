"""Stock screener: a row of daily-bar metrics per symbol, optionally filtered by a rule expression."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .. import indicators as ind
from ..market_data import BarStore, closed_bars
from .dsl import Evaluator, referenced_symbols, validate
from .refs import load_symbols

COLUMNS = {
    "price": "价格", "change_pct": "涨跌%", "volume": "成交量", "rel_volume": "量比", "perf_1w": "1周%",
    "perf_1m": "1月%", "perf_3m": "3月%", "perf_6m": "6月%", "perf_ytd": "年初至今%", "perf_1y": "1年%",
    "rsi14": "RSI14", "adx14": "ADX14", "atr_pct": "ATR%", "vs_sma50": "距50日线%", "vs_sma200": "距200日线%",
    "from_high_52w": "距52周高%", "from_low_52w": "距52周低%",
}


def _f(v, d=2):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), d)


def _perf(c: pd.Series, n: int):
    return _f((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100) if len(c) > n else None


def screen_row(sym: str, df: pd.DataFrame) -> dict:
    c = df["close"]
    last = df.iloc[-1]
    year = df[df.index >= pd.Timestamp(df.index[-1].year, 1, 1, tz="UTC")]
    ytd_base = year["close"].iloc[0] if len(year) else None
    w52 = df.iloc[-252:]
    vol20 = df["volume"].iloc[-21:-1].mean()
    return {
        "symbol": sym, "price": _f(last.close, 4), "change_pct": _perf(c, 1), "volume": _f(last.volume, 0),
        "rel_volume": _f(last.volume / vol20) if vol20 else None,
        "perf_1w": _perf(c, 5), "perf_1m": _perf(c, 21), "perf_3m": _perf(c, 63), "perf_6m": _perf(c, 126),
        "perf_1y": _perf(c, 252), "perf_ytd": _f((last.close / ytd_base - 1) * 100) if ytd_base else None,
        "rsi14": _f(ind.rsi(c, 14).iloc[-1], 1), "adx14": _f(ind.adx(df, 14).iloc[-1], 1),
        "atr_pct": _f(ind.atr(df, 14).iloc[-1] / last.close * 100),
        "vs_sma50": _f((last.close / ind.sma(c, 50).iloc[-1] - 1) * 100) if len(c) >= 50 else None,
        "vs_sma200": _f((last.close / ind.sma(c, 200).iloc[-1] - 1) * 100) if len(c) >= 200 else None,
        "from_high_52w": _f((last.close / w52["high"].max() - 1) * 100),
        "from_low_52w": _f((last.close / w52["low"].min() - 1) * 100),
    }


def run_screener(store: BarStore, symbols: list[str], expr: str = "", fetch: bool = True) -> dict:
    if expr:
        validate(expr)
    if fetch:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda s: store.maybe_update(s, "1d", min_gap_s=300), symbols))
    refs = load_symbols(store, referenced_symbols(expr), "1d", 450, fetch) if expr else {}
    rows, missing = [], []
    for sym in symbols:
        df = closed_bars(store.get(sym, "1d", 400, refresh_if_empty=False), "1d")
        if len(df) < 30:
            missing.append(sym)
            continue
        row = screen_row(sym, df)
        if expr:
            row["match"] = bool(Evaluator(df, refs=refs).eval(expr).iloc[-1])
        rows.append(row)
    matched = [r for r in rows if r.get("match", True)]
    return {"columns": COLUMNS, "rows": matched, "total": len(rows), "missing": missing, "filter": expr}
