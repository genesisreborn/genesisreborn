"""AI report: one fresh analysis cycle + one Claude call -> stored, rendered report."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import Database, utcnow
from .pipeline import run_cycle

log = logging.getLogger(__name__)

VIEW_ZH = {"bullish": "🟢 看涨", "neutral": "🟡 中性", "bearish": "🔴 看空"}
_report_lock = threading.Lock()


class ReportCooldown(Exception):
    def __init__(self, latest: dict, retry_after_s: int):
        super().__init__(f"report generated recently; retry in {retry_after_s}s")
        self.latest = latest
        self.retry_after_s = retry_after_s


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def generate_report(db: Database, settings: Settings, force: bool = False) -> dict:
    """Refresh data, ask Claude once, store and return the report.

    Without ``force``, a request inside the cooldown window raises ReportCooldown
    instead of spending another Claude call.
    """
    with _report_lock:
        latest = db.get_report()
        if latest and not force:
            age = utcnow() - _parse(latest["generated_at"])
            window = timedelta(minutes=settings.report_cooldown_minutes)
            if age < window:
                raise ReportCooldown(latest, int((window - age).total_seconds()))

        snap = run_cycle(db, settings, use_claude=True)
        snap["report_id"] = db.save_report(snap)
        path = write_markdown(snap, settings.reports_dir)
        log.info("report #%s generated (engine=%s) -> %s", snap["report_id"], snap.get("insight_engine"), path)
        return snap


def write_markdown(snap: dict, directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    stamp = snap["generated_at"].replace(":", "").replace("-", "").rstrip("Z")
    path = os.path.join(directory, f"report_{stamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_markdown(snap))
    return path


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def render_markdown(snap: dict) -> str:
    ins = snap.get("insights") or {}
    engine = "Claude" if snap.get("insight_engine") == "claude" else "规则引擎"
    out = [f"# StockPulse 热门股报告", "",
           f"生成时间：{snap['generated_at']} UTC · 分析引擎：{engine} · 追踪 {snap.get('tracked', 0)} 只股票", ""]
    if ins.get("headline"):
        out += [f"## {ins['headline']}", ""]
    if snap.get("market_overview"):
        out += ["### 市场概览", "", snap["market_overview"], ""]
    if snap.get("themes"):
        out += ["### 热点主题", "", *[f"- {t}" for t in snap["themes"]], ""]
    if ins.get("top_picks"):
        out += ["### 重点关注", "", "、".join(ins["top_picks"]), ""]

    out += ["### 热门股榜单", "",
            "| # | 代码 | 热度 | 信号 | 价格 | 日涨跌 | 5日 | 提及 | 情绪 | 观点 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(snap.get("ranking", []), 1):
        v = (r.get("insight") or {}).get("view", "neutral")
        price = f"${r['price']:.2f}" if r.get("price") else "—"
        out.append(f"| {i} | **{r['ticker']}** | {r['score']:.0f} | {r['signal']} | {price} | {_pct(r.get('change_1d'))} "
                   f"| {_pct(r.get('change_5d'))} | {r['mentions']} | {r['sentiment']:+.2f} | {VIEW_ZH.get(v, v)} |")
    out.append("")

    out += ["### 个股点评", ""]
    for r in snap.get("ranking", []):
        si = r.get("insight") or {}
        if not si:
            continue
        out += [f"#### {r['ticker']} · {VIEW_ZH.get(si.get('view'), '')}（信心 {si.get('confidence')}）", "",
                si.get("summary", ""), "",
                f"- 催化剂：{si.get('catalyst', '')}", f"- 风险：{si.get('risks', '')}"]
        for h in r.get("headlines", [])[:3]:
            out.append(f"- [{h['title']}]({h['url']}) _{h['source']}_")
        out.append("")

    if ins.get("outlook"):
        out += ["### 后市关注", "", ins["outlook"], ""]
    if ins.get("risk_warning"):
        out += ["### 风险提示", "", ins["risk_warning"], ""]
    out += ["---", "", "_本报告由程序根据公开资讯自动生成，仅供参考，不构成投资建议。_", ""]
    return "\n".join(out)
