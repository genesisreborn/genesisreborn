"""Ticker universe and extraction of ticker mentions from free text."""
from __future__ import annotations

import re

# Ticker -> company aliases (lowercase). Covers large caps plus names that are
# frequently "hot" on retail/social channels. Extend freely.
UNIVERSE: dict[str, list[str]] = {
    "AAPL": ["apple"], "MSFT": ["microsoft"], "NVDA": ["nvidia"], "AMZN": ["amazon"],
    "GOOGL": ["alphabet", "google"], "META": ["meta platforms", "facebook"], "TSLA": ["tesla"],
    "AVGO": ["broadcom"], "AMD": ["advanced micro devices"], "INTC": ["intel"], "QCOM": ["qualcomm"],
    "TSM": ["tsmc", "taiwan semiconductor"], "ASML": ["asml"], "MU": ["micron"], "ARM": ["arm holdings"],
    "SMCI": ["super micro", "supermicro"], "DELL": ["dell technologies"], "ORCL": ["oracle"],
    "CRM": ["salesforce"], "ADBE": ["adobe"], "NFLX": ["netflix"], "PLTR": ["palantir"],
    "SNOW": ["snowflake"], "CRWD": ["crowdstrike"], "PANW": ["palo alto networks"], "NET": ["cloudflare"],
    "SHOP": ["shopify"], "UBER": ["uber"], "ABNB": ["airbnb"], "COIN": ["coinbase"], "HOOD": ["robinhood"],
    "MSTR": ["microstrategy", "strategy inc"], "SQ": ["block inc"], "PYPL": ["paypal"], "SOFI": ["sofi"],
    "RIVN": ["rivian"], "LCID": ["lucid"], "NIO": ["nio inc"], "XPEV": ["xpeng"], "LI": ["li auto"],
    "BABA": ["alibaba"], "PDD": ["pinduoduo", "pdd holdings", "temu"], "JD": ["jd.com"], "BIDU": ["baidu"],
    "GME": ["gamestop"], "AMC": ["amc entertainment"], "BBBY": [], "RDDT": ["reddit inc"],
    "DJT": ["trump media"], "IONQ": ["ionq"], "RGTI": ["rigetti"], "QBTS": ["d-wave"],
    "SOUN": ["soundhound"], "AI": ["c3.ai"], "PATH": ["uipath"], "U": ["unity software"],
    "RKLB": ["rocket lab"], "ASTS": ["ast spacemobile"], "LUNR": ["intuitive machines"],
    "OKLO": ["oklo"], "SMR": ["nuscale"], "CEG": ["constellation energy"], "VST": ["vistra"],
    "MARA": ["marathon digital"], "RIOT": ["riot platforms"], "CLSK": ["cleanspark"],
    "JPM": ["jpmorgan", "jp morgan"], "BAC": ["bank of america"], "GS": ["goldman sachs"],
    "MS": ["morgan stanley"], "WFC": ["wells fargo"], "C": ["citigroup"], "V": ["visa inc"],
    "MA": ["mastercard"], "BRK-B": ["berkshire hathaway"], "XOM": ["exxon"], "CVX": ["chevron"],
    "OXY": ["occidental"], "LLY": ["eli lilly"], "NVO": ["novo nordisk"], "UNH": ["unitedhealth"],
    "PFE": ["pfizer"], "MRNA": ["moderna"], "JNJ": ["johnson & johnson"], "ABBV": ["abbvie"],
    "HIMS": ["hims & hers"], "WMT": ["walmart"], "COST": ["costco"], "TGT": ["target corp"],
    "HD": ["home depot"], "NKE": ["nike"], "SBUX": ["starbucks"], "MCD": ["mcdonald's", "mcdonalds"],
    "DIS": ["disney"], "BA": ["boeing"], "LMT": ["lockheed"], "RTX": ["raytheon"], "GE": ["ge aerospace"],
    "CAT": ["caterpillar"], "F": ["ford motor"], "GM": ["general motors"], "T": ["at&t"], "VZ": ["verizon"],
    "SPY": [], "QQQ": [], "IWM": [], "SOXL": [], "TQQQ": [], "ARKK": [],
}

# Tickers that are also common English words / abbreviations: only count them
# when written as a cashtag ($AI) to avoid false positives.
AMBIGUOUS = {
    "A", "AI", "ALL", "ARE", "C", "CAT", "F", "GE", "GS", "HD", "IT", "LI", "MA", "MS", "NET", "NOW",
    "ON", "OR", "PATH", "SO", "T", "U", "V", "ARM", "SMR", "DD", "CEO", "USA", "GDP", "CPI", "IPO",
    "ETF", "EPS", "FDA", "SEC", "FED", "AT", "BE", "BY", "GO", "IN", "IS", "OK", "TV", "UK", "US",
}

CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5}(?:[.-][A-Za-z])?)\b")
BARE_RE = re.compile(r"(?<![\w$])([A-Z]{2,5})(?![\w])")
# e.g. "Apple (AAPL)" / "(NASDAQ: NVDA)"
PAREN_RE = re.compile(r"\((?:NASDAQ|NYSE|AMEX|NYSEARCA)?\s*:?\s*([A-Z]{1,5})\)")

_ALIAS_PATTERNS = [
    (ticker, re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE))
    for ticker, aliases in UNIVERSE.items()
    for alias in aliases
]


def normalize(ticker: str) -> str:
    return ticker.upper().replace(".", "-")


def extract_tickers(text: str, extra_universe: set[str] | None = None) -> set[str]:
    """Return the set of known tickers referenced in ``text``.

    Rules: cashtags ($XYZ) for any known ticker; bare uppercase symbols and
    "(NASDAQ: XYZ)" only for non-ambiguous known tickers; company names via
    the alias table.
    """
    if not text:
        return set()
    known = set(UNIVERSE) | (extra_universe or set())
    found: set[str] = set()

    for m in CASHTAG_RE.finditer(text):
        t = normalize(m.group(1))
        if t in known:
            found.add(t)
    for m in PAREN_RE.finditer(text):
        t = normalize(m.group(1))
        if t in known:
            found.add(t)
    for m in BARE_RE.finditer(text):
        t = m.group(1)
        if t in known and t not in AMBIGUOUS:
            found.add(t)
    for ticker, pat in _ALIAS_PATTERNS:
        if ticker in known and pat.search(text):
            found.add(ticker)
    return found
