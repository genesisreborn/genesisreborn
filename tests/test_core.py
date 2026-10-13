import json
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stockpulse import sentiment
from stockpulse.config import Settings
from stockpulse.db import Database
from stockpulse.insights import rule_insights
from stockpulse.market import Quote, quotes_from_frame
from stockpulse.pipeline import run_cycle
from stockpulse.scoring import TickerStats, percentile_ranks, score_all
from stockpulse.sources.rss import parse_feed
from stockpulse.sources.social import parse_reddit, parse_stocktwits
from stockpulse.telegram_bot import TelegramNotifier, _chunks, format_report
from stockpulse.tickers import extract_tickers


def make_settings(tmp_path, **kw):
    s = Settings()
    s.db_path = str(tmp_path / "t.db")
    s.demo_mode = True
    s.anthropic_api_key = ""
    s.telegram_bot_token = kw.pop("token", "")
    s.telegram_chat_ids = []
    s.watchlist = ["AAPL", "NVDA"]
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---- tickers ----
@pytest.mark.parametrize("text,expected", [
    ("Nvidia (NVDA) shares surge", {"NVDA"}),
    ("$TSLA puts and $ai calls", {"TSLA", "AI"}),
    ("AI is changing everything for IT teams", set()),          # ambiguous words ignored
    ("Apple and Microsoft report earnings", {"AAPL", "MSFT"}),  # company names
    ("Shares of (NASDAQ: PLTR) jumped", {"PLTR"}),
    ("I paid $100 for lunch", set()),
])
def test_extract_tickers(text, expected):
    assert extract_tickers(text) == expected


def test_extract_tickers_extra_universe():
    assert extract_tickers("ZZZQ beats estimates", {"ZZZQ"}) == {"ZZZQ"}


# ---- sentiment ----
def test_sentiment_direction():
    assert sentiment.score("Stock soars after earnings beat and upgrade") > 0.3
    assert sentiment.score("Shares plunge after fraud probe and downgrade") < -0.3


# ---- scoring ----
def test_percentile_ranks_ties():
    r = percentile_ranks({"a": 1, "b": 1, "c": 5})
    assert r["c"] == 1.0 and r["a"] == r["b"] == 0.25


def test_score_all_orders_hot_first():
    stats = {
        "HOT": TickerStats("HOT", mentions=30, news=10, social=20, sources=8, sentiment=0.6, engagement=5000, baseline_daily=2),
        "MEH": TickerStats("MEH", mentions=3, news=3, sources=2, sentiment=0.0, baseline_daily=3),
        "BAD": TickerStats("BAD", mentions=12, news=10, social=2, sources=5, sentiment=-0.7, baseline_daily=4),
    }
    quotes = {
        "HOT": Quote("HOT", price=10, change_1d=6, change_5d=12, volume_ratio=3.5, above_ma20=True),
        "MEH": Quote("MEH", price=10, change_1d=0.1, change_5d=0, volume_ratio=0.9, above_ma20=False),
        "BAD": Quote("BAD", price=10, change_1d=-5, change_5d=-9, volume_ratio=2.0, above_ma20=False),
    }
    out = score_all(stats, quotes)
    assert [s.ticker for s in out][0] == "HOT"
    assert out[0].signal == "强势看涨"
    bad = next(s for s in out if s.ticker == "BAD")
    assert bad.signal == "热度高·偏空"
    assert all(0 <= s.score <= 100 for s in out)


def test_quotes_from_frame():
    idx = pd.date_range("2026-01-01", periods=30)
    cols = pd.MultiIndex.from_product([["AAA", "BBB"], ["Close", "Volume"]])
    data = []
    for i in range(30):
        data.append([100 + i, 1000, 50 - i * 0.5, 2000 if i < 29 else 8000])
    df = pd.DataFrame(data, index=idx, columns=cols)
    q = quotes_from_frame(["AAA", "BBB"], df)
    assert q["AAA"].change_1d > 0 and q["AAA"].above_ma20
    assert q["BBB"].volume_ratio == 4.0 and not q["BBB"].above_ma20


# ---- sources ----
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Micron (MU) soars on HBM demand</title><link>https://x/1</link>
<pubDate>Mon, 12 Oct 2026 14:00:00 GMT</pubDate><description>&lt;p&gt;Memory boom&lt;/p&gt;</description></item>
</channel></rss>"""


def test_parse_feed():
    items = parse_feed("Test", RSS)
    assert len(items) == 1
    assert items[0].title.startswith("Micron") and items[0].summary == "Memory boom"
    assert items[0].published == datetime(2026, 10, 12, 14, tzinfo=timezone.utc)


def test_parse_social():
    reddit = {"data": {"children": [
        {"data": {"title": "Daily thread", "stickied": True}},
        {"data": {"title": "$GME to the moon", "permalink": "/r/x/1", "created_utc": 1_760_000_000, "score": 100, "num_comments": 50}},
    ]}}
    items = parse_reddit("wallstreetbets", reddit)
    assert len(items) == 1 and items[0].engagement == 200
    st = parse_stocktwits({"symbols": [{"symbol": "rklb", "title": "Rocket Lab"}]})
    assert st[0].tickers == {"RKLB"}


# ---- pipeline ----
def test_demo_cycle_end_to_end(tmp_path):
    s = make_settings(tmp_path)
    db = Database(s.db_path)
    snap = run_cycle(db, s)
    assert snap["ranking"], "demo should produce a ranking"
    top = snap["ranking"][0]
    assert top["insight"] and top["headlines"]
    assert snap["insight_engine"] == "rules"
    assert db.latest_snapshot()["generated_at"] == snap["generated_at"]
    # second cycle should not duplicate stored articles
    n1 = db.query("SELECT COUNT(*) AS n FROM articles")[0]["n"]
    run_cycle(db, s)
    n2 = db.query("SELECT COUNT(*) AS n FROM articles")[0]["n"]
    assert n2 == n1


def test_rule_insights_risks():
    r = {"ticker": "X", "signal": "看涨", "score": 70, "velocity": 4, "volume_ratio": 2.5, "change_1d": 3,
         "change_5d": 20, "sentiment": 0.4, "news": 1, "social": 9, "headlines": [{"title": "X soars"}]}
    out = rule_insights([r])["stocks"][0]
    assert out["view"] == "bullish"
    assert "回吐" in out["risks"] and "社交媒体" in out["risks"]


# ---- telegram ----
def test_telegram_commands_and_alerts(tmp_path):
    sent = []

    def handler(request: httpx.Request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    s = make_settings(tmp_path, token="123:abc", alert_min_score=0, alert_top_n=2)
    db = Database(s.db_path)
    bot = TelegramNotifier(s, db, client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert "已订阅" in bot.handle_command("42", "/start")
    assert bot.recipients() == ["42"]
    assert "稍后" in bot.handle_command("42", "/top")

    snap = run_cycle(db, s)
    assert "StockPulse" in bot.handle_command("42", "/top@MyBot")
    t = snap["ranking"][0]["ticker"]
    assert t in bot.handle_command("42", f"/stock ${t.lower()}")

    fired = bot.check_alerts(snap)
    assert len(fired) == 2
    assert bot.check_alerts(snap) == []           # de-duplicated for the day
    assert bot.send_daily_report() == 1
    assert all(m["chat_id"] == "42" and m["parse_mode"] == "HTML" for m in sent)

    assert "取消" in bot.handle_command("42", "/stop")
    assert bot.recipients() == []


def test_chunks_respect_limit():
    text = "\n".join("x" * 100 for _ in range(100))
    assert all(len(c) <= 4096 for c in _chunks(text))


def test_format_report_escapes_html():
    snap = {"generated_at": "now", "market_overview": "<script>", "themes": [], "ranking": []}
    assert "&lt;script&gt;" in format_report(snap)


# ---- web ----
def test_web_api(tmp_path):
    from stockpulse.web.app import create_app

    s = make_settings(tmp_path)
    app = create_app(s, start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/snapshot").json()["ranking"] == []
        run_cycle(app.state.engine.db, s)
        snap = c.get("/api/snapshot").json()
        assert snap["ranking"]
        t = snap["ranking"][0]["ticker"]
        d = c.get(f"/api/stock/{t}").json()
        assert d["current"]["ticker"] == t and d["articles"]
        assert c.get("/api/stock/ZZZZ").status_code == 404
        assert c.get("/api/news").json()
        assert c.get("/api/status").json()["demo"] is True
        assert "StockPulse" in c.get("/").text
