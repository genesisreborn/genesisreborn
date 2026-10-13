"""Turn the ranking into human-readable views.

Periodic refreshes only use the free rule engine. Claude is called once per
report (see report.py), never on the refresh loop.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "market_overview": {"type": "string"},
        "top_picks": {"type": "array", "items": {"type": "string"}},
        "outlook": {"type": "string"},
        "risk_warning": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "view": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
                    "confidence": {"type": "integer"},
                    "catalyst": {"type": "string"},
                    "risks": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["ticker", "view", "confidence", "catalyst", "risks", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "market_overview", "themes", "top_picks", "outlook", "risk_warning", "stocks"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是一名谨慎的美股市场分析师，为个人投资者撰写简明的中文盘面观察。
你会收到一个由程序计算出的"热门股"列表：每只股票附带资讯/社交讨论热度、热度变化倍数、情绪分数(-1~1)、
价格与成交量数据，以及最近的标题。请基于这些证据写一份当日报告：
- headline：一句话标题，点出今天最重要的热点。
- market_overview：3-5 句话概括当前市场热点与情绪。
- themes：3-5 个正在发酵的主题（如"AI 算力"、"量子计算"）。
- stocks：对每只股票给出 view(bullish/neutral/bearish)、confidence(0-100)、catalyst(驱动因素)、
  risks(主要风险)、summary(一句话看法)。
- top_picks：从列表中挑出 1-5 只"热度与基本面消息共振、短期上涨概率相对较高"的股票代码；没有合适的就给空数组。
- outlook：接下来 1-2 个交易日值得关注的事件、板块或价位信号。
- risk_warning：当前最需要警惕的风险（炒作过热、负面消息、追高等）。
要求：只依据给出的数据与标题推断，不要编造未提供的财务数字或事件；热度高但情绪偏空或已大涨的要明确提示追高/回调风险；
社交媒体炒作要与基本面新闻区分开。全部用简体中文。"""


def _payload(ranking: list[dict]) -> str:
    rows = []
    for r in ranking:
        rows.append({
            "ticker": r["ticker"], "score": r["score"], "mentions_24h": r["mentions"],
            "news": r["news"], "social": r["social"], "buzz_velocity_x": r["velocity"],
            "sentiment": r["sentiment"], "price": r.get("price"), "change_1d_pct": r.get("change_1d"),
            "change_5d_pct": r.get("change_5d"), "volume_vs_20d_avg": r.get("volume_ratio"),
            "headlines": [h["title"] for h in r.get("headlines", [])[:6]],
        })
    return json.dumps(rows, ensure_ascii=False)


def claude_insights(ranking: list[dict], settings: Settings) -> dict[str, Any] | None:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.beta.messages.create(
            model=settings.claude_model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": INSIGHT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "今日热门股数据：\n" + _payload(ranking)}],
        )
    except anthropic.RateLimitError as exc:
        log.warning("Claude rate limited: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API error %s: %s", exc.status_code, exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("Claude connection error: %s", exc)
        return None

    if response.stop_reason == "refusal":
        log.warning("Claude declined the insight request")
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Claude returned non-JSON insight output (stop_reason=%s)", response.stop_reason)
        return None
    data["engine"] = "claude"
    return data


VIEW_BY_SIGNAL = {"强势看涨": "bullish", "看涨": "bullish", "热度高·偏空": "bearish", "追高风险": "neutral"}


def rule_insights(ranking: list[dict]) -> dict[str, Any]:
    """Deterministic fallback when no LLM is configured."""
    stocks = []
    for r in ranking:
        view = VIEW_BY_SIGNAL.get(r["signal"], "neutral")
        parts = []
        if r["velocity"] >= 3:
            parts.append(f"讨论量是近期均值的 {r['velocity']:.1f} 倍")
        if r.get("volume_ratio") and r["volume_ratio"] >= 1.8:
            parts.append(f"成交量放大至 20 日均量的 {r['volume_ratio']:.1f} 倍")
        if r.get("change_1d") is not None:
            parts.append(f"日涨跌 {r['change_1d']:+.2f}%")
        tone = "偏多" if r["sentiment"] > 0.15 else "偏空" if r["sentiment"] < -0.15 else "中性"
        parts.append(f"舆论情绪{tone}({r['sentiment']:+.2f})")
        risks = []
        if r.get("change_5d") is not None and r["change_5d"] > 15:
            risks.append("5 日涨幅较大，留意获利回吐")
        if r["social"] > r["news"] * 2:
            risks.append("热度主要来自社交媒体，炒作成分较高")
        if r["sentiment"] < -0.15:
            risks.append("负面消息占比高")
        headline = r["headlines"][0]["title"] if r.get("headlines") else ""
        stocks.append({
            "ticker": r["ticker"], "view": view, "confidence": int(min(90, r["score"])),
            "catalyst": headline or "资讯热度上升", "risks": "；".join(risks) or "暂无明显风险信号",
            "summary": "，".join(parts) + "。",
        })
    bulls = [s["ticker"] for s in stocks if s["view"] == "bullish"][:5]
    overview = (f"当前共追踪 {len(ranking)} 只热门股，" +
                (f"看涨信号最强的是 {', '.join(bulls)}。" if bulls else "暂无明显强势看涨信号。") +
                "（规则引擎生成；配置 ANTHROPIC_API_KEY 可获得 AI 深度点评）")
    picks = [s["ticker"] for s in stocks if s["view"] == "bullish" and s["confidence"] >= 70][:5]
    return {"headline": f"热度榜前三：{', '.join(r['ticker'] for r in ranking[:3])}" if ranking else "暂无热门股",
            "market_overview": overview, "themes": [], "top_picks": picks, "outlook": "",
            "risk_warning": "热度主要反映讨论量与舆论情绪，不代表价格一定上涨。", "stocks": stocks, "engine": "rules"}


def build_insights(ranking: list[dict], settings: Settings, use_claude: bool = False) -> dict[str, Any]:
    if use_claude and settings.claude_enabled and ranking:
        got = claude_insights(ranking, settings)
        if got:
            return got
    return rule_insights(ranking)
