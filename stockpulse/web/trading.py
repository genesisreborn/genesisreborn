"""Chart / strategy / signal API (the TradingView-style part of the app)."""
from __future__ import annotations

import functools
import json
import logging
import math
import threading
import re
import uuid
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd
from fastapi import APIRouter, Body, HTTPException

from .. import indicators as ind
from ..db import utcnow
from ..calendar_data import get_calendar
from ..fundamentals import get_fundamentals
from ..options import get_chain
from ..market_data import INTERVALS, closed_bars
from ..strategy import StrategyConfig
from ..strategy.backtest import run_backtest
from ..strategy.dsl import FUNCTION_NAMES, SERIES_NAMES, DSLError
from ..strategy.presets import PRESETS
from ..strategy.alerts import Alert
from ..strategy.optimize import OBJECTIVES, OptimizeError, optimize
from ..strategy.refs import load_refs
from ..strategy.screener import run_screener
from ..strategy.script import EXAMPLES as SCRIPT_EXAMPLES, run_script, script_refs, validate_script
from ..strategy.scanner import MAX_SYMBOLS, scan
from ..strategy.signals import SignalEngine

MAX_BARS = 5000
SYMBOL_PARAM_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,19}$")
log = logging.getLogger(__name__)


def bad_input_is_400(fn):
    """Routes taking a free-form JSON body: wrong value types become 400 instead of 500."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (TypeError, ValueError, AttributeError, KeyError) as exc:
            if isinstance(exc, DSLError):
                raise HTTPException(400, {"errors": [str(exc)]}) from exc
            log.info("bad request body for %s: %r", fn.__name__, exc)
            raise HTTPException(400, f"参数格式错误：{exc}") from exc
    return wrapper


def _line(s: pd.Series) -> list[dict]:
    return [{"time": int(t.timestamp()), "value": round(float(v), 4)}
            for t, v in s.items() if v is not None and math.isfinite(float(v))]


def _num(parts: list[str], i: int, default: float) -> float:
    try:
        v = float(parts[i])
    except (IndexError, ValueError):
        return default
    return v if math.isfinite(v) and abs(v) <= MAX_BARS * 10 else default


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
    if k in ("adx", "dmi"):
        n = int(_num(parts, 1, 14))
        d = ind.dmi(df, n)
        return [{"id": f"{spec}:adx", "name": f"ADX {n}", "pane": spec, "kind": "line", "levels": [20, 40], "data": _line(d["adx"])},
                {"id": f"{spec}:pdi", "name": "+DI", "pane": spec, "kind": "line", "tone": "up", "data": _line(d["plus_di"])},
                {"id": f"{spec}:mdi", "name": "-DI", "pane": spec, "kind": "line", "tone": "down", "data": _line(d["minus_di"])}]
    if k == "cci":
        n = int(_num(parts, 1, 20))
        return [{"id": spec, "name": f"CCI {n}", "pane": spec, "kind": "line", "levels": [-100, 100], "data": _line(ind.cci(df, n))}]
    if k == "willr":
        n = int(_num(parts, 1, 14))
        return [{"id": spec, "name": f"%R {n}", "pane": spec, "kind": "line", "levels": [-80, -20], "data": _line(ind.willr(df, n))}]
    if k == "mfi":
        n = int(_num(parts, 1, 14))
        return [{"id": spec, "name": f"MFI {n}", "pane": spec, "kind": "line", "levels": [20, 80], "data": _line(ind.mfi(df, n))}]
    if k == "roc":
        n = int(_num(parts, 1, 12))
        return [{"id": spec, "name": f"ROC {n}", "pane": spec, "kind": "line", "levels": [0], "data": _line(ind.roc(c, n))}]
    if k == "obv":
        return [{"id": spec, "name": "OBV", "pane": spec, "kind": "line", "data": _line(ind.obv(df))}]
    if k == "stochrsi":
        n, kk, d = int(_num(parts, 1, 14)), int(_num(parts, 2, 3)), int(_num(parts, 3, 3))
        st = ind.stoch_rsi(c, n, kk, d)
        return [{"id": f"{spec}:k", "name": "StochRSI K", "pane": spec, "kind": "line", "levels": [20, 80], "data": _line(st["k"])},
                {"id": f"{spec}:d", "name": "StochRSI D", "pane": spec, "kind": "line", "data": _line(st["d"])}]
    if k == "keltner":
        n, m = int(_num(parts, 1, 20)), _num(parts, 2, 2)
        kc = ind.keltner(df, n, m)
        return [{"id": f"{spec}:{col}", "name": f"KC {col}", "pane": "price", "kind": "line", "group": spec,
                 "data": _line(kc[col])} for col in ("upper", "mid", "lower")]
    if k == "ichimoku":
        a, b, s2 = int(_num(parts, 1, 9)), int(_num(parts, 2, 26)), int(_num(parts, 3, 52))
        ic = ind.ichimoku(df, a, b, s2)
        names = {"tenkan": "转换线", "kijun": "基准线", "span_a": "先行A", "span_b": "先行B"}
        return [{"id": f"{spec}:{col}", "name": names[col], "pane": "price", "kind": "line", "group": spec,
                 "data": _line(ic[col])} for col in names]
    if k == "psar":
        st, mx = _num(parts, 1, 0.02), _num(parts, 2, 0.2)
        return [{"id": spec, "name": "SAR", "pane": "price", "kind": "dots", "data": _line(ind.psar(df, st, mx))}]
    if k == "volsma":
        n = int(_num(parts, 1, 20))
        return [{"id": spec, "name": f"量 MA{n}", "pane": "volume", "kind": "line", "data": _line(ind.sma(df["volume"], n))}]
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
        if not SYMBOL_PARAM_RE.match(symbol):
            raise HTTPException(400, f"代码格式不对：{symbol[:30]}")
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
        script_errors: list[str] = []
        for raw in [x.strip() for x in indicators.split(",") if x.strip()][:20]:
            # "uid=spec" lets the client draw several instances of the same indicator
            uid, _, spec = raw.partition("=") if "=" in raw and not raw.startswith("script:") else ("", "", raw)
            if spec.startswith("script:"):
                try:
                    out = script_series(df, spec, interval)
                except DSLError as exc:
                    script_errors.append(str(exc))
                    continue
            else:
                try:
                    out = indicator_series(df, spec)
                except (ValueError, ZeroDivisionError, OverflowError) as exc:
                    raise HTTPException(400, f"指标参数无效：{spec}（{exc}）") from exc
            for s in out:
                if uid:
                    s = {**s, "id": f"{uid}|{s['id']}", "uid": uid,
                         "pane": s["pane"] if s["pane"] in ("price", "volume") else f"{uid}|{s['pane']}"}
                series.append(s)
        bars = [{"time": int(t.timestamp()), "open": round(row.open, 4), "high": round(row.high, 4),
                 "low": round(row.low, 4), "close": round(row.close, 4), "volume": float(row.volume)}
                for t, row in df.iterrows()]
        closed = closed_bars(df, interval)
        last, prev = df["close"].iloc[-1], df["close"].iloc[-2] if len(df) > 1 else df["close"].iloc[-1]
        return {
            "symbol": symbol, "interval": interval, "provider": store.provider, "bars": bars, "series": series,
            "last": {"price": round(float(last), 4), "change_pct": round((last / prev - 1) * 100, 2),
                     "time": bars[-1]["time"], "forming": len(closed) < len(df)},
            "signals": db.list_signals(200, symbol, interval),
            "script_errors": script_errors,
        }

    # ---------------------------------------------------------------- custom indicator scripts
    scripts_lock = threading.Lock()

    def get_scripts() -> list[dict]:
        raw = db.get_kv("scripts")
        return json.loads(raw) if raw else []

    def script_series(df: pd.DataFrame, spec: str, interval: str) -> list[dict]:
        """spec 'script:ID' or 'script:ID:len=30;mult=2' -> chart series (plots, hlines as levels, shapes)."""
        _, sid, *rest = spec.split(":", 2)
        sc = next((x for x in get_scripts() if x["id"] == sid), None)
        if sc is None:
            raise DSLError(f"脚本 {sid} 不存在（可能已删除）")
        inputs = {}
        for kv in (rest[0] if rest else "").split(";"):
            k, _, v = kv.partition("=")
            try:
                f = float(v)
            except ValueError:
                continue
            if math.isfinite(f):
                inputs[k.strip()] = f
        refs = {}
        for sym in script_refs(sc["source"]):
            store.maybe_update(sym, interval, min_gap_s=300)
            refs[sym] = store.get(sym, interval, len(df) + 50)
        try:
            res = run_script(sc["source"], df, inputs, refs=refs)
        except DSLError as exc:
            raise DSLError(f"{sc['name']}：{exc}") from exc
        pane = "price" if sc.get("overlay") else spec
        levels = [h["value"] for h in res["hlines"]]
        out = []
        for i, p in enumerate(res["plots"]):
            out.append({"id": f"{spec}:{i}", "name": p["title"], "pane": pane, "kind": p["kind"], "color": p["color"],
                        "group": spec, "levels": levels if i == 0 else [], "data": p["data"]})
        if not out and levels:   # hline-only script: draw an invisible carrier for the levels
            out.append({"id": f"{spec}:h", "name": sc["name"], "pane": pane, "kind": "line", "color": "#00000000",
                        "group": spec, "levels": levels, "data": [{"time": int(df.index[-1].timestamp()), "value": levels[0]}]})
        for i, sh in enumerate(res["shapes"]):
            out.append({"id": f"{spec}:s{i}", "name": sh["title"], "pane": "price", "kind": "shape", "color": sh["color"],
                        "group": spec, "direction": sh["direction"], "data": [{"time": t, "value": 0} for t in sh["times"]]})
        return out

    @r.get("/scripts")
    def list_scripts():
        return {"scripts": get_scripts(), "examples": SCRIPT_EXAMPLES}

    @r.post("/scripts/validate")
    @bad_input_is_400
    def check_script(body: dict[str, Any] = Body(...)):
        try:
            return {"ok": True, "inputs": validate_script(str(body.get("source", "")))}
        except DSLError as exc:
            return {"ok": False, "errors": [str(exc)]}

    @r.post("/scripts")
    @bad_input_is_400
    def save_script(body: dict[str, Any] = Body(...), _=require_token):
        name = str(body.get("name") or "").strip()[:60] or "自定义指标"
        source = str(body.get("source", ""))
        if len(source) > 20_000:
            raise HTTPException(400, "脚本太长")
        try:
            inputs = validate_script(source)
        except DSLError as exc:
            raise HTTPException(400, {"errors": [str(exc)]}) from exc
        sid = str(body.get("id") or "")
        item = {"id": sid if re.fullmatch(r"[a-z0-9]{6,16}", sid) else uuid.uuid4().hex[:10], "name": name,
                "overlay": bool(body.get("overlay")), "source": source, "inputs": inputs}
        with scripts_lock:   # read-modify-write of one kv blob
            scripts = [x for x in get_scripts() if x["id"] != item["id"]] + [item]
            if len(scripts) > 100:
                raise HTTPException(400, "最多保存 100 个脚本")
            db.set_kv("scripts", json.dumps(scripts, ensure_ascii=False))
        return item

    @r.delete("/scripts/{sid}")
    def delete_script(sid: str, _=require_token):
        with scripts_lock:
            db.set_kv("scripts", json.dumps([x for x in get_scripts() if x["id"] != sid], ensure_ascii=False))
        return {"deleted": sid}

    # ---------------------------------------------------------------- options + calendar
    @r.get("/options")
    def options(symbol: str, expiry: str | None = None):
        sym = symbol.upper().strip()
        if not SYMBOL_PARAM_RE.match(sym):
            raise HTTPException(400, "代码无效")
        try:
            return get_chain(db, settings, sym, expiry or None)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"期权数据获取失败：{exc}") from exc

    @r.get("/calendar")
    def calendar(start: str | None = None, days: int = 7):
        try:
            d0 = date.fromisoformat(start) if start else date.today() - timedelta(days=date.today().weekday())
        except ValueError as exc:
            raise HTTPException(400, "start 格式应为 YYYY-MM-DD") from exc
        if abs((d0 - date.today()).days) > 400:
            raise HTTPException(400, "只能查询前后一年内的日历")
        symbols = list(db.get_watchlist(settings.watchlist))
        try:
            return get_calendar(db, settings, d0, days, symbols)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"日历获取失败：{exc}") from exc

    # ---------------------------------------------------------------- drawings (per symbol, shared across intervals)
    @r.get("/drawings")
    def get_drawings(symbol: str):
        raw = db.get_kv(f"drawings:{symbol.upper().strip()}")
        return json.loads(raw) if raw else []

    @r.put("/drawings")
    def put_drawings(symbol: str, drawings: list[dict[str, Any]] = Body(...), _=require_token):
        if len(drawings) > 500:
            raise HTTPException(400, "最多保存 500 个图形")
        try:
            blob = json.dumps(drawings, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            raise HTTPException(400, "图形数据里有无效数字") from exc
        if len(blob) > 500_000:
            raise HTTPException(400, "图形数据太大")
        db.set_kv(f"drawings:{symbol.upper().strip()}", blob)
        return {"saved": len(drawings)}

    # ---------------------------------------------------------------- fundamentals + symbol news
    @r.get("/fundamentals")
    def fundamentals(symbol: str, refresh: bool = False):
        sym = symbol.upper().strip()
        if not SYMBOL_PARAM_RE.match(sym):
            raise HTTPException(400, "代码无效")
        try:
            data = get_fundamentals(db, settings, sym, refresh=refresh)
        except LookupError as exc:
            raise HTTPException(404, f"{sym} 没有基本面数据（指数、外汇、加密货币通常没有）") from exc
        except Exception as exc:  # noqa: BLE001 - Yahoo fails in many ways
            raise HTTPException(502, f"基本面数据获取失败：{exc}") from exc
        news = db.articles_for(sym, utcnow() - timedelta(days=14), limit=20)
        return {**data, "news": news}

    # ---------------------------------------------------------------- screener
    @r.post("/screener")
    @bad_input_is_400
    def screener(body: dict[str, Any] = Body(default={})):
        symbols = [s.strip().upper() for s in body.get("symbols") or [] if s and s.strip()]
        if not symbols:
            symbols = list(db.get_watchlist(settings.watchlist))
            snap = db.latest_snapshot() or {}
            symbols += [r["ticker"] for r in snap.get("ranking", [])]
        symbols = list(dict.fromkeys(symbols))[:100]
        try:
            return run_screener(store, symbols, (body.get("filter") or "").strip())
        except DSLError as exc:
            raise HTTPException(400, {"errors": [f"筛选条件：{exc}"]}) from exc

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
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(400, f"策略格式错误：{exc}") from exc
        errors = cfg.validate()
        if errors:
            raise HTTPException(400, {"errors": errors})
        return cfg

    @r.post("/strategies/validate")
    @bad_input_is_400
    def validate_strategy(body: dict[str, Any] = Body(...)):
        try:
            cfg = StrategyConfig.from_dict(body)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "errors": [str(exc)]}
        errors = cfg.validate()
        return {"ok": not errors, "errors": errors}

    @r.post("/strategies")
    @bad_input_is_400
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
    @bad_input_is_400
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
            res = run_backtest(df, cfg, settings.commission_bps, settings.slippage_bps,
                               refs=load_refs(store, cfg, MAX_BARS))
        except DSLError as exc:
            raise HTTPException(400, {"errors": [str(exc)]}) from exc
        return {"symbol": symbol, "interval": cfg.interval, "costs_bps": {"commission": settings.commission_bps,
                "slippage": settings.slippage_bps}, **res.to_dict()}

    @r.post("/scan")
    @bad_input_is_400
    def scan_symbols(body: dict[str, Any] = Body(...)):
        """Body: {strategy: {...} | strategy_id, symbols?: [...], universe?: "watchlist"|"hot"|"both", interval?}"""
        if body.get("strategy_id"):
            raw = db.get_strategy(int(body["strategy_id"]))
            if not raw:
                raise HTTPException(404, "策略不存在")
        else:
            raw = body.get("strategy") or {}
        if body.get("interval"):
            raw = {**raw, "interval": body["interval"]}
        cfg = parse_cfg(raw)
        symbols = [s.strip().upper() for s in body.get("symbols") or [] if s and s.strip()]
        if not symbols:
            universe = body.get("universe") or "both"
            if universe in ("watchlist", "both"):
                symbols += db.get_watchlist(settings.watchlist)
            if universe in ("hot", "both"):
                snap = db.latest_snapshot() or {}
                symbols += [r["ticker"] for r in snap.get("ranking", [])]
            symbols += cfg.symbols
        if not symbols:
            raise HTTPException(400, "没有可扫描的代码")
        return scan(store, cfg, symbols[:MAX_SYMBOLS], settings.commission_bps, settings.slippage_bps)

    @r.post("/optimize")
    @bad_input_is_400
    def optimize_params(body: dict[str, Any] = Body(...)):
        """Body: {strategy: {...}, symbol, interval?, grid: {"fast": "5:30:5"}, mode: split|walk_forward,
        objective, train_pct, folds, min_trades}"""
        raw = body.get("strategy") or {}
        if body.get("interval"):
            raw = {**raw, "interval": body["interval"]}
        cfg = parse_cfg(raw)
        symbol = (body.get("symbol") or (cfg.symbols[0] if cfg.symbols else "")).upper()
        if not symbol:
            raise HTTPException(400, "请指定代码")
        df = closed_bars(load(symbol, cfg.interval, int(body.get("limit") or 3000)), cfg.interval)
        try:
            out = optimize(df, cfg, body.get("grid") or {}, mode=body.get("mode", "split"),
                           objective=body.get("objective", "sharpe"), train_pct=float(body.get("train_pct", 70)),
                           folds=int(body.get("folds", 4)), min_trades=int(body.get("min_trades", 3)),
                           commission_bps=settings.commission_bps, slippage_bps=settings.slippage_bps,
                           refs=load_refs(store, cfg, MAX_BARS))
        except (OptimizeError, DSLError) as exc:
            raise HTTPException(400, {"errors": [str(exc)]}) from exc
        return {"symbol": symbol, "interval": cfg.interval, "objectives": OBJECTIVES, **out}

    # ---------------------------------------------------------------- paper account
    @r.get("/paper")
    def paper_summary():
        return engine.paper.summary()

    @r.post("/paper/reset")
    @bad_input_is_400
    def paper_reset(body: dict[str, Any] = Body(default={}), _=require_token):
        engine.paper.reset(body.get("capital"))
        return engine.paper.summary()

    @r.post("/paper/process")
    def paper_process(_=require_token):
        return {"events": engine.paper.process()}

    # ---------------------------------------------------------------- broker (Alpaca)
    @r.get("/broker")
    def broker_status():
        return engine.broker.status()

    @r.post("/broker/pause")
    def broker_pause(body: dict[str, Any] = Body(...), _=require_token):
        engine.broker.set_paused(bool(body.get("paused", True)))
        return {"paused": engine.broker.paused}

    @r.post("/broker/flatten")
    def broker_flatten(body: dict[str, Any] = Body(...), _=require_token):
        if not settings.report_api_token:
            raise HTTPException(400, "紧急清仓会动用真实账户，必须先设置 API_TOKEN")
        if body.get("confirm") != "FLATTEN":
            raise HTTPException(400, '需要 {"confirm": "FLATTEN"} 才会清仓')
        return engine.broker.flatten_all()

    # ---------------------------------------------------------------- realtime
    @r.get("/realtime")
    def realtime_status():
        return {**engine.realtime.status, "mode": engine.realtime.mode,
                "watched": len(engine.realtime.watched())}

    # ---------------------------------------------------------------- alerts
    @r.get("/alerts")
    def list_alerts(symbol: str | None = None):
        return db.list_alerts(symbol=symbol.upper() if symbol else None)

    @r.post("/alerts")
    @bad_input_is_400
    def save_alert(body: dict[str, Any] = Body(...), _=require_token):
        try:
            a = Alert.from_dict(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, {"errors": [str(exc)]}) from exc
        errors = a.validate()
        if errors:
            raise HTTPException(400, {"errors": errors})
        if a.id is None:
            a.last_bar_ts = a.last_triggered = None
        a.id = db.save_alert(a.to_dict())
        return next(x for x in db.list_alerts() if x["id"] == a.id)

    @r.delete("/alerts/{alert_id}")
    def delete_alert(alert_id: int, _=require_token):
        if not db.delete_alert(alert_id):
            raise HTTPException(404, "提醒不存在")
        return {"deleted": alert_id}

    @r.post("/alerts/run")
    def run_alerts(_=require_token):
        return {"fired": engine.run_alerts()}

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
