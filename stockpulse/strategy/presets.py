"""Built-in strategy templates. Each is a normal rule-based strategy you can copy and edit."""
from __future__ import annotations

from . import StrategyConfig

PRESETS: dict[str, dict] = {
    "ema_cross": dict(
        name="均线金叉/死叉", description="快线上穿慢线做多，下穿平仓。经典趋势跟随。",
        params={"fast": 12, "slow": 26},
        long_entry="crossover(ema(close, fast), ema(close, slow))",
        long_exit="crossunder(ema(close, fast), ema(close, slow))",
        stop_loss_pct=8,
    ),
    "rsi_reversion": dict(
        name="RSI 超卖反弹", description="价格在 200 日均线上方、RSI 跌破超卖线后回升时做多，RSI 回到高位平仓。",
        params={"len": 14, "oversold": 30, "overbought": 65},
        long_entry="close > sma(close, 200) and crossover(rsi(close, len), oversold)",
        long_exit="rsi(close, len) > overbought",
        stop_loss_pct=6,
    ),
    "macd_trend": dict(
        name="MACD 趋势", description="MACD 线上穿信号线且在零轴上方做多，下穿信号线平仓。",
        params={},
        long_entry="crossover(macd(close), macd_signal(close)) and macd(close) > 0",
        long_exit="crossunder(macd(close), macd_signal(close))",
        atr_stop_mult=2.5,
    ),
    "donchian_breakout": dict(
        name="唐奇安突破（海龟）", description="收盘突破前 N 日最高价做多，跌破前 M 日最低价平仓。",
        params={"entry_len": 20, "exit_len": 10},
        long_entry="close > highest(high, entry_len)[1]",
        long_exit="close < lowest(low, exit_len)[1]",
        atr_stop_mult=2,
    ),
    "bollinger_squeeze": dict(
        name="布林带放量突破", description="收盘突破布林上轨且成交量大于 20 日均量 1.5 倍做多，跌回中轨平仓。",
        params={"len": 20, "k": 2, "vol_mult": 1.5},
        long_entry="crossover(close, bb_upper(close, len, k)) and volume > sma(volume, 20) * vol_mult",
        long_exit="crossunder(close, bb_mid(close, len, k))",
        stop_loss_pct=5,
    ),
    "supertrend": dict(
        name="SuperTrend 多空", description="SuperTrend 翻多做多、翻空做空。适合趋势明显的品种。",
        params={"len": 10, "mult": 3},
        long_entry="supertrend_dir(len, mult) == 1 and supertrend_dir(len, mult)[1] == -1",
        long_exit="supertrend_dir(len, mult) == -1",
        short_entry="supertrend_dir(len, mult) == -1 and supertrend_dir(len, mult)[1] == 1",
        short_exit="supertrend_dir(len, mult) == 1",
    ),
    "momentum_pullback": dict(
        name="强势股回踩", description="多头排列（20>50>200 日均线）中回踩 20 日线后重新站上时做多。",
        params={},
        long_entry=("ema(close, 20) > sma(close, 50) and sma(close, 50) > sma(close, 200)"
                    " and low < ema(close, 20) and close > ema(close, 20) and close > open"),
        long_exit="close < sma(close, 50)",
        stop_loss_pct=7, take_profit_pct=20,
    ),
}


def preset(key: str, **overrides) -> StrategyConfig:
    d = {**PRESETS[key], "preset": key, **overrides}
    return StrategyConfig.from_dict(d)
