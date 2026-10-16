"""Telegram delivery: daily briefs, intraday alerts, and simple bot commands."""
from __future__ import annotations

import html
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

VIEW_ICON = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}
DISCLAIMER = "<i>⚠️ 以上内容由程序根据公开资讯自动生成，仅供参考，不构成投资建议。</i>"
REPORT_TITLE = "📊 StockPulse 每日 AI 报告"
MAX_LEN = 4000  # Telegram hard limit is 4096


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def format_stock_line(i: int, r: dict) -> str:
    ins = r.get("insight") or {}
    icon = VIEW_ICON.get(ins.get("view", "neutral"), "🟡")
    price = f"${r['price']:.2f}" if r.get("price") else "—"
    line = (f"{i}. {icon} <b>{_e(r['ticker'])}</b> 热度 <b>{r['score']:.0f}</b> · {_e(r['signal'])}\n"
            f"   {price} ({_pct(r.get('change_1d'))}) · 提及 {r['mentions']} · 情绪 {r['sentiment']:+.2f}")
    if ins.get("summary"):
        line += f"\n   💬 {_e(ins['summary'])}"
    return line


def format_report(snap: dict, limit: int = 10, title: str = "📈 StockPulse 热门股简报") -> str:
    ins = snap.get("insights") or {}
    parts = [f"<b>{title}</b>", f"<i>{_e(snap['generated_at'])} UTC</i>", ""]
    if ins.get("headline"):
        parts += [f"<b>{_e(ins['headline'])}</b>", ""]
    if snap.get("market_overview"):
        parts += [f"🧭 {_e(snap['market_overview'])}", ""]
    if snap.get("themes"):
        parts += ["🔥 主题：" + " · ".join(_e(t) for t in snap["themes"]), ""]
    if ins.get("top_picks"):
        parts += ["⭐ 重点关注：" + "、".join(_e(t) for t in ins["top_picks"]), ""]
    for i, r in enumerate(snap.get("ranking", [])[:limit], 1):
        parts.append(format_stock_line(i, r))
    if ins.get("outlook"):
        parts += ["", f"🔭 <b>后市关注</b>：{_e(ins['outlook'])}"]
    if ins.get("risk_warning"):
        parts += ["", f"⚠️ <b>风险提示</b>：{_e(ins['risk_warning'])}"]
    parts += ["", DISCLAIMER]
    return "\n".join(parts)


def format_stock_detail(r: dict) -> str:
    ins = r.get("insight") or {}
    lines = [
        f"<b>{_e(r['ticker'])}</b> · 热度 {r['score']:.0f} · {_e(r['signal'])}",
        f"价格 {('$%.2f' % r['price']) if r.get('price') else '—'} · 日 {_pct(r.get('change_1d'))} · 5日 {_pct(r.get('change_5d'))}",
        f"量比 {r.get('volume_ratio') or '—'} · 24h 提及 {r['mentions']}（新闻 {r['news']} / 社交 {r['social']}）· 热度倍数 {r['velocity']}x",
    ]
    if ins:
        lines += ["", f"观点：{VIEW_ICON.get(ins.get('view'), '')} {_e(ins.get('view'))}（信心 {ins.get('confidence')}）",
                  f"催化剂：{_e(ins.get('catalyst'))}", f"风险：{_e(ins.get('risks'))}", f"💬 {_e(ins.get('summary'))}"]
    if r.get("headlines"):
        lines += ["", "<b>最新资讯</b>"]
        for h in r["headlines"][:5]:
            lines.append(f"• <a href=\"{_e(h['url'])}\">{_e(h['title'])}</a> <i>({_e(h['source'])})</i>")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


SIGNAL_ICON = {"BUY": "🟢", "SELL": "🔵", "SHORT": "🔴", "COVER": "🟣"}


def format_signal(sig: dict) -> str:
    from .strategy.backtest import ACTION_ZH, REASON_ZH
    from .strategy.signals import bar_time_str

    lines = [f"{SIGNAL_ICON.get(sig['action'], '⚪')} <b>{_e(ACTION_ZH.get(sig['action'], sig['action']))}信号 · "
             f"{_e(sig['symbol'])}</b>（{_e(sig['interval'])}）",
             f"策略：{_e(sig['strategy'])}",
             f"触发：{_e(REASON_ZH.get(sig['reason'], sig['reason']))} · K 线 {_e(bar_time_str(sig['bar_ts'], sig['interval']))}"]
    if sig["reason"] in ("stop", "take_profit"):
        lines.append(f"成交价约 {sig['price']}")
    else:
        lines.append(f"收盘价 {sig['price']} → 按策略在下一根 K 线开盘执行")
    if sig.get("stop"):
        lines.append(f"参考止损 {sig['stop']}")
    if sig.get("target"):
        lines.append(f"参考止盈 {sig['target']}")
    lines += ["", "<i>⚠️ 策略信号由程序按你设定的规则自动生成，仅供参考，不构成投资建议。</i>"]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, settings: Settings, db: Database, client: httpx.Client | None = None):
        self.settings = settings
        self.db = db
        self.client = client or httpx.Client(timeout=40)
        self.base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self._stop = threading.Event()

    # ---- sending ----
    def recipients(self) -> list[str]:
        return sorted(set(self.settings.telegram_chat_ids) | set(self.db.subscribers()))

    def send(self, chat_id: str, text: str) -> bool:
        if not self.settings.telegram_enabled:
            return False
        try:
            for chunk in _chunks(text):
                r = self.client.post(f"{self.base}/sendMessage", json={
                    "chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True,
                })
                if r.status_code == 403:  # user blocked the bot
                    self.db.remove_subscriber(chat_id)
                    return False
                r.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("telegram send to %s failed: %s", chat_id, exc)
            return False

    def broadcast(self, text: str) -> int:
        return sum(self.send(c, text) for c in self.recipients())

    def send_report(self, snap: dict | None = None) -> int:
        """Broadcast a report (defaults to the latest stored one; never calls Claude)."""
        snap = snap or self.db.get_report() or self.db.latest_snapshot()
        if not snap:
            return 0
        return self.broadcast(format_report(snap, title=REPORT_TITLE if snap.get("report_id") else
                                            "📈 StockPulse 热门股简报"))

    def send_signal(self, sig: dict) -> int:
        return self.broadcast(format_signal(sig))

    def check_alerts(self, snap: dict) -> list[str]:
        """Alert on tickers newly entering the top N above the score threshold (once per day)."""
        today = datetime.now(ZoneInfo(self.settings.timezone)).strftime("%Y-%m-%d")
        fired = []
        for r in snap.get("ranking", [])[: self.settings.alert_top_n]:
            if r["score"] < self.settings.alert_min_score:
                continue
            if self.db.mark_alerted(r["ticker"], today):
                fired.append(r["ticker"])
                self.broadcast("🚨 <b>热度异动提醒</b>\n\n" + format_stock_detail(r))
        return fired

    # ---- commands (long polling) ----
    def handle_command(self, chat_id: str, text: str) -> str:
        cmd, _, arg = text.strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        snap = self.db.latest_snapshot()
        if cmd in ("/start", "/subscribe"):
            self.db.add_subscriber(chat_id)
            return ("✅ 已订阅 StockPulse！你会收到每日热门股简报和盘中异动提醒。\n\n"
                    "/report 最新 AI 报告\n/signals 最近交易信号\n/top 实时热门榜\n/stock TSLA 查看个股\n/stop 取消订阅")
        if cmd in ("/stop", "/unsubscribe"):
            self.db.remove_subscriber(chat_id)
            return "已取消订阅。发送 /start 可重新订阅。"
        if cmd == "/report":
            rep = self.db.get_report()
            return format_report(rep, title=REPORT_TITLE) if rep else "还没有生成过 AI 报告，到点会自动推送。"
        if cmd == "/signals":
            from .strategy.signals import describe_signal

            sigs = self.db.list_signals(10)
            if not sigs:
                return "还没有交易信号。在网页的「策略」页启用策略后，新信号会自动推送。"
            return "<b>最近的策略信号</b>\n" + "\n".join(
                f"• {_e(describe_signal(s))} · {_e(s['strategy'])} · {_e(s['interval'])}" for s in sigs)
        if cmd == "/top":
            return format_report(snap) if snap else "数据还在生成中，请稍后再试。"
        if cmd == "/stock":
            t = arg.strip().upper().lstrip("$")
            if not t:
                return "用法：/stock NVDA"
            for r in (snap or {}).get("ranking", []) + (snap or {}).get("watchlist", []):
                if r["ticker"] == t:
                    return format_stock_detail(r)
            return f"{_e(t)} 目前不在热门榜/关注列表中。"
        return "可用命令：/start /report /signals /top /stock TICKER /stop"

    def poll_forever(self) -> None:
        if not self.settings.telegram_enabled:
            return
        offset = int(self.db.get_kv("tg_offset", "0") or 0)
        while not self._stop.is_set():
            try:
                r = self.client.get(f"{self.base}/getUpdates", params={"timeout": 30, "offset": offset})
                r.raise_for_status()
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    text = msg.get("text", "")
                    if text.startswith("/"):
                        chat_id = str(msg["chat"]["id"])
                        self.send(chat_id, self.handle_command(chat_id, text))
                self.db.set_kv("tg_offset", str(offset))
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram polling error: %s", exc)
                time.sleep(5)

    def start_polling(self) -> threading.Thread:
        th = threading.Thread(target=self.poll_forever, name="telegram-poll", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        out.append(cur)
    return out
