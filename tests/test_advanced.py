"""Parameter optimisation, paper account, and real-time alerts."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stockpulse.config import Settings
from stockpulse.db import Database
from stockpulse.market_data import BarStore, demo_bars
from stockpulse.realtime import RealtimeMonitor, check_alert
from stockpulse.strategy import StrategyConfig
from stockpulse.strategy.optimize import OptimizeError, optimize, parse_grid, window_metrics
from stockpulse.strategy.paper import PaperAccount
from stockpulse.strategy.presets import preset


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "t.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = s.telegram_bot_token = s.massive_api_key = s.report_api_token = ""
    s.telegram_chat_ids = []
    s.commission_bps = 0
    s.slippage_bps = 0
    s.paper_capital = 10_000
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------- optimiser
def test_parse_grid():
    g = parse_grid({"fast": "5:20:5", "slow": [50, 20, 50], "k": "1,2"})
    assert g == {"fast": [5, 10, 15, 20], "slow": [20, 50], "k": [1, 2]}
    with pytest.raises(OptimizeError):
        parse_grid({"a": "1:1000:1", "b": "1:1000:1"})
    with pytest.raises(OptimizeError):
        parse_grid({"a": "10:5:1"})
    with pytest.raises(OptimizeError):
        parse_grid({})


def test_window_metrics_slice():
    idx = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
    eq = pd.Series([100, 110, 99, 120, 132], index=idx, dtype=float)
    m = window_metrics(eq, [], idx[2], idx[4], "1d")
    # base is the close before the window, so the move into its first bar counts
    assert m["total_return_pct"] == pytest.approx((132 / 110 - 1) * 100, abs=0.01)
    assert m["max_drawdown_pct"] == pytest.approx((99 / 110 - 1) * 100, abs=0.01)
    # adjacent windows chain to the full-period return
    a = window_metrics(eq, [], idx[0], idx[2], "1d")["total_return_pct"]
    b = window_metrics(eq, [], idx[3], idx[4], "1d")["total_return_pct"]
    assert (1 + a / 100) * (1 + b / 100) == pytest.approx(1.32, abs=1e-3)


def test_optimize_split_and_walk_forward():
    df = demo_bars("OPT", "1d", 900)
    cfg = preset("ema_cross")
    r = optimize(df, cfg, {"fast": "5:20:5", "slow": "30:90:30"}, mode="split", train_pct=70, min_trades=1)
    assert r["combos"] == 12 and len(r["rows"]) == 12
    assert set(r["best_params"]) == {"fast", "slow"}
    assert r["train_range"][1] < r["test_range"][0]
    assert r["heatmap"]["x"] == "fast" and len(r["heatmap"]["cells"]) == 12
    scores = [x["score"] for x in r["rows"] if x["score"] is not None]
    assert scores == sorted(scores, reverse=True)

    wf = optimize(df, cfg, {"fast": "5:20:5", "slow": "30:90:30"}, mode="walk_forward", folds=4, min_trades=1)
    assert len(wf["folds"]) == 4
    for a, b in zip(wf["folds"], wf["folds"][1:]):
        assert a["test_range"][1] < b["test_range"][0]          # test windows never overlap
        assert a["train_range"][1] < a["test_range"][0]         # train always before its test
    assert 0 <= wf["param_stability_pct"] <= 100
    assert isinstance(wf["warnings"], list)


def test_optimize_rejects_unknown_param():
    with pytest.raises(OptimizeError, match="不在策略参数"):
        optimize(demo_bars("X", "1d", 400), preset("ema_cross"), {"nope": "1:3:1"})


# ---------------------------------------------------------------- paper account
def _bars(store, sym, rows, start="2026-03-02"):
    idx = pd.date_range(start, periods=len(rows), freq="D", tz="UTC") + pd.Timedelta(hours=4)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1e6
    store.save(sym, "1d", df)
    return idx


def test_paper_fill_next_open_and_stop(tmp_path):
    s = settings(tmp_path)
    db = Database(s.db_path)
    store = BarStore(db, s)
    acct = PaperAccount(db, store, s)
    cfg = StrategyConfig(name="p", long_entry="close > 0", interval="1d", symbols=["AAA"], stop_loss_pct=5,
                         execution="paper", alloc_pct=50)
    sid = db.save_strategy(cfg.to_dict())
    idx = _bars(store, "AAA", [(100, 101, 99, 100), (102, 103, 101, 102)])
    sig = {"id": 1, "strategy_id": sid, "strategy": "p", "symbol": "AAA", "interval": "1d", "action": "BUY",
           "reason": "signal", "bar_ts": int(idx[0].timestamp()), "price": 100}
    assert acct.submit(sig)["status"] == "pending"
    assert acct.submit(sig) is None                     # same signal twice -> one order
    now = idx[-1].to_pydatetime() + timedelta(days=1)
    ev = acct.process(now=now, update=False)
    assert ev[0]["type"] == "fill" and ev[0]["price"] == 102 and ev[0]["stop"] == pytest.approx(96.9)
    pos = acct.positions()[0]
    assert pos["qty"] == pytest.approx(5000 / 102) and acct.cash == pytest.approx(5000)

    # next bar gaps below the stop -> filled at the open
    _bars(store, "AAA", [(95, 96, 94, 95)], start="2026-03-04")
    ev = acct.process(now=now + timedelta(days=2), update=False)
    assert ev[0]["type"] == "close" and ev[0]["reason"] == "stop" and ev[0]["price"] == 95
    summ = acct.summary()
    assert summ["positions"] == [] and summ["trades_count"] == 1
    assert summ["equity"] == pytest.approx(10_000 + (95 - 102) * 5000 / 102)
    assert summ["equity_curve"]


def test_paper_exit_requires_position_and_reverse(tmp_path):
    s = settings(tmp_path)
    db = Database(s.db_path)
    store = BarStore(db, s)
    acct = PaperAccount(db, store, s)
    sid = db.save_strategy(StrategyConfig(name="r", long_entry="close>0", short_entry="close<0", interval="1d",
                                          symbols=["BBB"], execution="paper").to_dict())
    idx = _bars(store, "BBB", [(50, 51, 49, 50), (50, 52, 49, 51), (51, 53, 50, 52), (52, 53, 48, 49)])
    base = {"strategy_id": sid, "strategy": "r", "symbol": "BBB", "interval": "1d", "price": 50}
    assert acct.submit({**base, "id": 1, "action": "SELL", "reason": "signal", "bar_ts": int(idx[0].timestamp())}) is None
    acct.submit({**base, "id": 2, "action": "BUY", "reason": "signal", "bar_ts": int(idx[0].timestamp())})
    acct.process(now=idx[-1].to_pydatetime() + timedelta(days=1), update=False)
    # reversal: SELL then SHORT on bar 2 -> both fill at bar 3 open
    acct.submit({**base, "id": 3, "action": "SELL", "reason": "reverse", "bar_ts": int(idx[2].timestamp())})
    acct.submit({**base, "id": 4, "action": "SHORT", "reason": "signal", "bar_ts": int(idx[2].timestamp())})
    ev = acct.process(now=idx[-1].to_pydatetime() + timedelta(days=1), update=False)
    assert [e["type"] for e in ev] == ["close", "fill"]
    assert acct.positions()[0]["qty"] < 0


def test_engine_routes_paper_signals(tmp_path):
    from stockpulse.scheduler import Engine

    s = settings(tmp_path)
    eng = Engine(s)
    cfg = StrategyConfig(name="always", long_entry="close > 0", long_exit="close < 0", interval="1d",
                         symbols=["AAPL"], enabled=True, execution="paper")
    sid = eng.db.save_strategy(cfg.to_dict())
    bar_ts = int(eng.signals.store.get("AAPL", "1d", 5).index[-2].timestamp())
    eng.signals.run = lambda: [{"id": 7, "strategy_id": sid, "strategy": "always", "symbol": "AAPL", "interval": "1d",
                                "action": "BUY", "reason": "signal", "bar_ts": bar_ts, "price": 1, "execution": "paper"},
                               {"id": 8, "strategy_id": sid, "strategy": "always", "symbol": "MSFT", "interval": "1d",
                                "action": "BUY", "reason": "signal", "bar_ts": bar_ts, "price": 1, "execution": "signal"}]
    eng.run_signals()
    orders = eng.paper.summary()["orders"]
    assert len(orders) == 1 and orders[0]["symbol"] == "AAPL"
    assert orders[0]["status"] == "filled"            # next bar already exists -> filled on the same run
    assert eng.paper.positions()[0]["symbol"] == "AAPL"


# ---------------------------------------------------------------- real-time
def test_check_alert_kinds():
    assert check_alert({"kind": "cross_up", "level": 10}, 9.9, 10.0)
    assert not check_alert({"kind": "cross_up", "level": 10}, None, 11)      # needs a previous price
    assert not check_alert({"kind": "cross_up", "level": 10}, 10.5, 11)
    assert check_alert({"kind": "cross_down", "level": 10}, 10.1, 9.9)
    assert check_alert({"kind": "above", "level": 10}, None, 10.01)
    assert check_alert({"kind": "below", "level": 10}, None, 9)


def test_realtime_monitor_fires_once(tmp_path):
    from stockpulse.strategy.alerts import Alert, AlertEngine

    s = settings(tmp_path, realtime_mode="poll")
    db = Database(s.db_path)
    fired = []
    prices = iter([99.0, 99.5, 100.2, 101.0])
    mon = RealtimeMonitor(db, s, on_fire=fired.append, price_fn=lambda sym: next(prices))
    a = Alert(symbol="AAPL", condition="crossover(close, 100)", kind="cross_up", level=100, realtime=True)
    assert a.validate() == []
    bad = Alert(symbol="AAPL", condition="rsi(close) < 30", kind="rsi_below", realtime=True)
    assert bad.validate()
    db.save_alert(a.to_dict())
    for _ in range(4):
        mon.poll_once()
    assert len(fired) == 1 and fired[0]["price"] == 100.2
    assert db.list_alerts(active_only=True) == []     # once -> closed
    # bar-based engine ignores real-time alerts
    db.save_alert(Alert(symbol="AAPL", condition="close > 0", kind="above", level=0, realtime=True).to_dict())
    assert AlertEngine(db, BarStore(db, s)).run(now=datetime.now(timezone.utc) + timedelta(days=2)) == []


# ---------------------------------------------------------------- API
def test_advanced_api(tmp_path):
    from stockpulse.web.app import create_app

    s = settings(tmp_path)
    app = create_app(s, start_scheduler=False)
    with TestClient(app) as c:
        body = {"strategy": preset("ema_cross").to_dict(), "symbol": "AAPL", "interval": "1d",
                "grid": {"fast": "5:15:5", "slow": "30:60:30"}, "mode": "walk_forward", "folds": 3, "min_trades": 1}
        r = c.post("/api/optimize", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "walk_forward" and len(r.json()["folds"]) == 3
        assert c.post("/api/optimize", json={**body, "grid": {"zzz": "1:2:1"}}).status_code == 400

        p = c.get("/api/paper").json()
        assert p["equity"] == 10_000 and p["positions"] == []
        assert c.post("/api/paper/reset", json={"capital": 5000}).json()["initial"] == 5000
        assert "events" in c.post("/api/paper/process").json()
        rt = c.get("/api/realtime").json()
        assert rt["mode"] == "poll" and rt["watched"] == 0

        bad = preset("ema_cross", execution="broker").to_dict()  # broker needs symbols
        assert c.post("/api/strategies/validate", json=bad).json()["ok"] is False


# ---------------------------------------------------------------- refs & risk sizing
def test_ref_functions_align_and_validate():
    from stockpulse.strategy.dsl import DSLError, Evaluator, referenced_symbols, validate

    idx = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
    main = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [1, 2, 3, 4, 5], "volume": 1.0}, index=idx)
    spy = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [10.0, 30.0], "volume": 1.0}, index=idx[[0, 2]])
    ev = Evaluator(main, refs={"spy": spy})
    assert ev.eval_value('ref_close("SPY")').tolist() == [10, 10, 30, 30, 30]       # forward-filled, never from the future
    assert ev.eval('ref_close("SPY") > 20').tolist() == [False, False, True, True, True]
    with pytest.raises(DSLError, match="缺少 QQQ"):
        Evaluator(main).eval('ref_close("QQQ") > 0')
    with pytest.raises(DSLError, match="代码字符串"):
        validate("ref_close(close) > 0")
    validate('close > sma(ref_close("SPY"), 200)')                                    # validation needs no data
    assert referenced_symbols('ref_close("spy") > 1', 'ref_high("QQQ") > 1', "") == {"SPY", "QQQ"}


def test_market_filter_blocks_entries(tmp_path):
    from stockpulse.strategy.backtest import run_backtest
    from stockpulse.strategy.refs import load_refs

    s = settings(tmp_path)
    store = BarStore(Database(s.db_path), s)
    df = store.get("AAPL", "1d", 1500)
    base = StrategyConfig(name="b", long_entry="close > sma(close, 50)", long_exit="close < sma(close, 50)")
    filt = StrategyConfig(name="f", long_entry='close > sma(close, 50) and ref_close("SPY") > 1e9',
                          long_exit="close < sma(close, 50)")
    refs = load_refs(store, filt, 1500)
    assert "SPY" in refs and len(refs["SPY"]) > 100
    assert run_backtest(df, base).metrics["trades"] > 0
    assert run_backtest(df, filt, refs=refs).metrics["trades"] == 0        # SPY never above 1e9


def test_risk_based_position_size():
    from stockpulse.strategy.backtest import position_size, run_backtest

    assert position_size(10_000, 100, 95, 1, 100) == pytest.approx(20)       # 1% of 10k / $5 risk
    assert position_size(10_000, 100, 99, 1, 50) == pytest.approx(50)        # capped at 50% of equity
    assert position_size(10_000, 100, None, 1, 10) == pytest.approx(10)      # no stop -> allocation
    cfg = StrategyConfig(name="r", long_entry="close > 0", stop_loss_pct=5, risk_pct=1, interval="1d")
    assert cfg.validate() == []
    assert StrategyConfig(name="r", long_entry="close > 0", risk_pct=1).validate()   # needs a stop
    idx = pd.date_range("2026-01-01", periods=4, freq="D", tz="UTC")
    df = pd.DataFrame({"open": [100.0] * 4, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}, index=idx)
    res = run_backtest(df, cfg, 0, 0)
    # 1% risk of 10k with a 5% stop -> $2,000 position = 20 shares
    assert res.trades[-1].entry_price == 100 and res.equity.iloc[-1] == pytest.approx(10_000)
    eq_before = res.equity.iloc[1]
    assert eq_before == pytest.approx(10_000)


def test_paper_uses_risk_sizing(tmp_path):
    s = settings(tmp_path)
    db = Database(s.db_path)
    store = BarStore(db, s)
    acct = PaperAccount(db, store, s)
    sid = db.save_strategy(StrategyConfig(name="r", long_entry="close>0", interval="1d", symbols=["CCC"],
                                          stop_loss_pct=5, risk_pct=1, alloc_pct=50, execution="paper").to_dict())
    idx = _bars(store, "CCC", [(100, 101, 99, 100), (100, 101, 99, 100)])
    acct.submit({"id": 1, "strategy_id": sid, "strategy": "r", "symbol": "CCC", "interval": "1d", "action": "BUY",
                 "reason": "signal", "bar_ts": int(idx[0].timestamp()), "price": 100})
    acct.process(now=idx[-1].to_pydatetime() + timedelta(days=1), update=False)
    assert acct.positions()[0]["qty"] == pytest.approx(20)                    # 1% of 10k / (100 - 95)
