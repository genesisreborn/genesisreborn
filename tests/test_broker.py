"""Alpaca execution behind the risk gate, against a mocked Alpaca API (no network)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from stockpulse.broker import LIVE_CONFIRM_PHRASE, AlpacaClient, BrokerExecutor
from stockpulse.config import Settings
from stockpulse.db import Database


class FakeAlpaca:
    def __init__(self, equity=100_000, positions=None):
        self.equity = equity
        self.positions = positions or []
        self.calls = []

    def handler(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body, dict(req.headers)))
        path = req.url.path
        if path == "/v2/account":
            return httpx.Response(200, json={"equity": str(self.equity), "cash": "50000", "buying_power": "100000", "status": "ACTIVE"})
        if path == "/v2/positions" and req.method == "GET":
            return httpx.Response(200, json=self.positions)
        if path == "/v2/orders" and req.method == "POST":
            if any(c[2] and c[2].get("client_order_id") == body["client_order_id"] for c in self.calls[:-1]):
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            return httpx.Response(200, json={"id": "ord-1", **body})
        if path.startswith("/v2/positions/") and req.method == "DELETE":
            return httpx.Response(200, json={"id": "close-1"})
        if path == "/v2/orders" and req.method == "DELETE":
            return httpx.Response(207, json=[])
        return httpx.Response(404, json={"message": "not found"})

    def posted_orders(self):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1] == "/v2/orders"]


def make(tmp_path, fake, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "b.db")
    s.broker, s.alpaca_key_id, s.alpaca_secret_key = "alpaca", "KEY", "SECRET"
    s.alpaca_paper = True
    s.report_api_token = "tok"
    s.max_order_notional, s.max_daily_orders, s.max_open_positions, s.allow_short = 1000, 2, 5, False
    s.live_trading_confirm = ""
    for k, v in kw.items():
        setattr(s, k, v)
    client = AlpacaClient(s, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return BrokerExecutor(Database(s.db_path), s, client=client), s


def sig(action="BUY", sid=1, symbol="AAPL", price=200.0, **kw):
    return {"id": sid, "strategy_id": 9, "strategy": "s", "symbol": symbol, "interval": "1d", "action": action,
            "reason": "signal", "bar_ts": 1, "price": price, **kw}


def test_buy_is_capped_and_carries_stop(tmp_path):
    fake = FakeAlpaca()
    ex, _ = make(tmp_path, fake)
    rec = ex.execute(sig(stop=190.0), ["AAPL"], alloc_pct=10)   # 10% of 100k = 10k, capped at 1000
    assert rec["status"] == "submitted" and rec["qty"] == 5     # floor(1000 / 200)
    order = fake.posted_orders()[0]
    assert order == {"symbol": "AAPL", "qty": "5", "side": "buy", "type": "market", "time_in_force": "day",
                     "client_order_id": "sp-1-buy", "order_class": "oto", "stop_loss": {"stop_price": "190.00"}}
    headers = fake.calls[0][3]
    assert headers["apca-api-key-id"] == "KEY" and "paper-api" in str(ex.client.base)


def test_risk_gate_rejections(tmp_path):
    fake = FakeAlpaca()
    ex, s = make(tmp_path, fake)
    assert ex.execute(sig(symbol="TSLA"), ["AAPL"], 10)["detail"] == "代码不在策略的监控列表里"
    assert "做空" in ex.execute(sig("SHORT"), ["AAPL"], 10)["detail"]
    assert "不够买 1 股" in ex.execute(sig(price=5000), ["AAPL"], 10)["detail"]
    assert ex.execute(sig("SELL"), ["AAPL"], 10)["status"] == "skipped"          # nothing to sell
    assert ex.execute({**sig(), "reason": "stop"}, ["AAPL"], 10)["status"] == "skipped"
    # daily cap
    ex.execute(sig(sid=10), ["AAPL"], 10)
    ex.execute(sig(sid=11, symbol="MSFT"), ["AAPL", "MSFT"], 10)
    assert "每日下单上限" in ex.execute(sig(sid=12, symbol="NVDA"), ["NVDA"], 10)["detail"]
    assert len(fake.posted_orders()) == 2


def test_live_mode_needs_confirmation_token_and_respects_pause(tmp_path):
    fake = FakeAlpaca()
    ex, s = make(tmp_path, fake, alpaca_paper=False)
    assert "实盘模式未解锁" in ex.execute(sig(), ["AAPL"], 10)["detail"]
    s.live_trading_confirm = LIVE_CONFIRM_PHRASE
    assert ex.client.base == "https://api.alpaca.markets"
    ex.set_paused(True)
    assert ex.execute(sig(), ["AAPL"], 10)["detail"] == "自动交易已暂停"
    ex.set_paused(False)
    s.report_api_token = ""
    assert "API_TOKEN" in ex.execute(sig(), ["AAPL"], 10)["detail"]
    assert fake.posted_orders() == []


def test_exit_closes_position_and_duplicates_are_ignored(tmp_path):
    fake = FakeAlpaca(positions=[{"symbol": "AAPL", "qty": "5", "side": "long"}])
    ex, _ = make(tmp_path, fake)
    rec = ex.execute(sig("SELL", sid=3), ["AAPL"], 10)
    assert rec["status"] == "submitted" and rec["qty"] == 5
    assert ("DELETE", "/v2/positions/AAPL") in [(c[0], c[1]) for c in fake.calls]
    assert ex.execute(sig("BUY", sid=4), ["AAPL"], 10)["detail"] == "已持有该代码"

    fake.positions = []
    assert ex.execute(sig(sid=5), ["AAPL"], 10)["status"] == "submitted"
    assert ex.execute(sig(sid=5), ["AAPL"], 10)["detail"] == "重复提交，已忽略"


def test_position_cap_and_flatten(tmp_path):
    fake = FakeAlpaca(positions=[{"symbol": s, "qty": "1"} for s in ("A", "B")])
    ex, s = make(tmp_path, fake, max_open_positions=2)
    assert "持仓数已达上限" in ex.execute(sig(), ["AAPL"], 10)["detail"]
    out = ex.flatten_all()
    assert out["ok"] and out["closed"] == ["A", "B"] and ex.paused


def test_not_configured_never_calls_network(tmp_path):
    fake = FakeAlpaca()
    ex, s = make(tmp_path, fake, broker="")
    assert "未配置券商" in ex.execute(sig(), ["AAPL"], 10)["detail"]
    assert fake.calls == []
    assert ex.status()["configured"] is False


def test_engine_routes_broker_signals_and_api(tmp_path):
    from stockpulse.scheduler import Engine
    from stockpulse.strategy import StrategyConfig
    from stockpulse.web.app import create_app

    fake = FakeAlpaca()
    ex, s = make(tmp_path, fake)
    s.demo_mode = True
    s.telegram_bot_token = s.anthropic_api_key = s.massive_api_key = ""
    s.reports_dir = str(tmp_path / "rep")
    app = create_app(s, start_scheduler=False)
    eng: Engine = app.state.engine
    eng.broker = BrokerExecutor(eng.db, s, client=ex.client)
    cfg = StrategyConfig(name="b", long_entry="close > 0", interval="1d", symbols=["AAPL"], enabled=True,
                         execution="broker", alloc_pct=5)
    assert cfg.validate() == []
    assert StrategyConfig(name="b", long_entry="close > 0", execution="broker").validate()  # needs symbols
    sid = eng.db.save_strategy(cfg.to_dict())
    eng.signals.run = lambda: [{**sig(sid=77), "strategy_id": sid, "execution": "broker", "alloc_pct": 5, "symbols": ["AAPL"]}]
    eng.run_signals()
    assert fake.posted_orders()[0]["client_order_id"] == "sp-77-buy"

    auth = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        st = c.get("/api/broker").json()
        assert st["configured"] and st["mode"] == "paper" and st["log"][0]["status"] == "submitted"
        assert st["account"]["equity"] == "100000"
        assert c.post("/api/broker/pause", json={"paused": True}).status_code == 401
        assert c.post("/api/broker/pause", json={"paused": True}, headers=auth).json()["paused"] is True
        assert c.post("/api/broker/flatten", json={}, headers=auth).status_code == 400
        assert c.post("/api/broker/flatten", json={"confirm": "FLATTEN"}, headers=auth).json()["ok"] is True
