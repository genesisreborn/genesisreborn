"""Company profile, key statistics, financial statements and corporate events (Yahoo Finance via yfinance).

Results are cached in the kv table for ``CACHE_HOURS`` because fundamentals change slowly and Yahoo
rate-limits aggressively.
"""
from __future__ import annotations

import json
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

CACHE_HOURS = 12

# (key, label, yfinance info field, format) - format: money / pct / ratio / num / date / text
STATS = [
    ("market_cap", "市值", "marketCap", "money"),
    ("enterprise_value", "企业价值", "enterpriseValue", "money"),
    ("pe", "市盈率 TTM", "trailingPE", "ratio"),
    ("forward_pe", "预期市盈率", "forwardPE", "ratio"),
    ("peg", "PEG", "trailingPegRatio", "ratio"),
    ("ps", "市销率", "priceToSalesTrailing12Months", "ratio"),
    ("pb", "市净率", "priceToBook", "ratio"),
    ("ev_ebitda", "EV/EBITDA", "enterpriseToEbitda", "ratio"),
    ("eps", "每股收益 TTM", "trailingEps", "num"),
    ("forward_eps", "预期每股收益", "forwardEps", "num"),
    ("revenue", "营收 TTM", "totalRevenue", "money"),
    ("revenue_growth", "营收增速 (同比)", "revenueGrowth", "pct"),
    ("earnings_growth", "盈利增速 (同比)", "earningsGrowth", "pct"),
    ("gross_margin", "毛利率", "grossMargins", "pct"),
    ("operating_margin", "营业利润率", "operatingMargins", "pct"),
    ("profit_margin", "净利率", "profitMargins", "pct"),
    ("roe", "ROE", "returnOnEquity", "pct"),
    ("roa", "ROA", "returnOnAssets", "pct"),
    ("debt_to_equity", "负债/权益 %", "debtToEquity", "ratio"),
    ("current_ratio", "流动比率", "currentRatio", "ratio"),
    ("free_cash_flow", "自由现金流", "freeCashflow", "money"),
    ("dividend_yield", "股息率", "dividendYield", "pct"),
    ("payout_ratio", "派息比率", "payoutRatio", "pct"),
    ("beta", "Beta", "beta", "ratio"),
    ("high_52w", "52 周最高", "fiftyTwoWeekHigh", "num"),
    ("low_52w", "52 周最低", "fiftyTwoWeekLow", "num"),
    ("avg_volume", "平均成交量 (3 月)", "averageVolume", "num"),
    ("shares", "总股本", "sharesOutstanding", "num"),
    ("float_shares", "流通股", "floatShares", "num"),
    ("short_pct_float", "做空占流通股", "shortPercentOfFloat", "pct"),
    ("short_ratio", "空头回补天数", "shortRatio", "ratio"),
    ("insider_pct", "内部人持股", "heldPercentInsiders", "pct"),
    ("institution_pct", "机构持股", "heldPercentInstitutions", "pct"),
    ("target_mean", "分析师目标价 (均值)", "targetMeanPrice", "num"),
    ("target_high", "目标价最高", "targetHighPrice", "num"),
    ("target_low", "目标价最低", "targetLowPrice", "num"),
    ("analysts", "覆盖分析师数", "numberOfAnalystOpinions", "num"),
    ("recommendation", "评级", "recommendationKey", "text"),
]

# statement row -> (key, label); first match wins, Yahoo renames rows occasionally
STATEMENT_ROWS = [
    (("Total Revenue", "Operating Revenue"), "revenue", "营收"),
    (("Gross Profit",), "gross_profit", "毛利"),
    (("Operating Income", "Total Operating Income As Reported"), "operating_income", "营业利润"),
    (("Net Income", "Net Income Common Stockholders"), "net_income", "净利润"),
    (("Diluted EPS", "Basic EPS"), "eps", "每股收益"),
]

BALANCE_ROWS = [
    (("Total Assets",), "total_assets", "总资产"),
    (("Total Liabilities Net Minority Interest", "Total Liabilities"), "total_liabilities", "总负债"),
    (("Stockholders Equity", "Common Stock Equity"), "equity", "股东权益"),
    (("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"), "cash", "现金及短期投资"),
    (("Total Debt",), "total_debt", "总债务"),
    (("Net Debt",), "net_debt", "净债务"),
    (("Working Capital",), "working_capital", "营运资本"),
]

CASHFLOW_ROWS = [
    (("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"), "operating_cf", "经营现金流"),
    (("Capital Expenditure",), "capex", "资本开支"),
    (("Free Cash Flow",), "free_cf", "自由现金流"),
    (("Repurchase Of Capital Stock",), "buyback", "股票回购"),
    (("Cash Dividends Paid", "Common Stock Dividend Paid"), "dividends_paid", "分红支出"),
]

RECOMMENDATIONS = {"strong_buy": "强烈买入", "buy": "买入", "hold": "持有", "underperform": "跑输大盘",
                   "sell": "卖出", "none": "—"}


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return v


def _statement(df: pd.DataFrame | None, periods: int, rows=STATEMENT_ROWS) -> dict:
    """yfinance statement (rows = items, columns = period ends, newest first) -> {periods, rows}."""
    if df is None or df.empty:
        return {"periods": [], "rows": []}
    cols = sorted(df.columns, key=lambda c: pd.Timestamp(c))[-periods:]
    out_rows = []
    for names, key, label in rows:
        name = next((n for n in names if n in df.index), None)
        if name is None:
            continue
        vals = [_clean(df.at[name, c]) for c in cols]
        out_rows.append({"key": key, "label": label, "values": vals})
    by_key = {r["key"]: r["values"] for r in out_rows}
    rev = by_key.get("revenue")
    if rev and by_key.get("net_income"):
        out_rows.append({"key": "net_margin", "label": "净利率 %", "values": [
            round(n / r * 100, 2) if r and n is not None else None for r, n in zip(rev, by_key["net_income"])]})
    return {"periods": [pd.Timestamp(c).strftime("%Y-%m-%d") for c in cols], "rows": out_rows}


def _events(t: Any) -> list[dict]:
    """Earnings, dividends and splits as chart markers ({time: unix seconds, type, text})."""
    ev: list[dict] = []
    try:
        ed = t.get_earnings_dates(limit=16)
        if ed is not None:
            for ts, r in ed.iterrows():
                est, act = _clean(r.get("EPS Estimate")), _clean(r.get("Reported EPS"))
                txt = f"财报 预期 {est if est is not None else '—'} / 实际 {act if act is not None else '—'}"
                sur = _clean(r.get("Surprise(%)"))
                if sur is not None:
                    txt += f" ({sur:+.1f}%)"
                ev.append({"time": int(pd.Timestamp(ts).timestamp()), "type": "earnings", "text": txt,
                           "estimate": est, "actual": act, "surprise_pct": sur, "upcoming": act is None})
    except Exception as exc:  # noqa: BLE001 - optional data
        log.info("earnings dates failed: %s", exc)
    try:
        for ts, v in t.dividends.tail(40).items():
            ev.append({"time": int(pd.Timestamp(ts).timestamp()), "type": "dividend", "text": f"分红 {float(v):.4g}"})
    except Exception as exc:  # noqa: BLE001
        log.info("dividends failed: %s", exc)
    try:
        for ts, v in t.splits.tail(10).items():
            if v:
                ev.append({"time": int(pd.Timestamp(ts).timestamp()), "type": "split", "text": f"拆股 {float(v):g}:1"})
    except Exception as exc:  # noqa: BLE001
        log.info("splits failed: %s", exc)
    return sorted(ev, key=lambda e: e["time"])


def build_payload(symbol: str, info: dict, quarterly: pd.DataFrame | None, annual: pd.DataFrame | None,
                  events: list[dict], extra: dict[str, pd.DataFrame | None] | None = None) -> dict:
    """``extra``: balance_q / balance_a / cashflow_q / cashflow_a statements (yfinance layout)."""
    extra = extra or {}
    stats = []
    for key, label, field, fmt in STATS:
        v = _clean(info.get(field))
        if v is None:
            continue
        if key == "recommendation":
            v = RECOMMENDATIONS.get(str(v), str(v))
        if key == "dividend_yield" and isinstance(v, (int, float)) and v > 1:
            v = v / 100  # newer Yahoo responses already give percent
        stats.append({"key": key, "label": label, "value": v, "format": fmt})
    price = _clean(info.get("currentPrice") or info.get("regularMarketPrice"))
    target = _clean(info.get("targetMeanPrice"))
    if price and target:
        stats.append({"key": "target_upside", "label": "目标价空间", "value": target / price - 1, "format": "pct"})
    return {
        "symbol": symbol.upper(),
        "profile": {
            "name": info.get("longName") or info.get("shortName") or symbol.upper(),
            "sector": info.get("sector"), "industry": info.get("industry"),
            "country": info.get("country"), "exchange": info.get("exchange"),
            "currency": info.get("financialCurrency") or info.get("currency"),
            "employees": _clean(info.get("fullTimeEmployees")), "website": info.get("website"),
            "summary": info.get("longBusinessSummary"), "quote_type": info.get("quoteType"),
        },
        "stats": stats,
        "quarterly": _statement(quarterly, 8),
        "annual": _statement(annual, 5),
        "balance": {"quarterly": _statement(extra.get("balance_q"), 8, BALANCE_ROWS),
                    "annual": _statement(extra.get("balance_a"), 5, BALANCE_ROWS)},
        "cashflow": {"quarterly": _statement(extra.get("cashflow_q"), 8, CASHFLOW_ROWS),
                     "annual": _statement(extra.get("cashflow_a"), 5, CASHFLOW_ROWS)},
        "events": events,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def fetch_yahoo(symbol: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(symbol)
    info = t.info or {}
    if not info or (len(info) <= 2 and "trailingPegRatio" in info):
        raise LookupError(f"no fundamentals for {symbol}")
    def stmt(attr: str) -> pd.DataFrame | None:
        try:
            return getattr(t, attr)
        except Exception:  # noqa: BLE001 - optional data
            return None

    extra = {"balance_q": stmt("quarterly_balance_sheet"), "balance_a": stmt("balance_sheet"),
             "cashflow_q": stmt("quarterly_cashflow"), "cashflow_a": stmt("cashflow")}
    return build_payload(symbol, info, stmt("quarterly_income_stmt"), stmt("income_stmt"), _events(t), extra)


def demo_fundamentals(symbol: str) -> dict:
    """Deterministic fake data for offline mode and tests."""
    rng = random.Random(f"fund-{symbol.upper()}")
    rev0 = rng.uniform(2e9, 40e9)
    growth = rng.uniform(-0.05, 0.12)
    margin = rng.uniform(0.05, 0.35)
    shares = rng.uniform(2e8, 8e9)
    now = datetime.now(timezone.utc)
    q_ends = [pd.Timestamp(now - timedelta(days=91 * i)).normalize() for i in range(8, 0, -1)]
    q_rev = [rev0 * (1 + growth) ** i * rng.uniform(0.95, 1.05) for i in range(8)]
    q_ni = [r * margin * rng.uniform(0.8, 1.2) for r in q_rev]
    q = pd.DataFrame({c: {"Total Revenue": r, "Gross Profit": r * (margin + 0.3), "Operating Income": r * (margin + 0.05),
                          "Net Income": n, "Diluted EPS": n / shares} for c, r, n in zip(q_ends, q_rev, q_ni)})
    a_ends = [pd.Timestamp(now - timedelta(days=365 * i)).normalize() for i in range(5, 0, -1)]
    a_rev = [rev0 * 4 * (1 + growth * 4) ** i for i in range(5)]
    a = pd.DataFrame({c: {"Total Revenue": r, "Gross Profit": r * (margin + 0.3), "Operating Income": r * (margin + 0.05),
                          "Net Income": r * margin, "Diluted EPS": r * margin / shares} for c, r in zip(a_ends, a_rev)})
    from .market_data import demo_bars

    bars = demo_bars(symbol.upper(), "1d").tail(252)   # keep price-based stats consistent with the demo chart
    price = float(bars["close"].iloc[-1])
    ttm_rev, ttm_ni = sum(q_rev[-4:]), sum(q_ni[-4:])
    info = {
        "longName": f"{symbol.upper()} Demo Corp", "sector": "Technology", "industry": "Semiconductors",
        "country": "United States", "exchange": "NMS", "currency": "USD", "fullTimeEmployees": rng.randint(1000, 200000),
        "longBusinessSummary": "演示数据：这是离线模式生成的示例公司资料，不代表真实公司。",
        "marketCap": price * shares, "trailingPE": price * shares / ttm_ni if ttm_ni > 0 else None,
        "forwardPE": price * shares / (ttm_ni * 1.15) if ttm_ni > 0 else None, "priceToSalesTrailing12Months": price * shares / ttm_rev,
        "priceToBook": rng.uniform(1, 20), "trailingEps": ttm_ni / shares, "forwardEps": ttm_ni * 1.15 / shares,
        "totalRevenue": ttm_rev, "revenueGrowth": (1 + growth) ** 4 - 1, "grossMargins": margin + 0.3,
        "operatingMargins": margin + 0.05, "profitMargins": margin, "returnOnEquity": rng.uniform(0.05, 0.5),
        "debtToEquity": rng.uniform(10, 150), "dividendYield": rng.choice([None, 0.005, 0.012, 0.025]),
        "beta": rng.uniform(0.6, 2.2), "fiftyTwoWeekHigh": float(bars["high"].max()), "fiftyTwoWeekLow": float(bars["low"].min()),
        "averageVolume": rng.randint(1_000_000, 50_000_000), "sharesOutstanding": shares, "floatShares": shares * 0.95,
        "shortPercentOfFloat": rng.uniform(0.005, 0.12), "heldPercentInstitutions": rng.uniform(0.4, 0.9),
        "currentPrice": price, "targetMeanPrice": price * rng.uniform(0.9, 1.3), "numberOfAnalystOpinions": rng.randint(5, 50),
        "recommendationKey": rng.choice(["buy", "strong_buy", "hold"]),
    }
    events = []
    for i, c in enumerate(q_ends):
        est = q_ni[i] / shares * rng.uniform(0.9, 1.05)
        act = q_ni[i] / shares
        events.append({"time": int((c + timedelta(days=25)).timestamp()), "type": "earnings",
                       "text": f"财报 预期 {est:.2f} / 实际 {act:.2f} ({(act / est - 1) * 100:+.1f}%)",
                       "estimate": round(est, 2), "actual": round(act, 2), "surprise_pct": round((act / est - 1) * 100, 1),
                       "upcoming": False})
    nxt = q_ends[-1] + timedelta(days=91 + 25)
    events.append({"time": int(nxt.timestamp()), "type": "earnings", "text": "财报 (预计)", "estimate": None,
                   "actual": None, "surprise_pct": None, "upcoming": True})
    if info["dividendYield"]:
        for c in q_ends:
            events.append({"time": int((c + timedelta(days=40)).timestamp()), "type": "dividend",
                           "text": f"分红 {price * info['dividendYield'] / 4:.3f}"})
    def balance(ends, revs):
        return pd.DataFrame({c: {"Total Assets": r * 3, "Total Liabilities Net Minority Interest": r * 1.4,
                                 "Stockholders Equity": r * 1.6, "Cash Cash Equivalents And Short Term Investments": r * 0.5,
                                 "Total Debt": r * 0.6, "Net Debt": r * 0.1, "Working Capital": r * 0.3}
                             for c, r in zip(ends, revs)})

    def cashflow(ends, revs):
        return pd.DataFrame({c: {"Operating Cash Flow": r * (margin + 0.08), "Capital Expenditure": -r * 0.06,
                                 "Free Cash Flow": r * (margin + 0.02), "Repurchase Of Capital Stock": -r * 0.04,
                                 "Cash Dividends Paid": -r * 0.02} for c, r in zip(ends, revs)})

    extra = {"balance_q": balance(q_ends, [r * 4 for r in q_rev]), "balance_a": balance(a_ends, a_rev),
             "cashflow_q": cashflow(q_ends, q_rev), "cashflow_a": cashflow(a_ends, a_rev)}
    return build_payload(symbol, info, q, a, sorted(events, key=lambda e: e["time"]), extra)


def get_fundamentals(db: Database, settings: Settings, symbol: str, refresh: bool = False) -> dict:
    symbol = symbol.upper().strip()
    key = f"fund:{symbol}"
    if not refresh:
        raw = db.get_kv(key)
        if raw:
            cached = json.loads(raw)
            if time.time() - cached.get("_ts", 0) < CACHE_HOURS * 3600:
                cached.pop("_ts", None)
                return cached
    data = demo_fundamentals(symbol) if settings.demo_mode else fetch_yahoo(symbol)
    db.set_kv(key, json.dumps({**data, "_ts": time.time()}, ensure_ascii=False, default=str))
    return data
