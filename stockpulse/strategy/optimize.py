"""Parameter search with out-of-sample validation.

Two modes:
- split:        optimise on the first ``train_pct`` of the history, report the best
                parameter sets on the untouched remainder.
- walk_forward: roll a train window forward in ``folds`` steps; in each fold pick the
                best parameters on its train window and trade them on the following
                test window. The stitched test windows are the honest result.

Indicators are always computed on the full history up to each bar, so a test window
is warmed up exactly as it would be live; metrics are measured on the window only.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import replace

import numpy as np
import pandas as pd

from . import StrategyConfig
from .backtest import BARS_PER_YEAR, run_backtest

MAX_COMBOS = 300
OBJECTIVES = {"sharpe": "夏普比率", "total_return": "总收益", "calmar": "收益/回撤", "profit_factor": "盈亏比"}


class OptimizeError(ValueError):
    pass


def parse_grid(spec: dict) -> dict[str, list[float]]:
    """{"fast": "5:30:5", "slow": [20, 50, 100]} -> {"fast": [5,10,...,30], "slow": [20,50,100]}"""
    grid: dict[str, list[float]] = {}
    for name, v in (spec or {}).items():
        if isinstance(v, (list, tuple)):
            vals = [float(x) for x in v]
        elif isinstance(v, str) and ":" in v:
            parts = [float(x) for x in v.split(":")]
            if len(parts) != 3 or parts[2] <= 0 or parts[1] < parts[0]:
                raise OptimizeError(f"{name}: 范围写法是 起点:终点:步长，例如 5:30:5")
            n = int(math.floor((parts[1] - parts[0]) / parts[2] + 1e-9)) + 1
            if n > 200:
                raise OptimizeError(f"{name}: 取值太多（{n} 个），请加大步长")
            vals = [round(parts[0] + i * parts[2], 10) for i in range(n)]
        elif isinstance(v, str) and "," in v:
            vals = [float(x) for x in v.split(",") if x.strip()]
        else:
            vals = [float(v)]
        if not vals:
            raise OptimizeError(f"{name}: 没有取值")
        grid[name] = sorted(set(vals))
    if not grid:
        raise OptimizeError("请至少给一个参数设置取值范围")
    total = math.prod(len(v) for v in grid.values())
    if total > MAX_COMBOS:
        raise OptimizeError(f"参数组合共 {total} 种，超过上限 {MAX_COMBOS}，请缩小范围或加大步长")
    return grid


def window_metrics(equity: pd.Series, trades: list, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> dict:
    """Metrics of the equity curve over [start, end]; the base is the close before ``start`` so
    adjacent windows chain without losing the boundary bar."""
    lo = max(int(equity.index.searchsorted(start)) - 1, 0)
    hi = int(equity.index.searchsorted(end, side="right"))
    eq = equity.iloc[lo:hi]
    if len(eq) < 2:
        return {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "trades": 0, "profit_factor": None,
                "calmar": 0.0}
    rets = eq.pct_change().dropna()
    bpy = BARS_PER_YEAR.get(interval, 252)
    total = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    dd = float(((eq / eq.cummax()) - 1).min() * 100)
    sharpe = float(rets.mean() / rets.std() * math.sqrt(bpy)) if len(rets) > 1 and rets.std() > 0 else 0.0
    closed = [t for t in trades if t.exit_reason != "open" and start <= t.exit_time <= end]
    wins = sum(t.pnl_pct for t in closed if t.pnl_pct > 0)
    losses = -sum(t.pnl_pct for t in closed if t.pnl_pct <= 0)
    years = max((len(eq) - 1) / bpy, 1e-9)
    ann = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1) * 100 if eq.iloc[-1] > 0 else -100.0
    return {
        "total_return_pct": round(float(total), 2), "sharpe": round(sharpe, 2), "max_drawdown_pct": round(dd, 2),
        "trades": len(closed), "profit_factor": round(wins / losses, 2) if losses > 0 else None,
        "calmar": round(ann / abs(dd), 2) if dd < 0 else round(ann, 2),
    }


def score(m: dict, objective: str, min_trades: int) -> float:
    if m["trades"] < min_trades:
        return -1e9 + m["trades"]  # too few trades to mean anything
    if objective == "total_return":
        return m["total_return_pct"]
    if objective == "calmar":
        return m["calmar"]
    if objective == "profit_factor":
        return m["profit_factor"] if m["profit_factor"] is not None else (1e6 if m["total_return_pct"] > 0 else 0)
    return m["sharpe"]


def _combos(grid: dict[str, list[float]]) -> list[dict[str, float]]:
    names = list(grid)
    return [dict(zip(names, vals)) for vals in itertools.product(*(grid[n] for n in names))]


def _run_all(df: pd.DataFrame, cfg: StrategyConfig, combos: list[dict], costs: tuple[float, float], refs=None):
    """One full-history backtest per combo; windows are measured from the same run."""
    out = []
    for params in combos:
        c = replace(cfg, params={**cfg.params, **params})
        res = run_backtest(df, c, *costs, refs=refs)
        out.append((params, res))
    return out


def optimize(df: pd.DataFrame, cfg: StrategyConfig, grid_spec: dict, mode: str = "split", objective: str = "sharpe",
             train_pct: float = 70, folds: int = 4, min_trades: int = 3,
             commission_bps: float = 5, slippage_bps: float = 5, refs: dict | None = None) -> dict:
    if objective not in OBJECTIVES:
        raise OptimizeError(f"优化目标必须是 {', '.join(OBJECTIVES)} 之一")
    if len(df) < 150:
        raise OptimizeError("数据太少（至少需要 150 根 K 线）")
    grid = parse_grid(grid_spec)
    for name in grid:
        if name not in cfg.params:
            raise OptimizeError(f"参数 {name!r} 不在策略参数里（策略参数：{', '.join(cfg.params) or '无'}）")
    combos = _combos(grid)
    runs = _run_all(df, cfg, combos, (commission_bps, slippage_bps), refs)
    idx = df.index
    iv = cfg.interval

    if mode == "split":
        cut = int(len(df) * min(max(train_pct, 30), 90) / 100)
        tr = (idx[0], idx[cut - 1])
        te = (idx[cut], idx[-1])
        rows = []
        for params, res in runs:
            ins = window_metrics(res.equity, res.trades, *tr, iv)
            oos = window_metrics(res.equity, res.trades, *te, iv)
            rows.append({"params": params, "train": ins, "test": oos, "score": score(ins, objective, min_trades)})
        rows.sort(key=lambda r: r["score"], reverse=True)
        best = rows[0]
        # How much of the in-sample edge survives out of sample, across the top 5 sets
        top = rows[: min(5, len(rows))]
        result = {
            "mode": "split", "objective": objective, "combos": len(combos),
            "train_range": [int(tr[0].timestamp()), int(tr[1].timestamp())],
            "test_range": [int(te[0].timestamp()), int(te[1].timestamp())],
            "best_params": best["params"], "best": {"train": best["train"], "test": best["test"]},
            "top_test_mean_return_pct": round(float(np.mean([r["test"]["total_return_pct"] for r in top])), 2),
            "rows": [{**r, "score": round(r["score"], 4) if r["score"] > -1e8 else None} for r in rows[:50]],
        }
        heat_end = cut - 1
    elif mode == "walk_forward":
        folds = int(min(max(folds, 2), 8))
        n = len(df)
        # first 40% is the initial train window; the rest is split into equal test windows
        first_train = int(n * 0.4)
        test_len = (n - first_train) // folds
        if test_len < 20:
            raise OptimizeError("数据不够做这么多折，请减少折数或加载更多 K 线")
        fold_rows = []
        oos_returns = []
        chosen = []
        for k in range(folds):
            tr_end = first_train + k * test_len
            tr_start = max(0, tr_end - first_train)  # rolling train window of fixed length
            te_end = n - 1 if k == folds - 1 else tr_end + test_len - 1
            tr = (idx[tr_start], idx[tr_end - 1])
            te = (idx[tr_end], idx[te_end])
            scored = []
            for params, res in runs:
                ins = window_metrics(res.equity, res.trades, *tr, iv)
                scored.append((score(ins, objective, min_trades), params, res, ins))
            scored.sort(key=lambda x: x[0], reverse=True)
            _, params, res, ins = scored[0]
            oos = window_metrics(res.equity, res.trades, *te, iv)
            chosen.append(tuple(sorted(params.items())))
            oos_returns.append(oos["total_return_pct"])
            fold_rows.append({"fold": k + 1, "train_range": [int(tr[0].timestamp()), int(tr[1].timestamp())],
                              "test_range": [int(te[0].timestamp()), int(te[1].timestamp())],
                              "params": params, "train": ins, "test": oos})
        compounded = (np.prod([1 + r / 100 for r in oos_returns]) - 1) * 100
        most = max(set(chosen), key=chosen.count)
        bh = (df["close"].iloc[-1] / df["close"].iloc[first_train - 1] - 1) * 100   # from the close before the first test bar
        result = {
            "mode": "walk_forward", "objective": objective, "combos": len(combos), "folds": fold_rows,
            "oos_total_return_pct": round(float(compounded), 2),
            "oos_buy_hold_pct": round(float(bh), 2),
            "oos_positive_folds": sum(r > 0 for r in oos_returns),
            "param_stability_pct": round(chosen.count(most) / len(chosen) * 100, 1),
            "best_params": dict(most),
        }
    else:
        raise OptimizeError("mode 必须是 split 或 walk_forward")

    # 2-parameter heatmap of the in-sample objective (split) / full-period objective (walk-forward)
    if len(grid) == 2:
        a, b = list(grid)
        rng = (idx[0], idx[heat_end]) if mode == "split" else (idx[0], idx[-1])
        cells = []
        for params, res in runs:
            m = window_metrics(res.equity, res.trades, *rng, iv)
            s = score(m, objective, min_trades)
            cells.append({"x": params[a], "y": params[b], "value": round(s, 3) if s > -1e8 else None})
        result["heatmap"] = {"x": a, "y": b, "xs": grid[a], "ys": grid[b], "cells": cells}

    result["warnings"] = _warnings(result)
    return result


def _warnings(r: dict) -> list[str]:
    w = []
    if r["mode"] == "split":
        tr, te = r["best"]["train"], r["best"]["test"]
        if tr["sharpe"] > 0.5 and te["sharpe"] < tr["sharpe"] * 0.3:
            w.append("最优参数在样本外的夏普比样本内低很多，可能是过拟合。")
        if te["total_return_pct"] < 0:
            w.append("最优参数在样本外亏损。")
        if tr["trades"] < 5:
            w.append("样本内交易次数很少，结果的统计意义有限。")
    else:
        if r["param_stability_pct"] < 50:
            w.append("每一折选出的最优参数都不一样，参数不稳定。")
        if r["oos_total_return_pct"] < r["oos_buy_hold_pct"]:
            w.append("走前验证的样本外收益没有跑赢同期买入持有。")
        if r["oos_positive_folds"] < len(r["folds"]) / 2:
            w.append("一半以上的样本外窗口亏损。")
    return w
