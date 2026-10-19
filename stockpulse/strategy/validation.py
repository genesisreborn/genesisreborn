"""Robustness report: several strategies x a basket of symbols, full period and out-of-sample.

For every (strategy, symbol) the full history is backtested once; metrics are reported for
the whole period and for the last ``oos_pct`` of it (the part you would not have tuned on).
"""
from __future__ import annotations

import statistics
from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from ..market_data import BarStore, closed_bars
from . import StrategyConfig
from .backtest import run_backtest
from .optimize import window_metrics
from .presets import preset
from .refs import load_refs

DEFAULT_STRATEGIES = ["donchian_breakout", "donchian_market_filter", "momentum_pullback", "rsi2_reversion"]
DEFAULT_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "JPM", "XOM", "LLY", "COST", "HD"]


def _bh(df: pd.DataFrame, start: pd.Timestamp) -> float:
    pos = max(int(df.index.searchsorted(start)) - 1, 0)   # same base as window_metrics: the close before start
    sub = df["close"].iloc[pos:]
    return round((sub.iloc[-1] / sub.iloc[0] - 1) * 100, 2) if len(sub) > 1 else 0.0


def validate_strategies(store: BarStore, strategies: list[StrategyConfig], symbols: list[str], oos_pct: float = 30,
                        commission_bps: float = 5, slippage_bps: float = 5, history: int = 5000,
                        update: bool = True) -> dict:
    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        if update:
            store.maybe_update(sym, strategies[0].interval, min_gap_s=300)
        df = closed_bars(store.get(sym, strategies[0].interval, history, refresh_if_empty=update), strategies[0].interval)
        if len(df) >= 250:
            data[sym] = df
    results = []
    for cfg in strategies:
        # Compare signal quality on equal terms: every trade uses the full account (no risk sizing).
        cfg = replace(cfg, risk_pct=0, position_pct=100)
        refs = load_refs(store, cfg, history, update)
        rows = []
        for sym, df in data.items():
            res = run_backtest(df, cfg, commission_bps, slippage_bps, refs=refs)
            cut = df.index[int(len(df) * (1 - oos_pct / 100))]
            oos = window_metrics(res.equity, res.trades, cut, df.index[-1], cfg.interval)
            m = res.metrics
            rows.append({"symbol": sym, "total_return_pct": m["total_return_pct"], "buy_hold_pct": m["buy_hold_pct"],
                         "max_drawdown_pct": m["max_drawdown_pct"], "sharpe": m["sharpe"], "trades": m["trades"],
                         "win_rate_pct": m["win_rate_pct"], "exposure_pct": m["exposure_pct"],
                         "oos_return_pct": oos["total_return_pct"], "oos_buy_hold_pct": _bh(df, cut),
                         "oos_max_drawdown_pct": oos["max_drawdown_pct"], "oos_trades": oos["trades"]})
        if not rows:
            continue
        med = lambda k: round(statistics.median(r[k] for r in rows), 2)  # noqa: E731
        bh_dd = []
        for sym, df in data.items():
            eq = df["close"]
            bh_dd.append(float(((eq / eq.cummax()) - 1).min() * 100))
        results.append({
            "key": cfg.preset or cfg.name, "name": cfg.name, "rows": rows,
            "summary": {
                "median_return_pct": med("total_return_pct"), "median_buy_hold_pct": med("buy_hold_pct"),
                "median_max_drawdown_pct": med("max_drawdown_pct"),
                "median_buy_hold_drawdown_pct": round(statistics.median(bh_dd), 2),
                "median_sharpe": med("sharpe"), "median_exposure_pct": med("exposure_pct"),
                "profitable_pct": round(sum(r["total_return_pct"] > 0 for r in rows) / len(rows) * 100, 1),
                "beat_buy_hold_pct": round(sum(r["total_return_pct"] > r["buy_hold_pct"] for r in rows) / len(rows) * 100, 1),
                "lower_drawdown_pct": round(sum(r["max_drawdown_pct"] > d for r, d in zip(rows, bh_dd)) / len(rows) * 100, 1),
                "oos_profitable_pct": round(sum(r["oos_return_pct"] > 0 for r in rows) / len(rows) * 100, 1),
                "oos_median_return_pct": med("oos_return_pct"), "oos_median_buy_hold_pct": med("oos_buy_hold_pct"),
                "total_trades": sum(r["trades"] for r in rows),
            },
        })
    first = min((df.index[0] for df in data.values()), default=None)
    last = max((df.index[-1] for df in data.values()), default=None)
    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "symbols": list(data), "skipped": [s for s in symbols if s not in data],
            "period": [first.strftime("%Y-%m-%d") if first is not None else None,
                       last.strftime("%Y-%m-%d") if last is not None else None],
            "oos_pct": oos_pct, "costs_bps": [commission_bps, slippage_bps], "strategies": results}


def default_strategies() -> list[StrategyConfig]:
    return [preset(k) for k in DEFAULT_STRATEGIES]


def render_validation_markdown(r: dict) -> str:
    p = lambda v: "—" if v is None else f"{v:+.1f}%"  # noqa: E731
    out = ["# 策略稳健性验证报告", "",
           f"区间：{r['period'][0]} → {r['period'][1]} · 代码 {len(r['symbols'])} 个（{', '.join(r['symbols'])}）"
           f" · 样本外 = 最后 {r['oos_pct']:.0f}% · 成本 {r['costs_bps'][0]}+{r['costs_bps'][1]} bp", ""]
    if r["skipped"]:
        out += [f"数据不足被跳过：{', '.join(r['skipped'])}", ""]
    out += ["## 汇总（各代码的中位数）", "",
            "| 策略 | 收益 | 买入持有 | 最大回撤 | 买入持有回撤 | 夏普 | 持仓时间 | 盈利代码 | 跑赢持有 | 回撤更小 | 样本外收益 | 样本外持有 | 样本外盈利代码 | 交易数 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in r["strategies"]:
        m = s["summary"]
        out.append(f"| {s['name']} | {p(m['median_return_pct'])} | {p(m['median_buy_hold_pct'])} | {m['median_max_drawdown_pct']:.1f}% "
                   f"| {m['median_buy_hold_drawdown_pct']:.1f}% | {m['median_sharpe']:.2f} | {m['median_exposure_pct']:.0f}% "
                   f"| {m['profitable_pct']:.0f}% | {m['beat_buy_hold_pct']:.0f}% | {m['lower_drawdown_pct']:.0f}% "
                   f"| {p(m['oos_median_return_pct'])} | {p(m['oos_median_buy_hold_pct'])} | {m['oos_profitable_pct']:.0f}% | {m['total_trades']} |")
    for s in r["strategies"]:
        out += ["", f"## {s['name']}", "",
                "| 代码 | 收益 | 买入持有 | 最大回撤 | 夏普 | 交易 | 胜率 | 样本外收益 | 样本外持有 |", "|---|---|---|---|---|---|---|---|---|"]
        for row in s["rows"]:
            wr = "—" if row["win_rate_pct"] is None else f"{row['win_rate_pct']:.0f}%"
            out.append(f"| {row['symbol']} | {p(row['total_return_pct'])} | {p(row['buy_hold_pct'])} | {row['max_drawdown_pct']:.1f}% "
                       f"| {row['sharpe']:.2f} | {row['trades']} | {wr} | {p(row['oos_return_pct'])} | {p(row['oos_buy_hold_pct'])} |")
    out += ["", "---", "",
            "_为公平比较信号质量，这里每笔交易都按全仓计算（忽略策略里的单笔风险设置）；实际按风险定仓位时收益和回撤会同比例缩小。"
            "回测不代表未来收益，未考虑分红和税费。_", ""]
    return "\n".join(out)
