"""A small, safe, Pine-Script-like expression language for strategy rules.

Examples::

    crossover(ema(close, fast), ema(close, slow)) and rsi(close, 14) < 70
    close > highest(high, 20)[1]               # [n] = value n bars ago
    ref_close("SPY") > sma(ref_close("SPY"), 200)   # market filter: another symbol, same interval
    supertrend_dir(10, 3) == 1 and volume > sma(volume, 20) * 1.5

Expressions are parsed with ``ast`` and only whitelisted nodes, names and
functions are allowed - no attribute access, imports or arbitrary calls.
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Callable

import numpy as np
import pandas as pd

from .. import indicators as ind


class DSLError(ValueError):
    pass


def _series(x, index) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    return pd.Series(x, index=index, dtype=float)


def _bool(x, index) -> pd.Series:
    s = _series(x, index)
    return s.fillna(False).astype(bool) if s.dtype == bool or s.dtype == object else (s.fillna(0) != 0)


def build_functions(df: pd.DataFrame) -> dict[str, Callable[..., Any]]:
    idx = df.index
    S = lambda x: _series(x, idx)  # noqa: E731
    return {
        # moving averages / oscillators
        "sma": lambda src, n=20: ind.sma(S(src), n),
        "ema": lambda src, n=20: ind.ema(S(src), n),
        "wma": lambda src, n=20: ind.wma(S(src), n),
        "rsi": lambda src=None, n=14: ind.rsi(S(df["close"] if src is None else src), n),
        "macd": lambda src=None, f=12, s=26, sig=9: ind.macd(S(df["close"] if src is None else src), f, s, sig)["macd"],
        "macd_signal": lambda src=None, f=12, s=26, sig=9: ind.macd(S(df["close"] if src is None else src), f, s, sig)["signal"],
        "macd_hist": lambda src=None, f=12, s=26, sig=9: ind.macd(S(df["close"] if src is None else src), f, s, sig)["hist"],
        "bb_upper": lambda src=None, n=20, k=2: ind.bollinger(S(df["close"] if src is None else src), n, k)["upper"],
        "bb_mid": lambda src=None, n=20, k=2: ind.bollinger(S(df["close"] if src is None else src), n, k)["mid"],
        "bb_lower": lambda src=None, n=20, k=2: ind.bollinger(S(df["close"] if src is None else src), n, k)["lower"],
        "stoch_k": lambda n=14, d=3: ind.stoch(df, n, d)["k"],
        "stoch_d": lambda n=14, d=3: ind.stoch(df, n, d)["d"],
        "atr": lambda n=14: ind.atr(df, n),
        "vwap": lambda: ind.vwap(df),
        "supertrend": lambda n=10, mult=3: ind.supertrend(df, n, mult)["line"],
        "supertrend_dir": lambda n=10, mult=3: ind.supertrend(df, n, mult)["dir"],
        "obv": lambda: ind.obv(df),
        "highest": lambda src, n=20: ind.highest(S(src), n),
        "lowest": lambda src, n=20: ind.lowest(S(src), n),
        # series helpers
        "crossover": lambda a, b: ind.crossover(S(a), b),
        "crossunder": lambda a, b: ind.crossunder(S(a), b),
        "prev": lambda src, n=1: S(src).shift(int(n)),
        "change": lambda src, n=1: S(src).pct_change(int(n), fill_method=None) * 100,
        "rising": lambda src, n=1: (S(src).diff() > 0).astype(float).rolling(int(n)).sum() == int(n),
        "falling": lambda src, n=1: (S(src).diff() < 0).astype(float).rolling(int(n)).sum() == int(n),
        "abs": lambda x: S(x).abs(),
        "max": lambda a, b: np.maximum(S(a), S(b)),
        "min": lambda a, b: np.minimum(S(a), S(b)),
    }


REF_FUNCS = {"ref_open": "open", "ref_high": "high", "ref_low": "low", "ref_close": "close", "ref_volume": "volume"}
FUNCTION_NAMES = sorted([*build_functions(pd.DataFrame(columns=["open", "high", "low", "close", "volume"])), *REF_FUNCS])


def referenced_symbols(*exprs: str) -> set[str]:
    """Symbols used via ref_close("SPY") etc. in the given expressions."""
    out: set[str] = set()
    for e in exprs:
        if not (e or "").strip():
            continue
        try:
            tree = parse(e)
        except DSLError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in REF_FUNCS
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value.strip().upper())
    return out
SERIES_NAMES = ["open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"]

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod}
_CMPOPS = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Eq: operator.eq, ast.NotEq: operator.ne}


class Evaluator:
    def __init__(self, df: pd.DataFrame, params: dict[str, float] | None = None,
                 refs: dict[str, pd.DataFrame] | None = None, strict_refs: bool = True):
        """``refs``: bars of other symbols (same interval) for ref_*(); missing refs raise
        unless ``strict_refs`` is False (validation), where the main series stands in."""
        self.df = df
        self.idx = df.index
        self.refs = {k.upper(): v for k, v in (refs or {}).items()}
        self.strict_refs = strict_refs
        self.funcs = build_functions(df)
        self.names: dict[str, Any] = {
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
            "volume": df["volume"], "hl2": (df["high"] + df["low"]) / 2,
            "hlc3": (df["high"] + df["low"] + df["close"]) / 3,
            "ohlc4": (df["open"] + df["high"] + df["low"] + df["close"]) / 4,
            "true": True, "false": False,
        }
        for k, v in (params or {}).items():
            if k in self.names or k in self.funcs:
                raise DSLError(f"参数名 {k!r} 与内置名称冲突")
            self.names[k] = v

    def _ref(self, node: ast.Call) -> pd.Series:
        col = REF_FUNCS[node.func.id]
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            raise DSLError(f'{node.func.id}() 需要一个代码字符串，例如 {node.func.id}("SPY")')
        sym = node.args[0].value.strip().upper()
        if not sym or len(sym) > 15:
            raise DSLError("代码格式不对")
        ref = self.refs.get(sym)
        if ref is None or ref.empty:
            if self.strict_refs:
                raise DSLError(f"缺少 {sym} 的行情数据")
            return self.df[col]
        # align on time; forward-fill only from earlier bars, so nothing from the future leaks in
        return ref[col].astype(float).reindex(self.idx, method="ffill")

    def eval(self, expr: str) -> pd.Series:
        tree = parse(expr)
        out = self._ev(tree.body)
        return _bool(out, self.idx)

    def eval_value(self, expr: str) -> pd.Series:
        return _series(self._ev(parse(expr).body), self.idx)

    def _ev(self, node: ast.AST):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
                return node.value
            raise DSLError(f"不支持的常量 {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in self.names:
                return self.names[node.id]
            raise DSLError(f"未知名称 {node.id!r}")
        if isinstance(node, ast.BoolOp):
            vals = [_bool(self._ev(v), self.idx) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.UnaryOp):
            v = self._ev(node.operand)
            if isinstance(node.op, ast.Not):
                return ~_bool(v, self.idx)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](self._ev(node.left), self._ev(node.right))
        if isinstance(node, ast.Compare):
            left = self._ev(node.left)
            result = None
            for op, comp in zip(node.ops, node.comparators):
                if type(op) not in _CMPOPS:
                    raise DSLError("不支持的比较运算")
                right = self._ev(comp)
                part = _bool(_CMPOPS[type(op)](_series(left, self.idx), right), self.idx)
                result = part if result is None else (result & part)
                left = right
            return result
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in REF_FUNCS:
            return self._ref(node)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in self.funcs:
                name = getattr(node.func, "id", "?")
                raise DSLError(f"未知函数 {name!r}")
            if node.keywords:
                raise DSLError("函数参数请按位置传入，例如 ema(close, 20)")
            args = [self._ev(a) for a in node.args]
            try:
                return self.funcs[node.func.id](*args)
            except TypeError as exc:
                raise DSLError(f"{node.func.id}() 参数错误：{exc}") from exc
        if isinstance(node, ast.Subscript):
            base = _series(self._ev(node.value), self.idx)
            n = self._ev(node.slice)
            if not isinstance(n, (int, float)) or n < 0:
                raise DSLError("[n] 只能是非负整数（n 根 K 线之前）")
            return base.shift(int(n))
        if isinstance(node, ast.IfExp):
            cond = _bool(self._ev(node.test), self.idx)
            return _series(self._ev(node.body), self.idx).where(cond, _series(self._ev(node.orelse), self.idx))
        raise DSLError(f"不支持的语法：{type(node).__name__}")


def parse(expr: str) -> ast.Expression:
    expr = (expr or "").strip()
    if not expr:
        raise DSLError("表达式为空")
    if len(expr) > 2000:
        raise DSLError("表达式过长")
    try:
        return ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise DSLError(f"语法错误：{exc.msg}（第 {exc.offset} 个字符附近）") from exc


def validate(expr: str, params: dict | None = None) -> None:
    """Raise DSLError if the expression is invalid (evaluated on a tiny synthetic frame)."""
    idx = pd.date_range("2020-01-01", periods=60, freq="D", tz="UTC")
    base = np.linspace(100, 120, 60)
    df = pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base + 0.5,
                       "volume": np.full(60, 1e6)}, index=idx)
    Evaluator(df, params, strict_refs=False).eval(expr)
