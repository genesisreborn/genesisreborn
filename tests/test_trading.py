"""Indicators, strategy DSL, backtester, live signals and the chart/strategy API."""
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stockpulse import indicators as ind
from stockpulse.config import Settings
from stockpulse.db import Database
from stockpulse.market_data import BarStore, closed_bars, demo_bars, fetch_massive
from stockpulse.strategy import StrategyConfig
from stockpulse.strategy.backtest import run_backtest
from stockpulse.strategy.dsl import DSLError, Evaluator, validate
from stockpulse.strategy.presets import PRESETS, preset
from stockpulse.strategy.signals import SignalEngine, evaluate


def frame(closes, opens=None, highs=None, lows=None, start="2026-01-05"):
    idx = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens if opens is not None else c, dtype=float)
    h = np.asarray(highs if highs is not None else np.maximum(o, c), dtype=float)
    lo = np.asarray(lows if lows is not None else np.minimum(o, c), dtype=float)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1e6}, index=idx)


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "t.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = ""
    s.telegram_bot_token = ""
    s.massive_api_key = ""
    s.report_api_token = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------- indicators
def test_sma_ema_rsi_basics():
    s = pd.Series(np.arange(1, 31, dtype=float))
    assert ind.sma(s, 5).iloc[-1] == 28.0
    assert np.isnan(ind.sma(s, 5).iloc[3])
    assert ind.rsi(s, 14).iloc[-1] == 100.0          # only gains
    down = pd.Series(np.arange(30, 0, -1, dtype=float))
    assert ind.rsi(down, 14).iloc[-1] < 1
    e = ind.ema(pd.Series([10.0] * 40), 10)
    assert abs(e.iloc[-1] - 10) < 1e-9


def test_crossover_and_no_lookahead():
    a = pd.Series([1, 2, 3, 2, 1, 2, 3], dtype=float)
    assert ind.crossover(a, 2.5).tolist() == [False, False, True, False, False, False, True]
    assert ind.crossunder(a, 1.5).tolist() == [False] * 4 + [True, False, False]
    df = demo_bars("XYZ", "1d", 300)
    full = ind.supertrend(df, 10, 3)
    part = ind.supertrend(df.iloc[:200], 10, 3)
    pd.testing.assert_frame_equal(full.iloc[:200], part)


# ---------------------------------------------------------------- DSL
def test_dsl_evaluates_and_indexes_history():
    df = frame([10, 11, 12, 13, 12, 14])
    ev = Evaluator(df, {"lvl": 12})
    assert ev.eval("close > lvl").tolist() == [False, False, False, True, False, True]
    assert ev.eval("close > close[1] and not (close > 13.5)").tolist() == [False, True, True, True, False, False]
    assert ev.eval("1 < close - 10 < 3").tolist() == [False, False, True, False, True, False]


@pytest.mark.parametrize("expr,msg", [
    ("__import__('os').system('x')", "未知函数"),
    ("close.__class__", "不支持"),
    ("foo > 1", "未知名称"),
    ("ema(close, n=5)", "位置"),
    ("close >", "语法"),
    ("2 ** 99999", "不支持"),
    ("", "为空"),
])
def test_dsl_rejects_unsafe_or_bad(expr, msg):
    with pytest.raises(DSLError, match=msg):
        validate(expr)


def test_all_presets_valid():
    for key in PRESETS:
        assert preset(key).validate() == [], key


def test_strategy_validation_messages():
    cfg = StrategyConfig.from_dict({"name": "", "interval": "3d", "params": {"Bad": 1}, "long_entry": "rsi(close) <"})
    errs = " ".join(cfg.validate())
    assert "名称" in errs and "周期" in errs and "参数名" in errs and "做多开仓" in errs


# ---------------------------------------------------------------- backtest
def test_backtest_fills_next_open_with_costs():
    #           0    1    2    3    4    5
    closes = [100, 100, 105, 110, 108, 104]
    opens = [100, 100, 101, 106, 111, 107]
    cfg = StrategyConfig(name="t", long_entry="close > 103", long_exit="close < 109", interval="1d")
    res = run_backtest(frame(closes, opens), cfg, commission_bps=10, slippage_bps=0)
    ev = [(e.action, e.time.day, e.price) for e in res.events]
    # signal on bar 2 close -> buy bar 3 open (106); exit signal on bar 4 close -> sell bar 5 open (107)
    assert ev == [("BUY", 8, 106.0), ("SELL", 10, 107.0)]
    t = res.trades[0]
    assert t.pnl_pct == pytest.approx((107 / 106 - 1 - 0.002) * 100)
    assert res.metrics["trades"] == 1 and res.position == 0
    qty = 10000 / 106
    final_cash = 10000 - 10000 * 0.001 - 10000 + qty * 107 * (1 - 0.001)
    assert res.equity.iloc[-1] == pytest.approx(final_cash)


def test_stop_loss_intrabar_and_gap():
    closes = [100, 101, 100, 99, 98]
    opens = [100, 100, 100, 99.5, 90]
    lows = [99, 100, 94, 98, 89]
    cfg = StrategyConfig(name="t", long_entry="close > 100.5", stop_loss_pct=5, interval="1d")
    res = run_backtest(frame(closes, opens, lows=lows), cfg, 0, 0)
    # buy at bar 2 open 100 -> stop 95; bar 2 low 94 -> stopped at 95
    assert [(e.action, e.reason, e.price) for e in res.events] == [("BUY", "signal", 100.0), ("SELL", "stop", 95.0)]
    # gap through the stop fills at the open
    cfg2 = StrategyConfig(name="t", long_entry="close > 100.5 and close[1] < 100.5", stop_loss_pct=5, interval="1d")
    closes2, opens2, lows2 = [100, 101, 100, 99, 98], [100, 100, 100, 99.5, 90], [99, 100, 96, 98, 89]
    res2 = run_backtest(frame(closes2, opens2, lows=lows2), cfg2, 0, 0)
    assert res2.events[-1].reason == "stop" and res2.events[-1].price == 90.0


def test_short_and_reverse():
    closes = [100, 99, 98, 99, 101, 103, 102]
    cfg = StrategyConfig(name="t", long_entry="close > close[1] and close[1] > close[2]",
                         short_entry="close < close[1] and close[1] < close[2]", interval="1d")
    res = run_backtest(frame(closes), cfg, 0, 0)
    actions = [e.action for e in res.events]
    assert actions[:1] == ["SHORT"] and "COVER" in actions and actions[-1] == "BUY"
    assert res.trades[0].side == "short" and res.trades[0].entry_price == 99 and res.trades[0].exit_price == 103 and res.trades[0].pnl_pct < 0


def test_backtest_has_no_lookahead():
    df = demo_bars("ABC", "1d", 500)
    cfg = preset("ema_cross")
    full = run_backtest(df, cfg)
    cut = run_backtest(df.iloc[:350], cfg)
    t_cut = df.index[349]
    a = [(e.time, e.action, round(e.price, 6)) for e in full.events if e.time <= t_cut]
    b = [(e.time, e.action, round(e.price, 6)) for e in cut.events]
    assert a == b


# ---------------------------------------------------------------- data
def test_closed_bars_drops_forming_bar():
    df = demo_bars("ABC", "5m", 10, end=datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc))
    now = df.index[-1].to_pydatetime() + timedelta(minutes=2)
    assert len(closed_bars(df, "5m", now)) == 9
    assert len(closed_bars(df, "5m", now + timedelta(minutes=5))) == 10
    daily = frame([1, 2, 3], start="2026-10-14").tz_convert("UTC")
    daily.index = daily.index + pd.Timedelta(hours=4)  # 00:00 New York
    assert len(closed_bars(daily, "1d", datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))) == 2   # 14:00 NY
    assert len(closed_bars(daily, "1d", datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc))) == 3  # 16:30 NY


def test_bar_store_upsert(tmp_path):
    s = settings(tmp_path)
    store = BarStore(Database(s.db_path), s)
    assert store.update("aapl", "1d") > 0
    df1 = store.get("AAPL", "1d", 10_000)
    store.update("AAPL", "1d")
    df2 = store.get("AAPL", "1d", 10_000)
    assert len(df1) == len(df2) and df2.index.is_monotonic_increasing


def test_fetch_massive_pagination():
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if "cursor" not in str(req.url):
            return httpx.Response(200, json={"results": [{"t": 1760500800000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}],
                                             "next_url": "https://api.massive.com/v2/aggs/next?cursor=abc"})
        return httpx.Response(200, json={"results": [{"t": 1760587200000, "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 12}]})

    s = Settings()
    s.massive_api_key = "k"
    df = fetch_massive("aapl", "1d", False, s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert list(df["close"]) == [1.5, 1.8] and "/AAPL/range/1/day/" in calls[0]
    assert all("apiKey=k" in c for c in calls)


# ---------------------------------------------------------------- signals
def test_live_signal_from_pending_order():
    closes = [100] * 30 + [101, 102, 110]
    df = frame(closes)
    cfg = StrategyConfig(name="breakout", long_entry="close > highest(high, 10)[1] * 1.05",
                         stop_loss_pct=5, interval="1d", symbols=["X"])
    st = evaluate(cfg, df, now=df.index[-1].to_pydatetime() + timedelta(days=2))
    assert st["signals"] == [{"action": "BUY", "reason": "signal", "price": 110.0, "stop": 104.5}]
    assert st["position"] == 0


def test_signal_engine_records_once_and_notifies(tmp_path):
    s = settings(tmp_path)
    db = Database(s.db_path)
    eng = SignalEngine(db, s)
    # Always-true entry and exit after 1 bar guarantees a pending order on the last bar.
    cfg = StrategyConfig(name="flip", long_entry="close > 0", long_exit="close > 0", interval="1d",
                         symbols=["AAPL", "MSFT"], enabled=True)
    db.save_strategy(cfg.to_dict())
    now = datetime.now(timezone.utc) + timedelta(days=1)
    first = eng.run(now=now)
    assert {x["symbol"] for x in first} == {"AAPL", "MSFT"}
    assert eng.run(now=now) == []                       # deduplicated
    pos = eng.positions(now=now)
    assert len(pos) == 2 and all("position" in p for p in pos)


# ---------------------------------------------------------------- API
def test_trading_api(tmp_path):
    from stockpulse.web.app import create_app

    s = settings(tmp_path, report_api_token="tok")
    app = create_app(s, start_scheduler=False)
    auth = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        assert "lightweight-charts" in c.get("/chart").text
        assert c.get("/static/vendor/lightweight-charts.standalone.production.js").status_code == 200

        r = c.get("/api/chart", params={"symbol": "nvda", "interval": "1h", "indicators": "ema:20,bb:20:2,rsi,macd,supertrend"})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "NVDA" and len(body["bars"]) > 100
        panes = {x["pane"] for x in body["series"]}
        assert panes == {"price", "rsi", "macd"}
        assert c.get("/api/chart", params={"symbol": "X", "interval": "2h"}).status_code == 400
        assert c.get("/api/chart", params={"symbol": "X", "indicators": "nope:1"}).status_code == 400

        meta = c.get("/api/strategy/meta").json()
        assert "ema_cross" in meta["presets"] and "crossover" in meta["functions"]

        bad = {"name": "x", "long_entry": "close >"}
        assert c.post("/api/strategies/validate", json=bad).json()["ok"] is False
        assert c.post("/api/strategies", json=preset("ema_cross").to_dict()).status_code == 401

        cfg = preset("donchian_breakout", symbols=["AAPL"], enabled=True).to_dict()
        saved = c.post("/api/strategies", json=cfg, headers=auth).json()
        assert saved["id"] and saved["enabled"]
        bt = c.post("/api/backtest", json={"strategy_id": saved["id"], "symbol": "AAPL"}).json()
        assert bt["metrics"]["bars"] > 100 and "equity" in bt and bt["costs_bps"]["commission"] == 5

        adhoc = c.post("/api/backtest", json={"strategy": {"name": "a", "long_entry": "rsi(close, 14) < 30",
                                                           "long_exit": "rsi(close, 14) > 60"},
                                              "symbol": "TSLA", "interval": "1d"})
        assert adhoc.status_code == 200
        assert c.post("/api/backtest", json={"strategy": {"name": "a", "long_entry": "zz > 1"}, "symbol": "TSLA"}).status_code == 400

        assert c.put("/api/watchlist", json=["aapl", "AAPL", " tsla "], headers=auth).json()["symbols"] == ["AAPL", "TSLA"]
        wl = c.get("/api/watchlist").json()
        assert [w["symbol"] for w in wl] == ["AAPL", "TSLA"] and wl[0]["price"]

        assert "new" in c.post("/api/signals/run", headers=auth).json()
        assert isinstance(c.get("/api/signals").json(), list)
        assert isinstance(c.get("/api/positions").json(), list)
        st = c.get("/api/status").json()
        assert st["market_data"] == "demo" and st["active_strategies"] == 1

        assert c.delete(f"/api/strategies/{saved['id']}", headers=auth).status_code == 200
        assert c.delete(f"/api/strategies/{saved['id']}", headers=auth).status_code == 404


def test_engine_pushes_new_signals_to_telegram(tmp_path):
    import json as _json

    from stockpulse.scheduler import Engine
    from stockpulse.telegram_bot import TelegramNotifier, format_signal

    sent = []

    def handler(req):
        sent.append(_json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    s = settings(tmp_path, telegram_bot_token="1:x", telegram_chat_ids=["99"])
    eng = Engine(s)
    eng.notifier = TelegramNotifier(s, eng.db, client=httpx.Client(transport=httpx.MockTransport(handler)))
    cfg = StrategyConfig(name="always", long_entry="close > 0", long_exit="close > 0", interval="5m",
                         symbols=["AAPL"], enabled=True, stop_loss_pct=3)
    eng.db.save_strategy(cfg.to_dict())
    new = eng.run_signals()
    assert len(new) == 1 and len(sent) == 1
    text = sent[0]["text"]
    assert "AAPL" in text and "always" in text and "不构成投资建议" in text
    assert eng.run_signals() == [] and len(sent) == 1      # same bar -> no duplicate push

    msg = format_signal({"action": "BUY", "symbol": "NVDA", "interval": "1d", "strategy": "x<y>", "reason": "signal",
                         "bar_ts": 1760500800, "price": 101.5, "stop": 96.4})
    assert "买入做多" in msg and "x&lt;y&gt;" in msg and "参考止损 96.4" in msg and "2025-10-15" in msg
