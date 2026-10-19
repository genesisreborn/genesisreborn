"""Custom indicator scripts: a small Pine-Script-like language on top of the rule DSL.

Example::

    length = 20                       # a number on its own line = adjustable input
    mult = 2
    basis = sma(close, length)
    dev = mult * stdev(close, length)
    plot(basis, "中轨", "#2962ff")
    plot(basis + dev, "上轨")
    plot(basis - dev, "下轨")
    hline(0, "零轴")
    plotshape(crossover(close, basis + dev), "突破", "up")

Statements (one per line, ``#`` or ``//`` starts a comment):
    name = expr                       variable (series or number); later lines can use it
    plot(expr, "title"[, "color"[, "line|histogram|dots|area"]])
    hline(value, "title"[, "color"])
    plotshape(cond, "title"[, "up|down"[, "color"]])   marker on bars where cond is true

Expressions are the same safe DSL used in strategy rules (all its functions, ``[n]`` history,
``x if cond else y``); only these statement forms are accepted, nothing else executes.
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .dsl import DSLError, Evaluator, _bool, _series

MAX_LINES = 200
MAX_PLOTS = 16
KINDS = ("line", "histogram", "dots", "area")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


@dataclass
class Script:
    inputs: dict[str, float] = field(default_factory=dict)
    statements: list[tuple[int, ast.stmt]] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    out, quote = [], None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" or line.startswith("//", i):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _str_arg(node: ast.AST, what: str, lineno: int) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise DSLError(f"第 {lineno} 行：{what} 需要是字符串（用引号）")


def parse_script(src: str) -> Script:
    lines = (src or "").splitlines()
    if len(lines) > MAX_LINES:
        raise DSLError(f"脚本最多 {MAX_LINES} 行")
    sc = Script()
    seen_names: set[str] = set()
    for no, raw in enumerate(lines, 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if len(line) > 2000:
            raise DSLError(f"第 {no} 行太长")
        try:
            mod = ast.parse(line, mode="exec")
        except SyntaxError as exc:
            raise DSLError(f"第 {no} 行语法错误：{exc.msg}") from exc
        if len(mod.body) != 1:
            raise DSLError(f"第 {no} 行：一行只能写一条语句")
        st = mod.body[0]
        if isinstance(st, ast.Assign):
            if len(st.targets) != 1 or not isinstance(st.targets[0], ast.Name):
                raise DSLError(f"第 {no} 行：只能给单个变量赋值，例如 basis = sma(close, 20)")
            name = st.targets[0].id
            if name.startswith("_"):
                raise DSLError(f"第 {no} 行：变量名不能以下划线开头")
            val = st.value
            neg = isinstance(val, ast.UnaryOp) and isinstance(val.op, ast.USub)
            inner = val.operand if neg else val
            if (isinstance(inner, ast.Constant) and isinstance(inner.value, (int, float))
                    and not isinstance(inner.value, bool) and name not in seen_names):
                sc.inputs[name] = -inner.value if neg else inner.value
            seen_names.add(name)
            sc.statements.append((no, st))
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name) \
                and st.value.func.id in ("plot", "hline", "plotshape"):
            if st.value.keywords:
                raise DSLError(f"第 {no} 行：参数请按位置传入")
            if not st.value.args:
                raise DSLError(f"第 {no} 行：{st.value.func.id}() 缺少参数")
            sc.statements.append((no, st))
        else:
            raise DSLError(f"第 {no} 行：只支持 变量 = 表达式、plot()、hline()、plotshape()")
    if not any(isinstance(st, ast.Expr) for _, st in sc.statements):
        raise DSLError("脚本里至少要有一个 plot()、hline() 或 plotshape()")
    return sc


def _points(s: pd.Series) -> list[dict]:
    out = []
    for t, v in s.items():
        if v is None:
            continue
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            continue
        out.append({"time": int(t.timestamp()), "value": round(f, 6)})
    return out


def run_script(src: str, df: pd.DataFrame, inputs: dict[str, float] | None = None, refs=None,
               strict_refs: bool = True) -> dict:
    """Evaluate a script on bars -> {plots, hlines, shapes, inputs}."""
    sc = parse_script(src)
    overrides = {}
    for k, v in (inputs or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if k in sc.inputs and math.isfinite(f):
            overrides[k] = f
    applied: set[str] = set()
    ev = Evaluator(df, refs=refs, strict_refs=strict_refs)
    reserved = set(ev.names) | set(ev.funcs) | {"plot", "hline", "plotshape"}
    plots, hlines, shapes = [], [], []
    for no, st in sc.statements:
        try:
            if isinstance(st, ast.Assign):
                name = st.targets[0].id
                if name in reserved:
                    raise DSLError(f"{name!r} 是内置名称，换个变量名")
                if name in overrides and name not in applied:   # only the declaring line is the input
                    applied.add(name)
                    ev.names[name] = overrides[name]
                    continue
                val = ev._ev(st.value)
                ev.names[name] = val if isinstance(val, (int, float, bool)) else _series(val, df.index)
                continue
            call = st.value
            fn, args = call.func.id, call.args
            if fn == "plot":
                if len(plots) >= MAX_PLOTS:
                    raise DSLError(f"最多 {MAX_PLOTS} 条 plot")
                val = _series(ev._ev(args[0]), df.index).astype(float)
                title = _str_arg(args[1], "标题", no) if len(args) > 1 else f"plot{len(plots) + 1}"
                color = _str_arg(args[2], "颜色", no) if len(args) > 2 else None
                kind = _str_arg(args[3], "样式", no) if len(args) > 3 else "line"
                if color and not COLOR_RE.match(color):
                    raise DSLError("颜色请用 #RRGGBB 格式")
                if kind not in KINDS:
                    raise DSLError(f"样式只能是 {' / '.join(KINDS)}")
                plots.append({"title": title, "color": color, "kind": kind, "data": _points(val)})
            elif fn == "hline":
                v = ev._ev(args[0])
                if not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise DSLError("hline() 的值必须是有限的数字")
                color = _str_arg(args[2], "颜色", no) if len(args) > 2 else None
                if color and not COLOR_RE.match(color):
                    raise DSLError("颜色请用 #RRGGBB 格式")
                hlines.append({"value": float(v), "title": _str_arg(args[1], "标题", no) if len(args) > 1 else "", "color": color})
            else:  # plotshape
                cond = _bool(ev._ev(args[0]), df.index)
                title = _str_arg(args[1], "标题", no) if len(args) > 1 else ""
                direction = _str_arg(args[2], "方向", no) if len(args) > 2 else "up"
                if direction not in ("up", "down"):
                    raise DSLError("plotshape 方向只能是 up 或 down")
                color = _str_arg(args[3], "颜色", no) if len(args) > 3 else None
                if color and not COLOR_RE.match(color):
                    raise DSLError("颜色请用 #RRGGBB 格式")
                times = [int(t.timestamp()) for t, v in cond.items() if v]
                shapes.append({"title": title, "direction": direction, "color": color, "times": times[-500:]})
        except DSLError as exc:
            msg = str(exc)
            raise DSLError(msg if msg.startswith("第 ") else f"第 {no} 行：{msg}") from exc
        except (TypeError, ValueError, ZeroDivisionError, OverflowError, MemoryError, RecursionError) as exc:
            raise DSLError(f"第 {no} 行：{exc}") from exc
    return {"plots": plots, "hlines": hlines, "shapes": shapes,
            "inputs": {k: overrides.get(k, v) for k, v in sc.inputs.items()}}


def validate_script(src: str) -> dict:
    """Parse + evaluate on synthetic bars; returns the declared inputs."""
    idx = pd.date_range("2020-01-01", periods=120, freq="D", tz="UTC")
    base = 100 + np.sin(np.arange(120) / 6) * 5 + np.arange(120) * 0.1
    df = pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base + 0.3,
                       "volume": np.full(120, 1e6)}, index=idx)
    return run_script(src, df, strict_refs=False)["inputs"]


EXAMPLES = {
    "布林带 + 突破标记": """length = 20
mult = 2
basis = sma(close, length)
dev = mult * stdev(close, length)
plot(basis, "中轨", "#2962ff")
plot(basis + dev, "上轨", "#089981")
plot(basis - dev, "下轨", "#f23645")
plotshape(crossover(close, basis + dev), "突破上轨", "up")
plotshape(crossunder(close, basis - dev), "跌破下轨", "down")""",
    "动量振荡 (副图)": """fast = 12
slow = 26
osc = (ema(close, fast) - ema(close, slow)) / close * 100
plot(osc, "动量 %", "#2962ff", "histogram")
plot(ema(osc, 9), "信号", "#eb6834")
hline(0, "零轴")""",
    "相对强度 vs SPY (副图)": """rs = close / ref_close("SPY")
plot(rs / rs[50] * 100 - 100, "相对 SPY 50 根 %", "#7e57c2")
hline(0, "")""",
    "成交量异动": """n = 20
mult = 2.5
spike = volume > sma(volume, n) * mult
plotshape(spike and close > open, "放量上涨", "up", "#089981")
plotshape(spike and close < open, "放量下跌", "down", "#f23645")
plot(sma(close, 50), "MA50", "#888888")""",
}


def script_refs(src: str) -> set[str]:
    """Symbols used via ref_close("SPY") etc. in a script."""
    from .dsl import REF_FUNCS

    try:
        sc = parse_script(src)
    except DSLError:
        return set()
    out: set[str] = set()
    for _, st in sc.statements:
        for node in ast.walk(st):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in REF_FUNCS
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value.strip().upper())
    return out
