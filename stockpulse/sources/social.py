"""Retail/social buzz: Reddit investing subs and StockTwits trending."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

from . import RawItem, Source, http_client

SUBREDDITS = ["wallstreetbets", "stocks", "investing", "options", "StockMarket", "pennystocks"]


def parse_reddit(sub: str, data: dict) -> list[RawItem]:
    items = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        if p.get("stickied"):
            continue
        title = p.get("title", "").strip()
        if not title:
            continue
        items.append(RawItem(
            source=f"r/{sub}", kind="social", title=title,
            summary=(p.get("selftext") or "")[:600],
            url="https://www.reddit.com" + p.get("permalink", ""),
            published=datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc),
            engagement=float(p.get("score", 0)) + 2 * float(p.get("num_comments", 0)),
        ))
    return items


def fetch_reddit(sub: str) -> list[RawItem]:
    with http_client() as c:
        r = c.get(f"https://www.reddit.com/r/{sub}/hot.json", params={"limit": 75})
        r.raise_for_status()
        return parse_reddit(sub, r.json())


def parse_stocktwits(data: dict) -> list[RawItem]:
    now = datetime.now(timezone.utc)
    items = []
    for i, s in enumerate(data.get("symbols", [])):
        sym = (s.get("symbol") or "").upper()
        if not sym:
            continue
        title = f"${sym} trending on StockTwits (#{i + 1}): {s.get('title', '')}".strip()
        items.append(RawItem(
            source="StockTwits", kind="social", title=title, published=now,
            url=f"https://stocktwits.com/symbol/{sym}",
            summary=(s.get("trending_summary") or s.get("summary") or "")[:600],
            engagement=float(max(0, 30 - i) * 10), tickers={sym},
        ))
    return items


def fetch_stocktwits() -> list[RawItem]:
    with http_client() as c:
        r = c.get("https://api.stocktwits.com/api/2/trending/symbols.json")
        r.raise_for_status()
        return parse_stocktwits(r.json())


def sources() -> list[tuple[str, Source]]:
    out: list[tuple[str, Source]] = [(f"r/{s}", partial(fetch_reddit, s)) for s in SUBREDDITS]
    out.append(("StockTwits", fetch_stocktwits))
    return out
