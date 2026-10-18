"""Technical indicators on pandas Series/DataFrames (no look-ahead: each value uses data up to that bar)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int = 20) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).mean()


def ema(s: pd.Series, n: int = 20) -> pd.Series:
    return s.ewm(span=int(n), adjust=False, min_periods=int(n)).mean()


def wma(s: pd.Series, n: int = 20) -> pd.Series:
    n = int(n)
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    n = int(n)
    delta = s.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(down != 0, 100.0).where(up.notna())


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=int(signal), adjust=False, min_periods=int(signal)).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(s, n)
    sd = s.rolling(int(n), min_periods=int(n)).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / int(n), adjust=False, min_periods=int(n)).mean()


def highest(s: pd.Series, n: int = 20) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).max()


def lowest(s: pd.Series, n: int = 20) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).min()


def stoch(df: pd.DataFrame, n: int = 14, d: int = 3) -> pd.DataFrame:
    lo, hi = lowest(df["low"], n), highest(df["high"], n)
    k = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return pd.DataFrame({"k": k, "d": k.rolling(int(d), min_periods=int(d)).mean()})


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP for intraday bars (resets each calendar day); cumulative for daily+."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    idx = pd.DatetimeIndex(df.index)
    if len(idx) > 1 and (idx[1:] - idx[:-1]).min() < pd.Timedelta(days=1):
        day = idx.normalize()
        return pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv.cumsum() / df["volume"].cumsum().replace(0, np.nan)


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Returns columns: line (the trailing stop) and dir (+1 uptrend, -1 downtrend)."""
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    upper = (hl2 + mult * a).to_numpy()
    lower = (hl2 - mult * a).to_numpy()
    close = df["close"].to_numpy()
    m = len(df)
    fu, fl = upper.copy(), lower.copy()
    direction = np.ones(m)
    line = np.full(m, np.nan)
    for i in range(1, m):
        if np.isnan(upper[i]) or np.isnan(fu[i - 1]):
            continue
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if close[i] > fu[i - 1]:
            direction[i] = 1
        elif close[i] < fl[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    return pd.DataFrame({"line": line, "dir": np.where(np.isnan(line), np.nan, direction)}, index=df.index)


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["close"].diff()).fillna(0)
    return (sign * df["volume"]).cumsum()


def crossover(a: pd.Series, b) -> pd.Series:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return ((a > b) & (a.shift(1) <= b.shift(1))).fillna(False)


def crossunder(a: pd.Series, b) -> pd.Series:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return ((a < b) & (a.shift(1) >= b.shift(1))).fillna(False)


# ---------------------------------------------------------------- more indicators
def dmi(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Wilder's DMI: +DI, -DI and ADX."""
    n = int(n)
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr.replace(0, np.nan)
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"plus_di": pdi, "minus_di": mdi, "adx": dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()})


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return dmi(df, n)["adx"]


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    n = int(n)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(n, min_periods=n).mean()
    md = tp.rolling(n, min_periods=n).apply(lambda x: float(np.mean(np.abs(x - x.mean()))), raw=True)
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def willr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hi, lo = highest(df["high"], n), lowest(df["low"], n)
    return -100 * (hi - df["close"]) / (hi - lo).replace(0, np.nan)


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    n = int(n)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    flow = tp * df["volume"]
    pos = flow.where(tp > tp.shift(1), 0.0).rolling(n, min_periods=n).sum()
    neg = flow.where(tp < tp.shift(1), 0.0).rolling(n, min_periods=n).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))


def roc(s: pd.Series, n: int = 12) -> pd.Series:
    return (s / s.shift(int(n)) - 1) * 100


def stoch_rsi(s: pd.Series, n: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    r = rsi(s, n)
    lo, hi = r.rolling(int(n), min_periods=int(n)).min(), r.rolling(int(n), min_periods=int(n)).max()
    raw = (100 * (r - lo) / (hi - lo).replace(0, np.nan)).clip(0, 100)  # clip float noise
    kk = raw.rolling(int(k), min_periods=int(k)).mean()
    return pd.DataFrame({"k": kk, "d": kk.rolling(int(d), min_periods=int(d)).mean()})


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = ema(df["close"], n)
    a = atr(df, n)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * a, "lower": mid - mult * a})


def ichimoku(df: pd.DataFrame, conv: int = 9, base: int = 26, span_b: int = 52) -> pd.DataFrame:
    """Tenkan, Kijun and the two cloud spans (spans are NOT shifted forward here: value at t uses data up to t)."""
    mid = lambda n: (highest(df["high"], n) + lowest(df["low"], n)) / 2  # noqa: E731
    tenkan, kijun = mid(conv), mid(base)
    return pd.DataFrame({"tenkan": tenkan, "kijun": kijun, "span_a": (tenkan + kijun) / 2, "span_b": mid(span_b)})


def psar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.Series:
    high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    n = len(df)
    out = np.full(n, np.nan)
    if n < 2:
        return pd.Series(out, index=df.index)
    up = True
    af = step
    ep = high[0]
    sar = low[0]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < sar:
                up, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_step)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > sar:
                up, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_step)
        out[i] = sar
    return pd.Series(out, index=df.index)
