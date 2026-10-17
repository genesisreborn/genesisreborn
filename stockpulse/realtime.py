"""Intraday real-time price alerts (do not wait for the bar to close).

Two feeds:
- Massive WebSocket (when MASSIVE_API_KEY is set and REALTIME_MODE=ws): per-second
  aggregates ("A.<SYM>") using the Polygon-compatible protocol.
- Polling (default otherwise): the latest price every REALTIME_POLL_SECONDS from the
  configured provider (Yahoo / Massive REST / demo).

Only price-level alerts can be real-time (cross up / cross down / above / below).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from .config import Settings
from .db import Database, iso, utcnow
from .market_data import demo_bars

log = logging.getLogger(__name__)

PRICE_KINDS = ("cross_up", "cross_down", "above", "below")
REFIRE_MINUTES = 15  # "every bar" real-time alerts re-arm after this long


def check_alert(alert: dict, prev: float | None, price: float) -> bool:
    lvl = alert.get("level")
    if lvl is None:
        return False
    kind = alert.get("kind")
    if kind == "cross_up":
        return prev is not None and prev < lvl <= price
    if kind == "cross_down":
        return prev is not None and prev > lvl >= price
    if kind == "above":
        return price > lvl
    if kind == "below":
        return price < lvl
    return False


class RealtimeMonitor:
    def __init__(self, db: Database, settings: Settings, on_fire: Callable[[dict], None],
                 price_fn: Callable[[str], float | None] | None = None):
        self.db = db
        self.settings = settings
        self.on_fire = on_fire
        self.price_fn = price_fn or self._default_price
        self.last: dict[str, float] = {}
        self.status = {"mode": self.mode, "running": False, "symbols": [], "last_tick": None, "error": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def mode(self) -> str:
        m = self.settings.realtime_mode
        if m == "off":
            return "off"
        if m in ("ws", "auto") and self.settings.massive_api_key and not self.settings.demo_mode:
            return "ws"
        return "poll"

    def watched(self) -> list[dict]:
        return [a for a in self.db.list_alerts(active_only=True)
                if a.get("realtime") and a.get("kind") in PRICE_KINDS and a.get("level") is not None]

    # ------------------------------------------------------------ evaluation
    def on_price(self, symbol: str, price: float) -> list[dict]:
        """Feed one price update; fire matching alerts. Safe to call from any feed."""
        symbol = symbol.upper()
        prev = self.last.get(symbol)
        self.last[symbol] = price
        self.status["last_tick"] = iso(utcnow())
        fired = []
        for a in self.watched():
            if a["symbol"] != symbol or not check_alert(a, prev, price):
                continue
            if a.get("mode") == "every_bar" and a.get("last_triggered"):
                last = datetime.strptime(a["last_triggered"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if utcnow() - last < timedelta(minutes=REFIRE_MINUTES):
                    continue
            self.db.mark_alert(a["id"], int(time.time()), triggered=True, deactivate=a.get("mode") == "once")
            event = {**a, "price": round(price, 4), "realtime": True, "at": iso(utcnow())}
            fired.append(event)
            try:
                self.on_fire(event)
            except Exception:  # noqa: BLE001
                log.exception("realtime alert callback failed")
        return fired

    # ------------------------------------------------------------ feeds
    def _default_price(self, symbol: str) -> float | None:
        s = self.settings
        if s.demo_mode:
            return float(demo_bars(symbol, "1m", 3)["close"].iloc[-1])
        if s.massive_api_key:
            r = httpx.get(f"{s.massive_base_url}/v2/last/trade/{symbol}", params={"apiKey": s.massive_api_key}, timeout=10)
            r.raise_for_status()
            res = r.json().get("results") or {}
            return float(res["p"]) if "p" in res else None
        import yfinance as yf

        info = yf.Ticker(symbol).fast_info
        p = info.get("last_price") if hasattr(info, "get") else getattr(info, "last_price", None)
        return float(p) if p else None

    def poll_once(self) -> list[dict]:
        fired = []
        symbols = sorted({a["symbol"] for a in self.watched()})
        self.status["symbols"] = symbols
        for sym in symbols:
            try:
                p = self.price_fn(sym)
            except Exception as exc:  # noqa: BLE001
                self.status["error"] = f"{sym}: {exc}"
                continue
            if p:
                fired += self.on_price(sym, p)
        return fired

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                self.status["error"] = str(exc)
            self._stop.wait(max(5, self.settings.realtime_poll_seconds))

    async def _ws_session(self) -> None:
        import websockets

        url = self.settings.massive_ws_url
        async with websockets.connect(url, ping_interval=20) as ws:
            await ws.send(json.dumps({"action": "auth", "params": self.settings.massive_api_key}))
            subscribed: set[str] = set()
            last_sync = 0.0
            while not self._stop.is_set():
                if time.monotonic() - last_sync > 15:  # follow alert changes
                    want = {a["symbol"] for a in self.watched()}
                    add, drop = want - subscribed, subscribed - want
                    if add:
                        await ws.send(json.dumps({"action": "subscribe", "params": ",".join(f"A.{s}" for s in sorted(add))}))
                    if drop:
                        await ws.send(json.dumps({"action": "unsubscribe", "params": ",".join(f"A.{s}" for s in sorted(drop))}))
                    subscribed = want
                    self.status["symbols"] = sorted(want)
                    last_sync = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                for msg in json.loads(raw):
                    ev = msg.get("ev")
                    if ev == "status" and msg.get("status") in ("auth_failed", "error"):
                        raise RuntimeError(msg.get("message", "auth failed"))
                    if ev in ("A", "AM") and "sym" in msg and "c" in msg:
                        self.on_price(msg["sym"], float(msg["c"]))

    def _ws_loop(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                asyncio.run(self._ws_session())
                backoff = 2
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self.status["error"] = f"websocket: {exc}"
                log.warning("realtime websocket error: %s (retry in %ss)", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def start(self) -> None:
        if self.mode == "off" or self._thread:
            return
        target = self._ws_loop if self.mode == "ws" else self._poll_loop
        self._thread = threading.Thread(target=target, name="realtime", daemon=True)
        self._thread.start()
        self.status["running"] = True
        log.info("realtime alerts started (%s)", self.mode)

    def stop(self) -> None:
        self._stop.set()
        self.status["running"] = False
