"""Daily pre-market brief: market, your portfolio, watchlist movers, today's earnings and macro data.

Sent to the Telegram owners (it contains your holdings) at BRIEF_TIMES on weekdays, and on /brief.
Every section is optional: one failing data source just drops that section.
"""
from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .calendar_data import get_calendar
from .config import Settings
from .db import Database
from .market_data import BarStore
from .portfolio import Portfolio

log = logging.getLogger(__name__)

MARKET = [("SPY", "标普"), ("QQQ", "纳指"), ("IWM", "罗素"), ("^VIX", "VIX")]
MAJOR_EVENTS = ("CPI", "PCE", "Nonfarm", "非农", "Payroll", "FOMC", "Fed", "利率", "GDP", "Unemployment", "失业",
                "Retail Sales", "零售", "ISM", "PPI", "Jobless", "初请")


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _p(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else ''}{v:.2f}%"


def _last_change(store: BarStore, sym: str, update: bool) -> tuple[float | None, float | None]:
    if update:
        store.maybe_update(sym, "1d", min_gap_s=300)
    df = store.get(sym, "1d", 5, refresh_if_empty=update)
    if len(df) < 2:
        return None, None
    last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
    return last, (last / prev - 1) * 100


def build_brief(db: Database, settings: Settings, store: BarStore, portfolio: Portfolio, update: bool = True) -> dict:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    sections: dict[str, list[str]] = {}
    watch = list(db.get_watchlist(settings.watchlist))

    try:
        sections["market"] = [f"{name} {_p(_last_change(store, sym, update)[1])}" for sym, name in MARKET]
    except Exception as exc:  # noqa: BLE001
        log.warning("brief market failed: %s", exc)

    holdings: list[str] = []
    try:
        pf = portfolio.summary(update=update, history=False)
        holdings = [p["symbol"] for p in pf["positions"]]
        if holdings:
            t = pf["totals"]
            lines = [f"市值 {t['market_value']:,.0f}，今日 {t['day_change']:+,.0f}（{_p(t['day_change_pct'])}），"
                     f"浮动盈亏 {t['unrealized']:+,.0f}（{_p(t['unrealized_pct'])}）"]
            movers = sorted(pf["positions"], key=lambda p: -abs(p["day_change_pct"] or 0))[:3]
            lines += [f"{_e(p['symbol'])} {_p(p['day_change_pct'])}" for p in movers]
            sections["portfolio"] = lines
    except Exception as exc:  # noqa: BLE001
        log.warning("brief portfolio failed: %s", exc)

    try:
        moves = []
        for sym in watch[:40]:
            last, chg = _last_change(store, sym, update)
            if chg is not None:
                moves.append((chg, sym, last))
        moves.sort()
        if moves:
            ups = [f"{_e(s)} {_p(c)}" for c, s, _ in reversed(moves[-3:]) if c > 0]
            downs = [f"{_e(s)} {_p(c)}" for c, s, _ in moves[:3] if c < 0]
            sections["watch"] = [("涨：" + "，".join(ups)) if ups else "", ("跌：" + "，".join(downs)) if downs else ""]
            sections["watch"] = [x for x in sections["watch"] if x]
    except Exception as exc:  # noqa: BLE001
        log.warning("brief watchlist failed: %s", exc)

    try:
        today = now.date()
        cal = get_calendar(db, settings, today - timedelta(days=today.weekday()), 7, watch + holdings)
        mine = set(watch) | set(holdings)
        horizon = {today, today + timedelta(days=1)}
        earn = [e for e in cal["earnings"] if e["symbol"] in mine
                and datetime.fromtimestamp(e["time"], tz).date() in horizon]
        if earn:
            timing = {"BMO": "盘前", "AMC": "盘后"}
            sections["earnings"] = [
                f"{_e(e['symbol'])} {datetime.fromtimestamp(e['time'], tz):%m-%d} {timing.get(e['timing'], '')}"
                f"{'，预期 EPS ' + format(e['estimate'], '.2f') if e.get('estimate') is not None else ''}" for e in earn]
        econ = [e for e in cal["economic"] if datetime.fromtimestamp(e["time"], tz).date() == today
                and (e.get("region") in (None, "US") and any(k.lower() in e["event"].lower() for k in MAJOR_EVENTS))]
        if econ:
            sections["economic"] = [f"{datetime.fromtimestamp(e['time'], tz):%H:%M} {_e(e['event'])}"
                                    f"{'，预期 ' + format(e['expected'], 'g') if e.get('expected') is not None else ''}"
                                    for e in econ[:8]]
    except Exception as exc:  # noqa: BLE001
        log.warning("brief calendar failed: %s", exc)

    try:
        since = int((now - timedelta(days=3)).timestamp())
        sigs = [s for s in db.list_signals(50) if (s.get("bar_ts") or 0) >= since][:8]
        if sigs:
            sections["signals"] = [f"{_e(s['symbol'])} {_e(s['action'])} · {_e(s['strategy'])} · {_e(s['interval'])}" for s in sigs]
    except Exception as exc:  # noqa: BLE001
        log.warning("brief signals failed: %s", exc)

    titles = {"market": "📊 大盘", "portfolio": "💼 我的持仓", "watch": "👀 自选异动", "earnings": "📣 今明两天财报",
              "economic": "🏛 今天的重要数据", "signals": "🤖 近期策略信号"}
    parts = [f"<b>☀️ StockPulse 盘前简报</b>  {now:%Y-%m-%d %a}"]
    for key, title in titles.items():
        if sections.get(key):
            parts.append(f"\n<b>{title}</b>\n" + "\n".join("• " + line for line in sections[key]))
    if len(parts) == 1:
        parts.append("\n暂时没有数据。")
    return {"sections": sections, "text": "\n".join(parts), "date": date.isoformat(now.date())}
