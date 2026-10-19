"""Regression tests for issues found in the code review."""
import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stockpulse import indicators as ind
from stockpulse.config import Settings
from stockpulse.market_data import demo_bars
from stockpulse.strategy.dsl import DSLError, Evaluator, validate
from stockpulse.strategy.script import run_script
from stockpulse.telegram_bot import MAX_LEN, _chunks


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "r.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = s.telegram_bot_token = s.massive_api_key = s.report_api_token = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.mark.parametrize("expr", ["close[1e400] > 0", "sma(close, 1e400) > 0", "prev(close, 1e19) > 0",
                                  "wma(close, 1e10) > 0", "sma(close, -5) > 0", "-" * 1990 + "close > 0",
                                  "round(close, 1e10) > 0"])
def test_dsl_numeric_abuse_is_dsl_error(expr):
    with pytest.raises(DSLError):
        validate(expr)


def test_wma_longer_than_data_is_nan_without_allocating():
    s = pd.Series(np.arange(10, dtype=float))
    assert ind.wma(s, 50).isna().all()


def test_mfi_strict_uptrend_is_100():
    idx = pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC")
    c = np.arange(1, 41, dtype=float)
    df = pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1e6}, index=idx)
    assert ind.mfi(df).dropna().eq(100).all() and ind.mfi(df).notna().sum() > 20


def test_script_input_override_only_on_declaring_line():
    df = demo_bars("X", "1d", 50)
    src = "length = 20\nlength = length * 2\nhline(length, 'x')"
    assert run_script(src, df)["hlines"][0]["value"] == 40
    assert run_script(src, df, {"length": 10})["hlines"][0]["value"] == 20
    assert run_script(src, df, {"length": float("inf")})["hlines"][0]["value"] == 40
    with pytest.raises(DSLError):
        run_script("hline(1e400)\nplot(close)", df)


def test_chunks_never_empty_or_oversized():
    for text in ["x" * 4100, "a\n" + "y" * 9000 + "\nb", ("line\n" * 2000)]:
        parts = _chunks(text)
        assert all(0 < len(p) <= MAX_LEN + 1 for p in parts)
        assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_trading_control_needs_admin_in_groups(tmp_path):
    from stockpulse.db import Database
    from stockpulse.telegram_bot import TelegramNotifier

    s = settings(tmp_path, telegram_chat_ids=["100", "-200"], telegram_admin_ids=["7"])
    bot = TelegramNotifier(s, Database(s.db_path))
    assert "暂停" in bot.handle_command("100", "/pause", "100", "private")
    assert "只有" in bot.handle_command("-200", "/resume", "8", "group")
    assert "恢复" in bot.handle_command("-200", "/resume", "7", "group")
    assert "只有" in bot.handle_command("555", "/pause", "555", "private")


def test_api_bad_inputs_are_400_not_500(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path), start_scheduler=False), raise_server_exceptions=False) as c:
        for spec in ["ema:0", "ema:-5", "rsi:0", "macd:0:0:0", "ema:nan", "ema:inf", "rsi:1e308", "bb:20:inf", "supertrend:0"]:
            r = c.get("/api/chart", params={"symbol": "AAPL", "indicators": spec})
            assert r.status_code in (200, 400), (spec, r.status_code, r.text[:200])
        assert c.get("/api/chart", params={"symbol": "../../etc/passwd"}).status_code == 400
        for path, body in [("/api/backtest", {"strategy_id": "abc"}), ("/api/backtest", {"strategy": "str"}),
                           ("/api/optimize", {"grid": "abc"}), ("/api/screener", {"symbols": [1]}),
                           ("/api/screener", {"filter": 5}), ("/api/strategies", {"name": "x", "symbols": [1, None], "long_entry": "close > 0"}),
                           ("/api/paper/reset", {"capital": -100}), ("/api/paper/reset", {"capital": [1]}),
                           ("/api/scan", {"limit": "abc"})]:
            r = c.post(path, json=body)
            assert r.status_code < 500, (path, body, r.status_code, r.text[:200])
        r = c.post("/api/strategies", content=b'{"name":"n","long_entry":"close > 0","stop_loss_pct":NaN}',
                   headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert c.get("/api/strategies").status_code == 200
        assert c.put("/api/drawings?symbol=AAPL", content=b'[{"p": NaN}]', headers={"Content-Type": "application/json"}).status_code == 400
        assert c.get("/api/calendar", params={"start": "9999-12-31"}).status_code == 400
        # open mode (no token) must not allow emptying a real account
        assert c.post("/api/broker/flatten", json={"confirm": "FLATTEN"}).status_code == 400


def test_chart_signals_filtered_by_interval_in_sql(tmp_path):
    from stockpulse.web.app import create_app

    app = create_app(settings(tmp_path), start_scheduler=False)
    db = app.state.engine.db
    db.add_signal({"strategy_id": 1, "strategy": "s", "symbol": "AAPL", "interval": "1d", "action": "BUY", "reason": "signal",
                   "bar_ts": 1, "price": 1.0})
    for i in range(250):
        db.add_signal({"strategy_id": 1, "strategy": "s", "symbol": "AAPL", "interval": "5m", "action": "BUY",
                       "reason": "signal", "bar_ts": 1000 + i, "price": 1.0})
    with TestClient(app) as c:
        body = c.get("/api/chart", params={"symbol": "AAPL", "interval": "1d"}).json()
        assert len(body["signals"]) == 1


def test_refresh_and_telegram_test_need_token(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path, report_api_token="t"), start_scheduler=False)) as c:
        assert c.post("/api/refresh").status_code == 401
        assert c.post("/api/telegram/test").status_code == 401


def test_alert_with_ref_symbol_fires(tmp_path):
    from stockpulse.db import Database
    from stockpulse.market_data import BarStore
    from stockpulse.strategy.alerts import AlertEngine

    s = settings(tmp_path)
    db = Database(s.db_path)
    store = BarStore(db, s)
    db.save_alert({"symbol": "AAPL", "interval": "1d", "condition": 'close > 0 and ref_close("SPY") > 0', "mode": "every",
                   "active": True, "realtime": False, "kind": "rule"})
    fired = AlertEngine(db, store).run(update=True)
    assert len(fired) == 1


def test_weekly_bar_closed_for_sunday_and_monday_stamps():
    from datetime import datetime, timezone

    from stockpulse.market_data import closed_bars

    tuesday = datetime(2026, 9, 22, 15, tzinfo=timezone.utc)
    for stamps in (["2026-09-13 04:00", "2026-09-20 04:00"], ["2026-09-14 04:00", "2026-09-21 04:00"]):   # Sunday / Monday
        idx = pd.DatetimeIndex(stamps, tz="UTC")
        df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
        assert len(closed_bars(df, "1wk", now=tuesday)) == 1


def test_stale_cache_triggers_full_fetch(tmp_path, monkeypatch):
    import time

    from stockpulse import market_data as md
    from stockpulse.db import Database

    s = settings(tmp_path, demo_mode=False)
    store = md.BarStore(Database(s.db_path), s)
    old = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                       index=pd.DatetimeIndex([pd.Timestamp(time.time() - 10 * 86400, unit="s", tz="UTC")]))
    store.save("AAPL", "5m", old)
    calls = []
    monkeypatch.setattr(md, "fetch_yfinance", lambda sym, iv, inc: calls.append(inc) or old.iloc[:0])
    store.update("AAPL", "5m")
    assert calls == [False]


def test_ticker_extraction_ambiguous_parens_and_crypto():
    from stockpulse.sources.social import parse_stocktwits
    from stockpulse.tickers import extract_tickers

    assert "AI" not in extract_tickers("Nvidia rallies on artificial intelligence (AI) demand")
    assert "AI" in extract_tickers("C3.ai (NYSE: AI) jumps")
    assert parse_stocktwits({"symbols": [{"symbol": "BTC.X"}, {"symbol": "TSLA"}]})[0].tickers == {"TSLA"}


def test_single_ticker_download_multiindex():
    from stockpulse.market import quotes_from_frame

    idx = pd.date_range("2026-01-01", periods=30, freq="D")
    cols = pd.MultiIndex.from_product([["AAPL"], ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(np.tile([1.0, 2.0, 0.5, 1.5, 1e6], (30, 1)), index=idx, columns=cols)
    assert "AAPL" in quotes_from_frame(["AAPL"], df)


def test_massive_retries_rate_limit(tmp_path, monkeypatch):
    import httpx

    from stockpulse import market_data as md

    monkeypatch.setattr(md.time, "sleep", lambda s: None)
    hits = []

    def handler(req):
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, json={"status": "ERROR"})
        return httpx.Response(200, json={"results": [{"t": 1_700_000_000_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})

    s = settings(tmp_path, massive_api_key="k")
    df = md.fetch_massive("AAPL", "1d", True, s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(df) == 1 and len(hits) == 2
