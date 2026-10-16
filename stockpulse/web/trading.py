"""Chart / strategy / signal API (the TradingView-style part of the app)."""
from __future__ import annotations

import math
import threading
from typing import Any, Callable

import pandas as pd
from fastapi import APIRouter, Body, HTTPException

from .. import indicators as ind
from ..market_data import INTERVALS, closed_bars
from ..strategy import StrategyConfig
from ..strategy.backtest import run_backtest
from ..strategy.dsl import FUNCTION_NAMES, SERIES_NAMES, DSLError
from ..strategy.presets import PRESETS
from ..strategy.signals import SignalEngine

MAX_BARS = 5000


def _line(s: pd.Series) -> list[dict]:
    return [{"time": int(t.timestamp()), "value": round(float(v), 4)}
            for t, v in s.items() if v is not None and not (isinstance(v, float) and math.isnan(v))]


def _num(parts: list[str], i: int, default: float) -> float:
    try:
        return float(parts[i])
    except (IndexError, ValueError):
        return default


def indicator_series(df: pd.DataFrame, spec: str) -> list[dict]:
    """'ema:20' -> [{id, name, pane, kind, data}]; pane 0 = price chart."""
    parts = spec.split(":")
    k = parts[0].lower()
    c = df["close"]
    if k in ("sma", "ema", "wma"):
        n = int(_num(parts, 1, 20))
        fn = {"sma": ind.sma, "ema": ind.ema, "wma": ind.wma}[k]
        return [{"id": spec, "name": f"{k.upper()} {n}", "pane": "price", "kind": "line", "data": _line(fn(c, n))}]
    if k == "bb":
        n, m = int(_num(parts, 1, 20)), _num(parts, 2, 2)
        b = ind.bollinger(c, n, m)
        return [{"id": f"{spec}:{col}", "name": f"BB {col}", "pane": "price", "kind": "line", "group": spec,
                 "data": _line(b[col])} for col in ("upper", "mid", "lower")]
    if k == "vwap":
        return [{"id": spec, "name": "VWAP", "pane": "price", "kind": "line", "data": _line(ind.vwap(df))}]
    if k == "supertrend":
        n, m = int(_num(parts, 1, 10)), _num(parts, 2, 3)
        st = ind.supertrend(df, n, m)
        up = st["line"].where(st["dir"] == 1)
        dn = st["line"].where(st["dir"] == -1)
        return [{"id": f"{spec}:up", "name": "SuperTrend ↑", "pane": "price", "kind": "line", "group": spec, "tone": "up", "data": _line(up)},
                {"id": f"{spec}:dn", "name": "SuperTrend ↓", "pane": "price", "kind": "line", "group": spec, "tone": "down", "data": _line(dn)}]
    if k == "donchian":
        n = int(_num(parts, 1, 20))
        return [{"id": f"{spec}:hi", "name": f"DC {n} 高", "pane": "price", "kind": "line", "group": spec, "data": _line(ind.highest(df["high"], n))},
                {"id": f"{spec}:lo", "name": f"DC {n} 低", "pane": "price", "kind": "line", "group": spec, "data": _line(ind.lowest(df["low"], n))}]
    if k == "rsi":
        n = int(_num(parts, 1, 14))
        return [{"id": spec, "name": f"RSI {n}", "pane": "rsi", "kind": "line", "levels": [30, 70], "data": _line(ind.rsi(c, n))}]
    if k == "macd":
        f, s_, g = int(_num(parts, 1, 12)), int(_num(parts, 2, 26)), int(_num(parts, 3, 9))
        m = ind.macd(c, f, s_, g)
        return [{"id": f"{spec}:hist", "name": "MACD Hist", "pane": "macd", "kind": "histogram", "data": _line(m["hist"])},
                {"id": f"{spec}:macd", "name": "MACD", "pane": "macd", "kind": "line", "data": _line(m["macd"])},
                {"id": f"{spec}:signal", "name": "Signal", "pane": "macd", "kind": "line", "data": _line(m["signal"])}]
    if k == "stoch":
        n, d = int(_num(parts, 1, 14)), int(_num(parts, 2, 3))
        st = ind.stoch(df, n, d)
        return [{"id": f"{spec}:k", "name": "%K", "pane": "stoch", "kind": "line", "levels": [20, 80], "data": _line(st["k"])},
                {"id": f"{spec}:d", "name": "%D", "pane": "stoch", "kind": "line", "data": _line(st["d"])}]
    if k == "atr":
        n = int(_num(parts, 1, 14))
        return [{"id": spec, "name": f"ATR {n}", "pane": "atr", "kind": "line", "data": _line(ind.atr(df, n))}]
    raise HTTPException(400, f"未知指标 {spec!r}")


def build_router(engine, require_token: Callable[..., None]) -> APIRouter:
    r = APIRouter(prefix="/api")
    db, settings = engine.db, engine.settings
    signals: SignalEngine = engine.signals
    store = signals.store

    def check_interval(interval: str) -> None:
        if interval not in INTERVALS:
            raise HTTPException(400, f"interval 必须是 {', '.join(INTERVALS)} 之一")

    def load(symbol: str, interval: str, limit: int, refresh: bool = True) -> pd.DataFrame:
        check_interval(interval)
        if refresh:
            store.maybe_update(symbol, interval)
        df = store.get(symbol, interval, min(max(limit, 50), MAX_BARS))
        if df.empty:
            raise HTTPException(404, f"没有 {symbol} 的 {interval} 数据（代码错误或数据源不可用）")
        return df

    # ---------------------------------------------------------------- chart
    @r.get("/chart")
    def chart(symbol: str, interval: str = "1d", limit: int = 600, indicators: str = ""):
        symbol = symbol.upper().strip()
        df = load(symbol, interval, limit)
        series: list[dict] = []
        for spec in [x.strip() for x in indicators.split(",") if x.strip()][:12]:
            series += indicator_series(df, spec)
        bars = [{"time": int(t.timestamp()), "open": round(row.open, 4), "high": round(row.high, 4),
                 "low": round(row.low, 4), "close": round(row.close, 4), "volume": float(row.volume)}
                for t, row in df.iterrows()]
        closed = closed_bars(df, interval)
        last, prev = df["close"].iloc[-1], df["close"].iloc[-2] if len(df) > 1 else df["close"].iloc[-1]
        return {
            "symbol": symbol, "interval": interval, "provider": store.provider, "bars": bars, "series": series,
            "last": {"price": round(float(last), 4), "change_pct": round((last / prev - 1) * 100, 2),
                     "time": bars[-1]["time"], "forming": len(closed) < len(df)},
            "signals": [s for s in db.list_signals(200, symbol) if s["interval"] == interval],
        }

    # ---------------------------------------------------------------- watchlist
    filling = threading.Lock()

    def fill_missing(symbols: list[str]) -> None:
        if not filling.acquire(blocking=False):
            return
        try:
            for sym in symbols:
                store.update(sym, "1d")
        finally:
            filling.release()

    @r.get("/watchlist")
    def watchlist():
        out, missing = [], []
        for sym in db.get_watchlist(settings.watchlist):
            df = store.get(sym, "1d", 2, refresh_if_empty=False)
            if len(df) >= 1:
                last = float(df["close"].iloc[-1])
                prev = float(df["close"].iloc[-2]) if len(df) > 1 else last
                out.append({"symbol": sym, "price": round(last, 4), "change_pct": round((last / prev - 1) * 100, 2)})
            else:
                missing.append(sym)
                out.append({"symbol": sym, "price": None, "change_pct": None})
        if missing:  # first sight of a symbol: fetch its history in the background
            threading.Thread(target=fill_missing, args=(missing,), daemon=True).start()
        return out

    @r.put("/watchlist")
    def set_watchlist(symbols: list[str] = Body(...), _=require_token):
        clean = []
        for s in symbols:
            s = s.strip().upper()
            if s and s not in clean and len(s) <= 15:
                clean.append(s)
        db.set_watchlist(clean[:100])
        return {"symbols": clean}

    @r.post("/watchlist/refresh")
    def refresh_watchlist(_=require_token):
        n = sum(store.update(s, "1d") > 0 for s in db.get_watchlist(settings.watchlist))
        return {"updated": n}

    # ---------------------------------------------------------------- strategies
    @r.get("/strategy/meta")
    def strategy_meta():
        return {"presets": PRESETS, "functions": FUNCTION_NAMES, "series": SERIES_NAMES,
                "intervals": list(INTERVALS)}

    @r.get("/strategies")
    def list_strategies():
        return db.list_strategies()

    def parse_cfg(body: dict[str, Any]) -> StrategyConfig:
        try:
            cfg = StrategyConfig.from_dict(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"策略格式错误：{exc}") from exc
        errors = cfg.validate()
        if errors:
            raise HTTPException(400, {"errors": errors})
        return cfg

    @r.post("/strategies/validate")
    def validate_strategy(body: dict[str, Any] = Body(...)):
        try:
            cfg = StrategyConfig.from_dict(body)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "errors": [str(exc)]}
        errors = cfg.validate()
        return {"ok": not errors, "errors": errors}

    @r.post("/strategies")
    def save_strategy(body: dict[str, Any] = Body(...), _=require_token):
        cfg = parse_cfg(body)
        if cfg.enabled and not cfg.symbols:
            raise HTTPException(400, {"errors": ["启用实时信号前请至少添加一个股票代码"]})
        cfg.id = db.save_strategy(cfg.to_dict())
        return db.get_strategy(cfg.id)

    @r.delete("/strategies/{sid}")
    def delete_strategy(sid: int, _=require_token):
        if not db.delete_strategy(sid):
            raise HTTPException(404, "策略不存在")
        return {"deleted": sid}

    @r.post("/backtest")
    def backtest(body: dict[str, Any] = Body(...)):
        """Body: {strategy: {...config} | strategy_id, symbol, interval?, limit?}"""
        if body.get("strategy_id"):
            raw = db.get_strategy(int(body["strategy_id"]))
            if not raw:
                raise HTTPException(404, "策略不存在")
        else:
            raw = body.get("strategy") or {}
        if body.get("interval"):
            raw = {**raw, "interval": body["interval"]}
        cfg = parse_cfg(raw)
        symbol = (body.get("symbol") or (cfg.symbols[0] if cfg.symbols else "")).upper()
        if not symbol:
            raise HTTPException(400, "请指定回测代码")
        df = closed_bars(load(symbol, cfg.interval, int(body.get("limit") or 2000)), cfg.interval)
        try:
            res = run_backtest(df, cfg, settings.commission_bps, settings.slippage_bps)
        except DSLError as exc:
            raise HTTPException(400, {"errors": [str(exc)]}) from exc
        return {"symbol": symbol, "interval": cfg.interval, "costs_bps": {"commission": settings.commission_bps,
                "slippage": settings.slippage_bps}, **res.to_dict()}

    # ---------------------------------------------------------------- signals
    @r.get("/signals")
    def list_signals(limit: int = 100, symbol: str | None = None):
        return db.list_signals(min(limit, 500), symbol.upper() if symbol else None)

    @r.get("/positions")
    def positions():
        return signals.positions()

    @r.post("/signals/run")
    def run_signals(_=require_token):
        return {"new": engine.run_signals()}

    return r
