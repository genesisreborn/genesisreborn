"""Portfolio tracker, market overview and the daily brief."""
import time

import pytest
from fastapi.testclient import TestClient

from stockpulse.config import Settings
from stockpulse.portfolio import PortfolioError, replay

DAY = 86400


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "p.db")
    s.reports_dir = str(tmp_path / "rep")
    s.demo_mode = True
    s.anthropic_api_key = s.telegram_bot_token = s.massive_api_key = s.report_api_token = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def tx(i, ts, side, qty, price, fee=0, sym="X"):
    return {"id": i, "ts": ts, "symbol": sym, "side": side, "qty": qty, "price": price, "fee": fee}


def test_average_cost_bookkeeping():
    book = replay([tx(1, 1, "BUY", 10, 100, fee=5), tx(2, 2, "BUY", 10, 120), tx(3, 3, "SELL", 5, 130, fee=1),
                   tx(4, 4, "DIV", 0, 12)])["X"]
    avg = (1000 + 5 + 1200) / 20
    assert book["qty"] == 15
    assert book["cost"] == pytest.approx(avg * 15)
    assert book["realized"] == pytest.approx(5 * (130 - avg) - 1)
    assert book["dividends"] == 12
    # selling everything resets cost; a later buy starts a fresh average
    book = replay([tx(1, 1, "BUY", 2, 10), tx(2, 2, "SELL", 2, 15), tx(3, 3, "BUY", 1, 50)])["X"]
    assert book["qty"] == 1 and book["cost"] == 50 and book["realized"] == 10


def test_oversell_rejected_in_time_order():
    with pytest.raises(PortfolioError):
        replay([tx(1, 10, "BUY", 5, 1), tx(2, 5, "SELL", 5, 1)])   # sell dated before the buy


def test_portfolio_api_flow(tmp_path):
    from stockpulse.web.app import create_app

    now = int(time.time())
    with TestClient(create_app(settings(tmp_path), start_scheduler=False)) as c:
        r = c.post("/api/portfolio/tx", json={"symbol": "aapl", "side": "BUY", "qty": 10, "price": 100, "ts": now - 100 * DAY,
                                              "note": "突破买入", "tags": "突破"})
        assert r.status_code == 200 and r.json()["symbol"] == "AAPL"
        assert c.post("/api/portfolio/tx", json={"symbol": "AAPL", "side": "SELL", "qty": 11, "price": 100}).status_code == 400
        assert c.post("/api/portfolio/tx", json={"symbol": "AAPL", "side": "BUY", "qty": -1, "price": 100}).status_code == 400
        assert c.post("/api/portfolio/tx", json={"symbol": "AAPL", "side": "HOLD", "qty": 1, "price": 1}).status_code == 400
        assert c.post("/api/portfolio/tx", json={"symbol": "AAPL", "side": "BUY", "qty": "x", "price": 1}).status_code == 400
        sell = c.post("/api/portfolio/tx", json={"symbol": "AAPL", "side": "SELL", "qty": 4, "price": 110, "ts": now - 10 * DAY}).json()
        pf = c.get("/api/portfolio").json()
        pos = pf["positions"][0]
        assert pos["symbol"] == "AAPL" and pos["qty"] == 6 and pos["avg_cost"] == 100
        assert pf["totals"]["realized"] == pytest.approx(40)
        assert pos["weight_pct"] == pytest.approx(100)
        pts = pf["history"]["points"]
        assert len(pts) > 50 and pts[-1]["cost"] == pytest.approx(600) and pts[-1]["benchmark"] > 0
        # deleting the buy would leave the sell uncovered
        buy_id = [t for t in pf["transactions"] if t["side"] == "BUY"][0]["id"]
        assert c.delete(f"/api/portfolio/tx/{buy_id}").status_code == 400
        assert c.put(f"/api/portfolio/tx/{sell['id']}", json={"note": "止盈一部分", "tags": "复盘"}).status_code == 200
        assert c.get("/api/portfolio/tx", params={"symbol": "AAPL"}).json()[0]["note"] == "止盈一部分"
        assert c.delete(f"/api/portfolio/tx/{sell['id']}").status_code == 200


def test_portfolio_needs_token_when_set(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path, report_api_token="t"), start_scheduler=False)) as c:
        assert c.get("/api/portfolio").status_code == 401
        assert c.get("/api/brief").status_code == 401
        assert c.get("/api/portfolio", headers={"Authorization": "Bearer t"}).status_code == 200


def test_market_overview_and_page(tmp_path):
    from stockpulse.web.app import create_app

    with TestClient(create_app(settings(tmp_path), start_scheduler=False)) as c:
        m = c.get("/api/market").json()
        assert len(m["sectors"]) == 11 and m["indices"][0]["symbol"] == "SPY"
        b = m["breadth"]
        assert b["count"] >= 1 and b["advancers"] + b["decliners"] <= b["count"]
        assert 0 <= b["above_sma50_pct"] <= 100
        assert m["gainers"] == sorted(m["gainers"], key=lambda r: -r["change_pct"])
        assert c.get("/market").status_code == 200


def test_brief_and_telegram_commands(tmp_path):
    from stockpulse.web.app import create_app

    app = create_app(settings(tmp_path, telegram_chat_ids=["1"]), start_scheduler=False)
    eng = app.state.engine
    eng.portfolio.add("NVDA", "BUY", 3, 80, int(time.time()) - 5 * DAY)
    text = eng.brief()["text"]
    assert "盘前简报" in text and "大盘" in text and "我的持仓" in text and "NVDA" in text
    bot = eng.notifier
    assert "盘前简报" in bot.handle_command("1", "/brief")
    assert "NVDA" in bot.handle_command("1", "/portfolio")
    assert "只有" in bot.handle_command("99", "/portfolio")     # subscribers never see holdings
