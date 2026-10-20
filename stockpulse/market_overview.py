"""Market overview: major indices, sector ETF heatmap, breadth and movers of your universe."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .market_data import BarStore
from .strategy.screener import screen_row

INDICES = [("SPY", "标普 500"), ("QQQ", "纳指 100"), ("DIA", "道琼斯"), ("IWM", "罗素 2000"),
           ("^VIX", "VIX 恐慌指数"), ("TLT", "20 年美债"), ("GLD", "黄金"), ("BTC-USD", "比特币")]
SECTORS = [("XLK", "科技"), ("XLC", "通信"), ("XLY", "可选消费"), ("XLF", "金融"), ("XLV", "医疗"),
           ("XLI", "工业"), ("XLP", "必需消费"), ("XLE", "能源"), ("XLB", "材料"), ("XLU", "公用事业"),
           ("XLRE", "房地产")]
PERIODS = {"1d": "change_pct", "1w": "perf_1w", "1m": "perf_1m", "3m": "perf_3m", "ytd": "perf_ytd", "1y": "perf_1y"}


def _rows(store: BarStore, symbols: list[str], names: dict[str, str], update: bool) -> list[dict]:
    if update:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda s: store.maybe_update(s, "1d", min_gap_s=120), symbols))
    out = []
    for sym in symbols:
        df = store.get(sym, "1d", 400, refresh_if_empty=False)   # includes today's forming bar
        if len(df) < 2:
            continue
        out.append({**screen_row(sym, df), "name": names.get(sym, "")})
    return out


def breadth(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"count": 0}
    pct = lambda cond: round(sum(1 for r in rows if cond(r)) / n * 100, 1)  # noqa: E731
    return {
        "count": n,
        "advancers": sum(1 for r in rows if (r.get("change_pct") or 0) > 0),
        "decliners": sum(1 for r in rows if (r.get("change_pct") or 0) < 0),
        "above_sma50_pct": pct(lambda r: (r.get("vs_sma50") or -1) > 0),
        "above_sma200_pct": pct(lambda r: (r.get("vs_sma200") or -1) > 0),
        "near_high_pct": pct(lambda r: r.get("from_high_52w") is not None and r["from_high_52w"] >= -2),
        "near_low_pct": pct(lambda r: r.get("from_low_52w") is not None and r["from_low_52w"] <= 2),
        "rsi_overbought": sum(1 for r in rows if (r.get("rsi14") or 0) >= 70),
        "rsi_oversold": sum(1 for r in rows if r.get("rsi14") is not None and r["rsi14"] <= 30),
    }


def market_overview(store: BarStore, universe: list[str], holdings: list[str] | None = None, update: bool = True) -> dict:
    universe = list(dict.fromkeys(s.upper() for s in universe if s))[:120]
    names = dict(INDICES + SECTORS)
    every = list(dict.fromkeys([s for s, _ in INDICES] + [s for s, _ in SECTORS] + universe + (holdings or [])))
    rows = {r["symbol"]: r for r in _rows(store, every, names, update)}
    uni = [rows[s] for s in universe if s in rows]
    by_change = sorted((r for r in uni if r.get("change_pct") is not None), key=lambda r: r["change_pct"])
    return {
        "indices": [rows[s] for s, _ in INDICES if s in rows],
        "sectors": [rows[s] for s, _ in SECTORS if s in rows],
        "universe": uni,
        "holdings": [rows[s] for s in (holdings or []) if s in rows],
        "breadth": breadth(uni),
        "gainers": [r for r in reversed(by_change) if r["change_pct"] > 0][:8],
        "losers": [r for r in by_change if r["change_pct"] < 0][:8],
        "periods": PERIODS,
    }
