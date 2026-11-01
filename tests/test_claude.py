"""Exercise the Claude report path against a local fake Messages endpoint."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stockpulse.config import Settings
from stockpulse.insights import build_insights

ANSWER = {
    "headline": "AI 算力领涨",
    "top_picks": ["NVDA"],
    "outlook": "关注周四 CPI。",
    "risk_warning": "估值偏高。",
    "market_overview": "AI 算力继续领涨。",
    "themes": ["AI 算力"],
    "stocks": [{"ticker": "NVDA", "view": "bullish", "confidence": 80, "catalyst": "财报超预期",
                "risks": "估值偏高", "summary": "趋势向上。"}],
}


@pytest.fixture
def fake_api(monkeypatch):
    captured = {"calls": 0}
    reply = {"stop_reason": "end_turn", "text": json.dumps(ANSWER, ensure_ascii=False)}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured["body"] = json.loads(body)
            captured["calls"] += 1
            captured["beta"] = self.headers.get("anthropic-beta")
            msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                   "content": [{"type": "text", "text": reply["text"]}], "stop_reason": reply["stop_reason"],
                   "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
            data = json.dumps(msg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield captured, reply
    srv.shutdown()


def _settings(tmp_path=None):
    s = Settings()
    s.anthropic_api_key = "test"
    s.demo_mode = False
    s.telegram_bot_token = ""
    s.watchlist = ["NVDA"]
    if tmp_path is not None:
        s.db_path = str(tmp_path / "r.db")
        s.reports_dir = str(tmp_path / "reports")
    return s


@pytest.fixture
def offline(monkeypatch):
    """Use demo news/quotes but keep Claude 'enabled' (pointed at the fake API)."""
    from stockpulse import market, pipeline
    from stockpulse.sources.demo import demo_items

    monkeypatch.setattr(pipeline, "gather", lambda settings: demo_items(seed=1))
    monkeypatch.setattr(market, "fetch_quotes", market.demo_quotes)


RANK = [{"ticker": "NVDA", "score": 80, "mentions": 10, "news": 5, "social": 5, "velocity": 3.0,
         "sentiment": 0.5, "signal": "看涨", "headlines": [{"title": "Nvidia soars"}]}]


def test_claude_insights_request_and_parse(fake_api):
    captured, _ = fake_api
    out = build_insights(RANK, _settings(), use_claude=True)
    assert out["engine"] == "claude" and out["stocks"][0]["ticker"] == "NVDA"
    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "server-side-fallback-2026-07-01" in captured["beta"]


def test_claude_refusal_falls_back_to_rules(fake_api):
    _, reply = fake_api
    reply["stop_reason"] = "refusal"
    out = build_insights(RANK, _settings(), use_claude=True)
    assert out["engine"] == "rules"


def test_refresh_never_calls_claude_but_report_calls_once(fake_api, offline, tmp_path):
    from stockpulse.db import Database
    from stockpulse.pipeline import run_cycle
    from stockpulse.report import ReportCooldown, generate_report

    captured, _ = fake_api
    s = _settings(tmp_path)
    db = Database(s.db_path)
    for _ in range(3):
        snap = run_cycle(db, s)
    assert captured["calls"] == 0 and snap["insight_engine"] == "rules"

    rep = generate_report(db, s)
    assert captured["calls"] == 1
    assert rep["insight_engine"] == "claude" and rep["insights"]["headline"] == "AI 算力领涨"
    assert db.get_report()["report_id"] == rep["report_id"]
    md = (tmp_path / "reports").glob("report_*.md")
    assert "AI 算力领涨" in next(md).read_text(encoding="utf-8")

    with pytest.raises(ReportCooldown):          # manual re-trigger inside the cooldown
        generate_report(db, s)
    assert captured["calls"] == 1
    generate_report(db, s, force=True)           # scheduled reports bypass the cooldown
    assert captured["calls"] == 2


def test_report_api_token_and_cooldown(fake_api, offline, tmp_path):
    from fastapi.testclient import TestClient

    from stockpulse.web.app import create_app

    s = _settings(tmp_path)
    s.report_api_token = "secret"
    app = create_app(s, start_scheduler=False)
    with TestClient(app) as c:
        assert c.post("/api/report").status_code == 401
        r = c.post("/api/report", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200 and r.json()["insight_engine"] == "claude"
        rid = r.json()["report_id"]
        assert c.post("/api/report?token=secret").status_code == 429
        assert c.get("/api/report/latest").json()["report_id"] == rid
        assert "AI 算力领涨" in c.get(f"/api/report/{rid}?format=md").text
        assert c.get("/api/reports").json()[0]["engine"] == "claude"
        assert c.get("/api/status").json()["last_report"]["id"] == rid
