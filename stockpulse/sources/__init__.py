"""News / social data sources. Each source yields RawItem objects."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (StockPulse; +https://github.com/genesisreborn/genesisreborn)"


@dataclass
class RawItem:
    source: str
    kind: str                     # "news" | "social"
    title: str
    published: datetime
    url: str = ""
    summary: str = ""
    engagement: float = 0.0       # upvotes / likes / comments, for social weighting
    tickers: set[str] = field(default_factory=set)  # tickers explicitly tagged by the source

    @property
    def id(self) -> str:
        key = (self.url or "") + "|" + self.title.strip().lower()
        return hashlib.sha1(key.encode()).hexdigest()


def http_client() -> httpx.Client:
    return httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def to_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


Source = Callable[[], Iterable[RawItem]]


def collect(sources: list[tuple[str, Source]]) -> list[RawItem]:
    """Run every source, isolating failures so one broken feed never stops the cycle."""
    items: list[RawItem] = []
    for name, fn in sources:
        try:
            got = list(fn())
            log.info("source %s: %d items", name, len(got))
            items.extend(got)
        except Exception as exc:  # noqa: BLE001 - network sources fail in many ways
            log.warning("source %s failed: %s", name, exc)
    return items
