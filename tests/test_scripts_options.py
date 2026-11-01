"""Indicator scripts, option chains and the calendar."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from stockpulse.config import Settings
from stockpulse.market_data import demo_bars
from stockpulse.options import summarize
from stockpulse.strategy.dsl import DSLError
from stockpulse.strategy.script import EXAMPLES, parse_script, run_script, script_refs, validate_script


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "s.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = s.telegram_bot_token = s.massive_api_key = s.report_api_token = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_script_runs_with_inputs_and_shapes():
    df = demo_bars("X", "1d", 300)
    src = """length = 20   # input
    basis = sma(close, length)
    // comment line
    plot(basis, "mid", "#ff0000")
    plot(close - basis, "diff", "#00ff00", "histogram")
    hline(0, "zero")
    plotshape(crossover(close, basis), "x up", "up")
    """
    src = "\n".join(line.strip() for line in src.splitlines())
    r = run_script(src, df)
    assert r["inputs"] == {"length": 20}
    assert [p["title"] for p in r["plots"]] == ["mid", "diff"] and r["plots"][1]["kind"] == "histogram"
    assert r["hlines"] == [{"value": 0.0, "title": "zero", "color": None}]
    assert r["shapes"][0]["times"]
    r30 = run_script(src, df, {"length": 30, "bogus": 5})
    assert r30["inputs"] == {"length": 30.0}
    assert len(r30["plots"][0]["data"]) == len(df) - 29


def test_script_rejects_unsafe_and_bad_statements():
    for bad in ["import os", "x = 1\nprint(x)", "a, b = 1, 2", "x = close.__class__\nplot(x)", "for i in range(3): pass",
                "plot(close, title='x')", "x = 1", "plot(foo)", "close = 1\nplot(close)", "plot(close, 'a', 'red')"]:
        with pytest.raises(DSLError):
            validate_script(bad)
    with pytest.raises(DSLError, match="第 2 行"):
        validate_script("a = sma(close, 5)\nplot(nope(a))")


def test_examples_valid_and_refs():
    for src in EXAMPLES.values():
        validate_script(src)
    assert script_refs(EXAMPLES["相对强度 vs SPY (副图)"]) == {"SPY"}
    assert parse_script("n = -3\nplot(close)").inputs == {"n": -3}


def test_scripts_api_and_chart(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path), start_scheduler=False)) as c:
        assert c.post("/api/scripts/validate", json={"source": "plot(close"}).json()["ok"] is False
        bad = c.post("/api/scripts", json={"name": "x", "source": "x = 1"})
        assert bad.status_code == 400
        item = c.post("/api/scripts", json={"name": "BB", "overlay": True, "source": EXAMPLES["布林带 + 突破标记"]}).json()
        sub = c.post("/api/scripts", json={"name": "RS", "source": EXAMPLES["相对强度 vs SPY (副图)"]}).json()
        listed = c.get("/api/scripts").json()
        assert {s["id"] for s in listed["scripts"]} == {item["id"], sub["id"]} and listed["examples"]
        specs = f"a=script:{item['id']}:length=30;mult=2,b=script:{sub['id']},c=script:missing"
        body = c.get("/api/chart", params={"symbol": "AAPL", "indicators": specs}).json()
        a = [s for s in body["series"] if s["uid"] == "a"]
        assert {s["kind"] for s in a} == {"line", "shape"} and all(s["pane"] == "price" for s in a)
        b = [s for s in body["series"] if s["uid"] == "b"]
        assert b and b[0]["pane"] != "price" and b[0]["levels"] == [0.0]
        assert len(body["script_errors"]) == 1
        assert c.delete(f"/api/scripts/{item['id']}").status_code == 200
        assert len(c.get("/api/scripts").json()["scripts"]) == 1


def test_scripts_save_requires_token(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path, report_api_token="t"), start_scheduler=False)) as c:
        assert c.post("/api/scripts", json={"source": "plot(close)"}).status_code == 401
        assert c.post("/api/scripts", json={"source": "plot(close)"}, headers={"Authorization": "Bearer t"}).status_code == 200


def test_option_summary_max_pain():
    calls = [{"strike": k, "volume": 10, "oi": oi, "iv": 0.3} for k, oi in [(90, 100), (100, 500), (110, 1000)]]
    puts = [{"strike": k, "volume": 20, "oi": oi, "iv": 0.4} for k, oi in [(90, 1000), (100, 500), (110, 100)]]
    sm = summarize(calls, puts, 101)
    assert sm["max_pain"] == 100 and sm["pc_volume"] == 2.0 and sm["pc_oi"] == 1.0
    assert sm["atm_iv"] == pytest.approx(0.35)


def test_options_and_calendar_api(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path), start_scheduler=False)) as c:
        d = c.get("/api/options", params={"symbol": "nvda"}).json()
        assert d["symbol"] == "NVDA" and d["calls"] and d["puts"] and d["expiry"] == d["expirations"][0]
        assert any(r["itm"] for r in d["calls"]) and any(not r["itm"] for r in d["calls"])
        d2 = c.get("/api/options", params={"symbol": "NVDA", "expiry": d["expirations"][3]}).json()
        assert d2["expiry"] == d["expirations"][3]
        cal = c.get("/api/calendar", params={"start": date.today().isoformat(), "days": 7}).json()
        assert cal["economic"] and cal["earnings"] and any(e["watched"] for e in cal["earnings"])
        assert c.get("/api/calendar", params={"start": "bad"}).status_code == 400
        f = c.get("/api/fundamentals", params={"symbol": "AAPL"}).json()
        assert f["balance"]["quarterly"]["rows"] and f["cashflow"]["annual"]["rows"]
