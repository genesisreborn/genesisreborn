"""New indicators, multi-instance indicator series, drawings storage and the screener."""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stockpulse import indicators as ind
from stockpulse.config import Settings
from stockpulse.market_data import demo_bars
from stockpulse.strategy.dsl import validate


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "c.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = s.telegram_bot_token = s.massive_api_key = s.report_api_token = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_indicator_values_sane():
    df = demo_bars("X", "1d", 400)
    d = ind.dmi(df)
    assert d["adx"].dropna().between(0, 100).all()
    assert ind.mfi(df).dropna().between(0, 100).all()
    assert ind.willr(df).dropna().between(-100, 0).all()
    assert ind.stoch_rsi(df["close"])["k"].dropna().between(0, 100).all()
    kc = ind.keltner(df).dropna()
    assert (kc["upper"] > kc["mid"]).all() and (kc["mid"] > kc["lower"]).all()
    sar = ind.psar(df).dropna()
    assert len(sar) > 300 and (sar > 0).all()
    s = pd.Series(np.arange(1, 30, dtype=float))
    assert ind.roc(s, 1).iloc[-1] == pytest.approx((29 / 28 - 1) * 100)
    # no look-ahead
    pd.testing.assert_series_equal(ind.cci(df).iloc[:300], ind.cci(df.iloc[:300]))


def test_new_dsl_functions_validate():
    validate("adx(14) > 25 and plus_di(14) > minus_di(14) and cci(20) < 100 and willr(14) > -80")
    validate("mfi(14) < 20 or roc(close, 12) > 5 or close > psar() or close > kc_upper(20, 2)")


def test_chart_api_new_indicators_and_instances(tmp_path):
    from stockpulse.web.app import create_app

    app = create_app(settings(tmp_path), start_scheduler=False)
    with TestClient(app) as c:
        specs = "a=ema:20,b=ema:50,c=adx:14,d=ichimoku,e=psar,f=volsma:20,g=stochrsi,keltner"
        body = c.get("/api/chart", params={"symbol": "AAPL", "interval": "1d", "indicators": specs}).json()
        ids = [s["id"] for s in body["series"]]
        assert "a|ema:20" in ids and "b|ema:50" in ids and "keltner:upper" in ids
        adx = [s for s in body["series"] if s.get("uid") == "c"]
        assert len(adx) == 3 and all(s["pane"] == "c|adx:14" for s in adx)
        assert {s["pane"] for s in body["series"] if s.get("uid") in ("a", "d", "e")} == {"price"}
        assert [s for s in body["series"] if s.get("uid") == "f"][0]["pane"] == "volume"
        assert [s for s in body["series"] if s.get("uid") == "e"][0]["kind"] == "dots"


def test_drawings_roundtrip_and_auth(tmp_path):
    from stockpulse.web.app import create_app

    app = create_app(settings(tmp_path, report_api_token="t"), start_scheduler=False)
    d = [{"id": "x1", "type": "trend", "points": [{"time": 1, "price": 10}, {"time": 2, "price": 11}], "color": "#f00"}]
    with TestClient(app) as c:
        assert c.get("/api/drawings", params={"symbol": "aapl"}).json() == []
        assert c.put("/api/drawings", params={"symbol": "aapl"}, json=d).status_code == 401
        assert c.put("/api/drawings", params={"symbol": "aapl"}, json=d, headers={"Authorization": "Bearer t"}).json()["saved"] == 1
        assert c.get("/api/drawings", params={"symbol": "AAPL"}).json() == d
        assert c.put("/api/drawings", params={"symbol": "X"}, json=[{}] * 501, headers={"Authorization": "Bearer t"}).status_code == 400


def test_screener(tmp_path):
    from stockpulse.web.app import create_app

    app = create_app(settings(tmp_path), start_scheduler=False)
    with TestClient(app) as c:
        out = c.post("/api/screener", json={"symbols": ["AAPL", "MSFT", "NVDA"]}).json()
        assert out["total"] == 3 and len(out["rows"]) == 3
        row = out["rows"][0]
        for k in ("price", "rsi14", "perf_1m", "vs_sma200", "from_high_52w", "rel_volume"):
            assert k in row and k in out["columns"]
        assert row["from_high_52w"] <= 0 <= row["from_low_52w"]
        flt = c.post("/api/screener", json={"symbols": ["AAPL", "MSFT", "NVDA"], "filter": "close < 0"}).json()
        assert flt["rows"] == [] and flt["total"] == 3
        assert c.post("/api/screener", json={"symbols": ["AAPL"], "filter": "close >"}).status_code == 400
        assert c.post("/api/screener", json={}).json()["total"] >= 5        # watchlist by default
