"""Strategy definitions: rule expressions + risk settings, validated and serialisable."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..market_data import INTERVALS
from .dsl import DSLError, validate

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


@dataclass
class StrategyConfig:
    name: str
    long_entry: str = ""
    long_exit: str = ""
    short_entry: str = ""
    short_exit: str = ""
    params: dict[str, float] = field(default_factory=dict)
    description: str = ""
    stop_loss_pct: float = 0.0       # e.g. 5 = exit if price moves 5% against the entry
    take_profit_pct: float = 0.0
    atr_stop_mult: float = 0.0       # e.g. 2 = stop at entry -/+ 2 x ATR(14)
    position_pct: float = 100.0      # % of equity per trade in backtests
    interval: str = "1d"
    symbols: list[str] = field(default_factory=list)
    enabled: bool = False            # live signal generation + alerts
    notify: bool = True              # push live signals to Telegram
    id: int | None = None
    preset: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        known = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in d.items() if k in known})
        cfg.normalize()
        return cfg

    def normalize(self) -> None:
        self.name = (self.name or "").strip()[:80]
        self.symbols = sorted({s.strip().upper() for s in self.symbols if s and s.strip()})
        self.params = {str(k): float(v) for k, v in (self.params or {}).items()}
        for attr in ("stop_loss_pct", "take_profit_pct", "atr_stop_mult", "position_pct"):
            setattr(self, attr, float(getattr(self, attr) or 0))

    def validate(self) -> list[str]:
        errors = []
        if not self.name:
            errors.append("策略名称不能为空")
        if self.interval not in INTERVALS:
            errors.append(f"周期必须是 {', '.join(INTERVALS)} 之一")
        if not (self.long_entry.strip() or self.short_entry.strip()):
            errors.append("至少需要一个开仓条件（做多或做空）")
        for key in self.params:
            if not re.match(r"^[a-z_][a-z0-9_]*$", key):
                errors.append(f"参数名 {key!r} 只能用小写字母、数字和下划线")
        for s in self.symbols:
            if not SYMBOL_RE.match(s):
                errors.append(f"代码格式不对：{s}")
        if not 0 < self.position_pct <= 100:
            errors.append("仓位比例需在 0-100 之间")
        for label, expr in (("做多开仓", self.long_entry), ("做多平仓", self.long_exit),
                            ("做空开仓", self.short_entry), ("做空平仓", self.short_exit)):
            if expr.strip():
                try:
                    validate(expr, self.params)
                except DSLError as exc:
                    errors.append(f"{label}：{exc}")
        return errors


__all__ = ["StrategyConfig", "DSLError"]
