"""Load bars of symbols referenced via ref_close("SPY") etc. (same interval as the strategy)."""
from __future__ import annotations

import pandas as pd

from ..market_data import BarStore
from . import StrategyConfig
from .dsl import referenced_symbols


def strategy_refs(cfg: StrategyConfig) -> set[str]:
    return referenced_symbols(cfg.long_entry, cfg.long_exit, cfg.short_entry, cfg.short_exit)


def load_symbols(store: BarStore, symbols: set[str], interval: str, limit: int = 1500,
                 update: bool = True) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in symbols:
        if update:
            store.maybe_update(sym, interval, min_gap_s=300)
        out[sym] = store.get(sym, interval, limit, refresh_if_empty=update)
    return out


def load_refs(store: BarStore, cfg: StrategyConfig, limit: int = 1500, update: bool = True) -> dict[str, pd.DataFrame]:
    return load_symbols(store, strategy_refs(cfg), cfg.interval, limit, update)
