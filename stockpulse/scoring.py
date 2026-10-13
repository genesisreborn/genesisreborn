"""Combine buzz, sentiment and market features into a 0-100 "hot" score."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .market import Quote

# Component weights (sum to 100)
WEIGHTS = {
    "buzz": 25,        # how much it's being talked about (mentions, engagement)
    "velocity": 20,    # talk now vs. its own recent baseline
    "sentiment": 20,   # tone of headlines/posts
    "volume": 15,      # unusual trading volume
    "momentum": 15,    # price trend
    "breadth": 5,      # number of distinct sources
}


@dataclass
class TickerStats:
    ticker: str
    mentions: int = 0
    news: int = 0
    social: int = 0
    sources: int = 0
    sentiment: float = 0.0
    engagement: float = 0.0
    baseline_daily: float = 0.0


@dataclass
class Scored:
    ticker: str
    score: float
    signal: str
    components: dict[str, float]
    mentions: int
    news: int
    social: int
    sources: int
    sentiment: float
    velocity: float
    price: float | None = None
    change_1d: float | None = None
    change_5d: float | None = None
    volume_ratio: float | None = None
    above_ma20: bool | None = None
    spark: list[float] | None = None
    headlines: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Map each key to its percentile rank in [0, 1] (ties share the average rank)."""
    if not values:
        return {}
    if len(values) == 1:
        return {k: 0.5 for k in values}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    ranks: dict[str, float] = {}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        r = ((i + j) / 2) / (n - 1)
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = r
        i = j + 1
    return ranks


def momentum_value(q: Quote | None) -> float:
    if q is None or q.change_1d is None:
        return 0.0
    v = q.change_1d * 0.6 + (q.change_5d or 0) * 0.3
    if q.above_ma20:
        v += 1.0
    return v


def signal_label(score: float, sentiment: float, change_1d: float | None, hot: bool = False) -> str:
    """``hot`` = the ticker is among the most-discussed names regardless of tone."""
    if score >= 75 and sentiment > 0.15:
        return "强势看涨"
    if score >= 60 and sentiment > 0.05:
        return "看涨"
    if sentiment < -0.2 and hot:
        return "热度高·偏空"
    if change_1d is not None and change_1d > 8 and sentiment <= 0.05:
        return "追高风险"
    if score >= 45:
        return "关注"
    return "中性"


def score_all(stats: dict[str, TickerStats], quotes: dict[str, Quote]) -> list[Scored]:
    if not stats:
        return []
    buzz_raw = {t: math.log1p(s.mentions) + 0.5 * math.log1p(s.engagement / 100) for t, s in stats.items()}
    velocity = {t: (s.mentions + 1) / (s.baseline_daily + 1) for t, s in stats.items()}
    vol = {t: (quotes[t].volume_ratio if t in quotes and quotes[t].volume_ratio else 1.0) for t in stats}
    mom = {t: momentum_value(quotes.get(t)) for t in stats}
    breadth = {t: float(s.sources) for t, s in stats.items()}

    pr_buzz = percentile_ranks(buzz_raw)
    pr_vel = percentile_ranks(velocity)
    pr_vol = percentile_ranks(vol)
    pr_mom = percentile_ranks(mom)
    pr_breadth = percentile_ranks(breadth)

    out: list[Scored] = []
    for t, s in stats.items():
        # Shrink sentiment toward 0 when there are few mentions.
        confidence = min(1.0, s.mentions / 5)
        sent = s.sentiment * confidence
        comps = {
            "buzz": pr_buzz[t] * WEIGHTS["buzz"],
            "velocity": pr_vel[t] * WEIGHTS["velocity"],
            "sentiment": (sent + 1) / 2 * WEIGHTS["sentiment"],
            "volume": pr_vol[t] * WEIGHTS["volume"],
            "momentum": pr_mom[t] * WEIGHTS["momentum"],
            "breadth": pr_breadth[t] * WEIGHTS["breadth"],
        }
        total = round(sum(comps.values()), 1)
        q = quotes.get(t)
        out.append(Scored(
            ticker=t, score=total,
            signal=signal_label(total, sent, q.change_1d if q else None, hot=pr_buzz[t] >= 0.5),
            components={k: round(v, 1) for k, v in comps.items()},
            mentions=s.mentions, news=s.news, social=s.social, sources=s.sources,
            sentiment=round(s.sentiment, 3), velocity=round(velocity[t], 2),
            price=q.price if q else None, change_1d=q.change_1d if q else None,
            change_5d=q.change_5d if q else None, volume_ratio=q.volume_ratio if q else None,
            above_ma20=q.above_ma20 if q else None, spark=q.spark if q else None,
        ))
    out.sort(key=lambda x: x.score, reverse=True)
    return out
