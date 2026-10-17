"""Real order execution through Alpaca, behind a risk gate.

Safety model (all must pass before any order is sent):
1. BROKER=alpaca and API keys configured.
2. ALPACA_PAPER=true (Alpaca's own paper environment) unless the operator also sets
   LIVE_TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY. Without it, live mode refuses to trade.
3. API_TOKEN is set (so nobody can change strategies over the web without it) and
   the kill switch (dashboard / Telegram /pause) is off.
4. Only strategies with execution="broker", only their listed symbols.
5. Per-order notional cap, daily order cap, open position cap, shorts off by default.
6. client_order_id is derived from the signal id, so a retry can never double-submit.
Orders are market orders; long entries carry a broker-side stop (OTO) when the
strategy has a stop, so protection does not depend on this program staying up.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import httpx

from .config import Settings
from .db import Database, iso, utcnow
from .strategy.backtest import position_size

log = logging.getLogger(__name__)

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL, mode TEXT NOT NULL,
    signal_id INTEGER, strategy_id INTEGER, strategy TEXT,
    symbol TEXT NOT NULL, action TEXT NOT NULL, qty REAL,
    status TEXT NOT NULL,          -- submitted | rejected | error | skipped
    broker_order_id TEXT, client_order_id TEXT, detail TEXT
);
"""


class BrokerError(RuntimeError):
    pass


class AlpacaClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.paper = settings.alpaca_paper
        self.base = "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"
        self.http = client or httpx.Client(timeout=20)
        self.headers = {"APCA-API-KEY-ID": settings.alpaca_key_id, "APCA-API-SECRET-KEY": settings.alpaca_secret_key}

    def _req(self, method: str, path: str, **kw):
        r = self.http.request(method, self.base + path, headers=self.headers, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise BrokerError(f"Alpaca {r.status_code}: {msg}")
        return r.json() if r.content else {}

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self._req("GET", "/v2/positions")

    def orders(self, limit: int = 50) -> list[dict]:
        return self._req("GET", "/v2/orders", params={"status": "all", "limit": limit, "direction": "desc"})

    def submit_order(self, symbol: str, qty: int, side: str, client_order_id: str, stop_price: float | None = None) -> dict:
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day",
                "client_order_id": client_order_id}
        if stop_price:
            body.update({"order_class": "oto", "stop_loss": {"stop_price": f"{stop_price:.2f}"}})
        return self._req("POST", "/v2/orders", json=body)

    def close_position(self, symbol: str) -> dict:
        return self._req("DELETE", f"/v2/positions/{symbol}")

    def cancel_all(self) -> None:
        self._req("DELETE", "/v2/orders")


class BrokerExecutor:
    def __init__(self, db: Database, settings: Settings, client: AlpacaClient | None = None):
        self.db = db
        self.settings = settings
        self._client = client
        with db.tx() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------ state
    @property
    def configured(self) -> bool:
        s = self.settings
        return s.broker == "alpaca" and bool(s.alpaca_key_id and s.alpaca_secret_key)

    @property
    def mode(self) -> str:
        return "paper" if self.settings.alpaca_paper else "live"

    @property
    def live_unlocked(self) -> bool:
        return self.settings.alpaca_paper or self.settings.live_trading_confirm == LIVE_CONFIRM_PHRASE

    @property
    def paused(self) -> bool:
        return self.db.get_kv("broker_paused", "0") == "1"

    def set_paused(self, paused: bool) -> None:
        self.db.set_kv("broker_paused", "1" if paused else "0")

    @property
    def client(self) -> AlpacaClient:
        if self._client is None:
            self._client = AlpacaClient(self.settings)
        return self._client

    def blocked_reason(self) -> str | None:
        if not self.configured:
            return "未配置券商（BROKER=alpaca 和 API Key）"
        if not self.settings.report_api_token:
            return "启用自动交易前必须设置 API_TOKEN（防止他人通过网页修改策略或下单）"
        if not self.live_unlocked:
            return f"实盘模式未解锁：需要设置 LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE}"
        if self.paused:
            return "自动交易已暂停"
        return None

    def orders_today(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = self.db.query("SELECT COUNT(*) AS n FROM broker_orders WHERE status='submitted' AND action IN ('BUY','SHORT')"
                          " AND created LIKE ?", (day + "%",))
        return r[0]["n"]

    def _log(self, sig: dict, status: str, qty=None, broker_id=None, coid=None, detail="") -> dict:
        rec = {"created": iso(utcnow()), "mode": self.mode, "signal_id": sig.get("id"), "strategy_id": sig.get("strategy_id"),
               "strategy": sig.get("strategy"), "symbol": sig["symbol"], "action": sig["action"], "qty": qty,
               "status": status, "broker_order_id": broker_id, "client_order_id": coid, "detail": detail[:500]}
        with self.db.tx() as c:
            c.execute("INSERT INTO broker_orders(created, mode, signal_id, strategy_id, strategy, symbol, action, qty, status,"
                      " broker_order_id, client_order_id, detail) VALUES (:created,:mode,:signal_id,:strategy_id,:strategy,"
                      ":symbol,:action,:qty,:status,:broker_order_id,:client_order_id,:detail)", rec)
        level = logging.INFO if status == "submitted" else logging.WARNING
        log.log(level, "broker %s %s %s: %s %s", status, sig["action"], sig["symbol"], qty, detail)
        return rec

    # ------------------------------------------------------------ execution
    def execute(self, sig: dict, symbols_allowed: list[str], alloc_pct: float) -> dict:
        """Turn one live signal into a broker order, or explain why not."""
        if sig.get("reason") in ("stop", "take_profit"):
            # Broker-side OTO stops handle protective exits; the strategy's simulated stop is informational.
            return self._log(sig, "skipped", detail="止损/止盈由券商端止损单处理")
        reason = self.blocked_reason()
        if reason:
            return self._log(sig, "rejected", detail=reason)
        s = self.settings
        sym = sig["symbol"]
        if sig.get("id") and self.db.query("SELECT 1 FROM broker_orders WHERE signal_id=? AND action=? AND status='submitted'",
                                           (sig["id"], sig["action"])):
            return self._log(sig, "skipped", detail="重复提交，已忽略")
        if sym not in symbols_allowed:
            return self._log(sig, "rejected", detail="代码不在策略的监控列表里")
        if sig["action"] in ("BUY", "SHORT") and self.orders_today() >= s.max_daily_orders:
            return self._log(sig, "rejected", detail=f"已达每日下单上限 {s.max_daily_orders}")
        if sig["action"] == "SHORT" and not s.allow_short:
            return self._log(sig, "rejected", detail="未开启做空（ALLOW_SHORT=false）")
        coid = f"sp-{sig.get('id') or int(datetime.now().timestamp())}-{sig['action'].lower()}"
        try:
            positions = {p["symbol"]: p for p in self.client.positions()}
            held = positions.get(sym)
            held_qty = float(held["qty"]) if held else 0.0

            if sig["action"] in ("SELL", "COVER"):
                if (sig["action"] == "SELL" and held_qty <= 0) or (sig["action"] == "COVER" and held_qty >= 0):
                    return self._log(sig, "skipped", detail="券商账户里没有对应持仓")
                o = self.client.close_position(sym)
                return self._log(sig, "submitted", qty=abs(held_qty), broker_id=o.get("id"), coid=coid, detail="平仓")

            if held_qty != 0:
                return self._log(sig, "skipped", detail="已持有该代码")
            if len(positions) >= s.max_open_positions:
                return self._log(sig, "rejected", detail=f"持仓数已达上限 {s.max_open_positions}")
            acct = self.client.account()
            if acct.get("trading_blocked") or acct.get("account_blocked"):
                return self._log(sig, "rejected", detail="券商账户被限制交易")
            equity = float(acct.get("equity") or 0)
            price = float(sig["price"])
            # risk-based size when the strategy asks for it (needs a stop), always within the caps
            sized = position_size(equity, price, sig.get("stop"), float(sig.get("risk_pct") or 0), alloc_pct) * price
            notional = min(sized, s.max_order_notional)
            qty = int(math.floor(notional / price)) if price > 0 else 0
            if qty < 1:
                return self._log(sig, "rejected", detail=f"金额 {notional:.0f} 不够买 1 股（价格 {price}）")
            stop = sig.get("stop") if sig["action"] == "BUY" else None
            side = "buy" if sig["action"] == "BUY" else "sell"
            o = self.client.submit_order(sym, qty, side, coid, stop_price=stop)
            return self._log(sig, "submitted", qty=qty, broker_id=o.get("id"), coid=coid,
                             detail=f"市价单{'，附止损 ' + str(stop) if stop else ''}")
        except BrokerError as exc:
            if "client_order_id" in str(exc) and "unique" in str(exc):
                return self._log(sig, "skipped", coid=coid, detail="重复提交，已忽略")
            return self._log(sig, "error", coid=coid, detail=str(exc))
        except httpx.HTTPError as exc:
            return self._log(sig, "error", coid=coid, detail=f"网络错误：{exc}")

    def flatten_all(self) -> dict:
        """Emergency: cancel open orders and close every position, then pause."""
        self.set_paused(True)
        if not self.configured:
            return {"ok": False, "detail": "未配置券商"}
        try:
            self.client.cancel_all()
            closed = [p["symbol"] for p in self.client.positions()]
            for sym in closed:
                self.client.close_position(sym)
            return {"ok": True, "closed": closed}
        except (BrokerError, httpx.HTTPError) as exc:
            return {"ok": False, "detail": str(exc)}

    def status(self) -> dict:
        out = {"configured": self.configured, "mode": self.mode, "live_unlocked": self.live_unlocked,
               "paused": self.paused, "blocked_reason": self.blocked_reason(),
               "limits": {"max_order_notional": self.settings.max_order_notional,
                          "max_daily_orders": self.settings.max_daily_orders,
                          "max_open_positions": self.settings.max_open_positions,
                          "allow_short": self.settings.allow_short},
               "orders_today": self.orders_today(),
               "log": [dict(r) for r in self.db.query("SELECT * FROM broker_orders ORDER BY id DESC LIMIT 50")]}
        if self.configured:
            try:
                a = self.client.account()
                out["account"] = {k: a.get(k) for k in ("equity", "cash", "buying_power", "status", "currency")}
                out["positions"] = [{k: p.get(k) for k in ("symbol", "qty", "side", "avg_entry_price", "current_price",
                                                           "market_value", "unrealized_pl", "unrealized_plpc")}
                                    for p in self.client.positions()]
            except (BrokerError, httpx.HTTPError) as exc:
                out["error"] = str(exc)
        return out
