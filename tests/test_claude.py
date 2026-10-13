"""Exercise the Claude insight path against a local fake Messages endpoint."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stockpulse.config import Settings
from stockpulse.insights import build_insights

ANSWER = {
    "market_overview": "AI 算力继续领涨。",
    "themes": ["AI 算力"],
    "stocks": [{"ticker": "NVDA", "view": "bullish", "confidence": 80, "catalyst": "财报超预期",
                "risks": "估值偏高", "summary": "趋势向上。"}],
}


@pytest.fixture
def fake_api(monkeypatch):
    captured = {}
    reply = {"stop_reason": "end_turn", "text": json.dumps(ANSWER, ensure_ascii=False)}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured["body"] = json.loads(body)
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


def _settings():
    s = Settings()
    s.anthropic_api_key = "test"
    s.demo_mode = False
    return s


RANK = [{"ticker": "NVDA", "score": 80, "mentions": 10, "news": 5, "social": 5, "velocity": 3.0,
         "sentiment": 0.5, "signal": "看涨", "headlines": [{"title": "Nvidia soars"}]}]


def test_claude_insights_request_and_parse(fake_api):
    captured, _ = fake_api
    out = build_insights(RANK, _settings())
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
    out = build_insights(RANK, _settings())
    assert out["engine"] == "rules"
