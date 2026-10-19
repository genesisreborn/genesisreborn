"""Fundamentals payload shaping, caching and the API endpoint."""
import pandas as pd
from fastapi.testclient import TestClient

from stockpulse.config import Settings
from stockpulse.db import Database
from stockpulse.fundamentals import build_payload, demo_fundamentals, get_fundamentals


def settings(tmp_path):
    s = Settings()
    s.db_path = str(tmp_path / "f.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = s.telegram_bot_token = s.massive_api_key = s.report_api_token = ""
    return s


def test_build_payload_from_yahoo_shapes():
    cols = [pd.Timestamp("2026-06-30"), pd.Timestamp("2026-03-31"), pd.Timestamp("2025-12-31")]  # newest first, like yfinance
    q = pd.DataFrame({c: {"Total Revenue": r, "Net Income": r / 10, "Diluted EPS": 1.0, "Gross Profit": float("nan")}
                      for c, r in zip(cols, [300.0, 200.0, 100.0])})
    info = {"longName": "Acme", "marketCap": 5e9, "trailingPE": float("nan"), "dividendYield": 0.42,
            "currentPrice": 10, "targetMeanPrice": 12, "recommendationKey": "buy"}
    p = build_payload("acme", info, q, None, [])
    assert p["symbol"] == "ACME" and p["profile"]["name"] == "Acme"
    st = {s["key"]: s for s in p["stats"]}
    assert "pe" not in st                              # NaN dropped
    assert st["dividend_yield"]["value"] == 0.42       # fractions kept as is
    assert st["recommendation"]["value"] == "买入"
    assert abs(st["target_upside"]["value"] - 0.2) < 1e-9
    qs = p["quarterly"]
    assert qs["periods"] == ["2025-12-31", "2026-03-31", "2026-06-30"]   # oldest -> newest
    rows = {r["key"]: r["values"] for r in qs["rows"]}
    assert rows["revenue"] == [100.0, 200.0, 300.0]
    assert rows["gross_profit"] == [None, None, None]
    assert rows["net_margin"] == [10.0, 10.0, 10.0]
    assert p["annual"] == {"periods": [], "rows": []}


def test_dividend_yield_percent_normalised():
    p = build_payload("X", {"dividendYield": 2.5}, None, None, [])
    assert p["stats"][0]["value"] == 0.025


def test_demo_and_cache(tmp_path):
    s = settings(tmp_path)
    db = Database(s.db_path)
    a = get_fundamentals(db, s, "nvda")
    assert a["symbol"] == "NVDA" and len(a["quarterly"]["periods"]) == 8
    assert any(e["type"] == "earnings" and e["upcoming"] for e in a["events"])
    assert db.get_kv("fund:NVDA")
    assert get_fundamentals(db, s, "NVDA") == a        # served from cache
    assert demo_fundamentals("NVDA")["stats"] == a["stats"]


def test_fundamentals_endpoint(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path), start_scheduler=False)) as c:
        r = c.get("/api/fundamentals", params={"symbol": "aapl"})
        assert r.status_code == 200
        d = r.json()
        assert d["symbol"] == "AAPL" and d["stats"] and d["news"] == []
        assert c.get("/api/fundamentals", params={"symbol": ""}).status_code == 400
        assert c.get("/multi").status_code == 200
