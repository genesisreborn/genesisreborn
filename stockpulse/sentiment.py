"""Headline sentiment: VADER augmented with a finance-specific lexicon."""
from __future__ import annotations

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

FINANCE_LEXICON = {
    # bullish
    "beat": 2.0, "beats": 2.0, "surge": 2.5, "surges": 2.5, "soar": 2.8, "soars": 2.8, "jump": 2.0,
    "jumps": 2.0, "rally": 2.2, "rallies": 2.2, "upgrade": 2.5, "upgraded": 2.5, "outperform": 2.0,
    "record": 1.5, "breakout": 2.0, "bullish": 2.8, "moon": 2.5, "mooning": 2.8, "calls": 1.0,
    "buyback": 1.8, "raises": 1.5, "raised": 1.2, "guidance": 0.3, "partnership": 1.5, "approval": 2.0,
    "approved": 2.0, "squeeze": 1.8, "tendies": 2.0, "skyrocket": 3.0, "rockets": 2.5, "strong": 1.5,
    "accelerates": 1.8, "exceeds": 2.0, "tops": 1.6, "rebound": 1.6, "gains": 1.6,
    # bearish
    "miss": -2.0, "misses": -2.0, "plunge": -2.8, "plunges": -2.8, "tumble": -2.5, "tumbles": -2.5,
    "sink": -2.2, "sinks": -2.2, "slump": -2.3, "downgrade": -2.5, "downgraded": -2.5,
    "underperform": -2.0, "bearish": -2.8, "puts": -1.0, "lawsuit": -1.8, "probe": -1.8,
    "investigation": -1.8, "recall": -1.8, "layoffs": -1.5, "cuts": -1.2, "bankruptcy": -3.2,
    "dilution": -2.0, "offering": -0.8, "short": -0.8, "fraud": -3.0, "warns": -2.0, "warning": -1.8,
    "halted": -1.5, "delisted": -3.0, "selloff": -2.3, "sell-off": -2.3, "crash": -3.0, "drops": -1.8,
    "falls": -1.6, "weak": -1.5, "tariff": -1.0, "tariffs": -1.0,
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(FINANCE_LEXICON)


def score(text: str) -> float:
    """Compound sentiment in [-1, 1]."""
    if not text:
        return 0.0
    return float(_analyzer.polarity_scores(text)["compound"])
