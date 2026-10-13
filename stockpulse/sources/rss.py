"""Financial news via public RSS feeds (no API keys required)."""
from __future__ import annotations

import calendar
import html
import re
from datetime import datetime, timezone
from functools import partial
from urllib.parse import quote_plus

import feedparser

from . import RawItem, Source, http_client

GENERAL_FEEDS: dict[str, str] = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CNBC Markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "CNBC Top News": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Nasdaq": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "Seeking Alpha": "https://seekingalpha.com/market_currents.xml",
    "Investing.com": "https://www.investing.com/rss/news_25.rss",
    "Benzinga": "https://www.benzinga.com/feed",
    "Google News: stocks": "https://news.google.com/rss/search?q=" + quote_plus("stock surges OR soars OR jumps when:1d") + "&hl=en-US&gl=US&ceid=US:en",
    "Google News: upgrades": "https://news.google.com/rss/search?q=" + quote_plus("analyst upgrade stock when:1d") + "&hl=en-US&gl=US&ceid=US:en",
}

TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).strip()


def _published(entry) -> datetime:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return datetime.now(timezone.utc)


def parse_feed(name: str, content: bytes | str, ticker: str | None = None) -> list[RawItem]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        title = _clean(e.get("title", ""))
        if not title:
            continue
        items.append(RawItem(
            source=name, kind="news", title=title, url=e.get("link", ""),
            summary=_clean(e.get("summary", ""))[:600], published=_published(e),
            tickers={ticker} if ticker else set(),
        ))
    return items


def fetch_feed(name: str, url: str, ticker: str | None = None) -> list[RawItem]:
    with http_client() as c:
        r = c.get(url)
        r.raise_for_status()
        return parse_feed(name, r.content, ticker)


def ticker_feed_url(ticker: str) -> str:
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"


def sources(watchlist: list[str]) -> list[tuple[str, Source]]:
    out: list[tuple[str, Source]] = [(n, partial(fetch_feed, n, u)) for n, u in GENERAL_FEEDS.items()]
    for t in watchlist:
        out.append((f"Yahoo:{t}", partial(fetch_feed, f"Yahoo Finance", ticker_feed_url(t), t)))
    return out
