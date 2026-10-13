"""One analysis cycle: collect -> store -> aggregate -> quote -> score -> insights -> snapshot."""
from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta

from . import market, sentiment
from .config import Settings
from .db import Database, iso, utcnow
from .insights import build_insights
from .scoring import TickerStats, score_all
from .sources import RawItem, collect, rss, social, to_utc
from .sources.demo import demo_items
from .tickers import extract_tickers, normalize

log = logging.getLogger(__name__)

VALID_TICKER = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")
_cycle_lock = threading.Lock()


def gather(settings: Settings) -> list[RawItem]:
    if settings.demo_mode:
        return demo_items()
    srcs = rss.sources(settings.watchlist)
    if settings.enable_social:
        srcs += social.sources()
    return collect(srcs)


def ingest(db: Database, items: list[RawItem], settings: Settings) -> int:
    extra = set(settings.watchlist)
    new = 0
    for it in items:
        text = f"{it.title}. {it.summary}"
        tickers = extract_tickers(text, extra)
        tickers |= {normalize(t) for t in it.tickers if VALID_TICKER.match(normalize(t))}
        if not tickers:
            continue
        art = {
            "id": it.id, "source": it.source, "kind": it.kind, "title": it.title, "summary": it.summary,
            "url": it.url, "published": iso(to_utc(it.published)),
            "sentiment": sentiment.score(it.title + ". " + it.summary[:280]), "engagement": it.engagement,
        }
        if db.add_article(art, tickers):
            new += 1
    return new


def aggregate(db: Database, settings: Settings) -> dict[str, TickerStats]:
    now = utcnow()
    since = now - timedelta(hours=settings.lookback_hours)
    base_start = since - timedelta(days=settings.baseline_days)
    baseline = db.mention_counts_between(base_start, since)

    stats: dict[str, TickerStats] = {}
    for r in db.mention_stats(since):
        stats[r["ticker"]] = TickerStats(
            ticker=r["ticker"], mentions=r["mentions"], news=r["news"], social=r["social"],
            sources=r["sources"], sentiment=r["sentiment"] or 0.0, engagement=r["engagement"] or 0.0,
            baseline_daily=baseline.get(r["ticker"], 0) / settings.baseline_days,
        )
    # Always include the watchlist, even if quiet today.
    for t in settings.watchlist:
        stats.setdefault(t, TickerStats(ticker=t, baseline_daily=baseline.get(t, 0) / settings.baseline_days))

    # Keep the most-discussed candidates (plus watchlist) to bound quote lookups.
    ranked = sorted(stats.values(), key=lambda s: (s.mentions, s.engagement), reverse=True)
    keep = {s.ticker for s in ranked[: settings.max_candidates]} | set(settings.watchlist)
    return {t: s for t, s in stats.items() if t in keep}


def run_cycle(db: Database, settings: Settings, use_claude: bool = False) -> dict:
    """Execute a full refresh and persist a snapshot. Thread-safe (one cycle at a time).

    ``use_claude`` is only set by report generation; the periodic refresh stays free.
    """
    with _cycle_lock:
        started = utcnow()
        items = gather(settings)
        new = ingest(db, items, settings)
        stats = aggregate(db, settings)

        tickers = sorted(stats)
        quotes = market.demo_quotes(tickers) if settings.demo_mode else market.fetch_quotes(tickers)
        scored = score_all(stats, quotes)

        since = utcnow() - timedelta(hours=settings.lookback_hours)
        ranking = []
        for s in scored[: settings.top_n]:
            s.headlines = [
                {"title": a["title"], "url": a["url"], "source": a["source"], "published": a["published"],
                 "sentiment": round(a["sentiment"], 2)}
                for a in db.articles_for(s.ticker, since, limit=8)
            ]
            ranking.append(s.to_dict())

        insights = build_insights(ranking, settings, use_claude=use_claude)
        by_ticker = {x["ticker"]: x for x in insights.get("stocks", [])}
        for r in ranking:
            r["insight"] = by_ticker.get(r["ticker"])

        payload = {
            "generated_at": iso(utcnow()),
            "duration_s": round((utcnow() - started).total_seconds(), 1),
            "items_fetched": len(items),
            "new_articles": new,
            "tracked": len(stats),
            "demo": settings.demo_mode,
            "insight_engine": insights.get("engine"),
            "market_overview": insights.get("market_overview", ""),
            "themes": insights.get("themes", []),
            "insights": {k: v for k, v in insights.items() if k != "stocks"},
            "ranking": ranking,
            "watchlist": [s.to_dict() for s in scored if s.ticker in settings.watchlist],
        }
        db.save_snapshot(payload)
        db.prune()
        log.info("cycle done: %d items, %d new, %d tracked, top=%s", len(items), new, len(stats),
                 [r["ticker"] for r in ranking[:5]])
        return payload
