"""Offline sample data so the full system can be exercised without network access."""
from __future__ import annotations

import random
import zlib
from datetime import datetime, timedelta, timezone

from . import RawItem

HEADLINES = [
    ("Reuters", "news", "Nvidia (NVDA) shares surge after record data-center revenue beats estimates"),
    ("CNBC Markets", "news", "Nvidia raises guidance as AI chip demand accelerates"),
    ("r/wallstreetbets", "social", "$NVDA calls printing again, this thing is mooning"),
    ("Yahoo Finance", "news", "Palantir (PLTR) jumps on new $500M Army contract"),
    ("r/stocks", "social", "PLTR breakout - anyone else loading up?"),
    ("StockTwits", "social", "$PLTR trending on StockTwits (#1): defense contract win"),
    ("MarketWatch", "news", "Tesla stock slumps after delivery miss and price cuts"),
    ("r/wallstreetbets", "social", "TSLA puts are the play this week, deliveries were weak"),
    ("Benzinga", "news", "IonQ (IONQ) soars as quantum computing stocks rally"),
    ("r/pennystocks", "social", "$RGTI and $IONQ quantum squeeze is real"),
    ("Seeking Alpha", "news", "Rigetti upgraded to Buy at Needham, shares jump"),
    ("CNBC Top News", "news", "Apple unveils new iPhone lineup; analysts see modest upgrade cycle"),
    ("Nasdaq", "news", "Micron (MU) beats on earnings as HBM memory sales soar"),
    ("r/stocks", "social", "MU earnings were strong, memory supercycle is here"),
    ("Investing.com", "news", "Oklo stock rockets as nuclear power deals with data centers expand"),
    ("r/wallstreetbets", "social", "$OKLO $SMR nuclear names flying today"),
    ("Google News: upgrades", "news", "Morgan Stanley upgrades AMD to overweight, sees AI GPU share gains"),
    ("MarketWatch", "news", "Boeing faces new FAA probe; shares fall"),
    ("Yahoo Finance", "news", "Coinbase (COIN) rallies as bitcoin tops record high"),
    ("r/CryptoCurrency", "social", "COIN and MSTR ripping with BTC"),
    ("StockTwits", "social", "$SOFI trending on StockTwits (#4): member growth accelerates"),
    ("CNBC Markets", "news", "Eli Lilly gains after FDA approval for new obesity drug"),
    ("Benzinga", "news", "Rocket Lab (RKLB) surges after successful Neutron test"),
    ("r/stocks", "social", "RKLB is the next SpaceX, strong backlog"),
    ("Reuters", "news", "Intel shares tumble on weak foundry outlook and layoffs"),
]


def demo_items(now: datetime | None = None, seed: int | None = None) -> list[RawItem]:
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed if seed is not None else int(now.timestamp() // 86400))
    items = []
    for src, kind, title in HEADLINES:
        for k in range(rng.randint(1, 3)):
            items.append(RawItem(
                source=src if k == 0 else f"{src} #{k}", kind=kind, title=title if k == 0 else f"{title} ({k})",
                published=now - timedelta(minutes=rng.randint(5, 20 * 60)),
                url=f"https://example.com/demo/{zlib.crc32(f'{title}|{k}'.encode())}",
                engagement=rng.randint(10, 3000) if kind == "social" else 0,
            ))
    # a thin baseline from previous days so velocity is meaningful
    for src, kind, title in rng.sample(HEADLINES, 8):
        items.append(RawItem(source=src, kind=kind, title=f"{title} (earlier)",
                             published=now - timedelta(days=rng.randint(2, 6)),
                             url=f"https://example.com/demo/old/{zlib.crc32(title.encode())}"))
    return items
