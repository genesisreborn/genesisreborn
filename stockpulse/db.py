"""SQLite persistence: articles, mentions, snapshots, subscribers, alerts."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id          TEXT PRIMARY KEY,          -- stable hash of url/title
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- news | social
    title       TEXT NOT NULL,
    summary     TEXT,
    url         TEXT,
    published   TEXT NOT NULL,             -- ISO8601 UTC
    sentiment   REAL NOT NULL DEFAULT 0,
    engagement  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);

CREATE TABLE IF NOT EXISTS mentions (
    article_id  TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    published   TEXT NOT NULL,
    PRIMARY KEY (article_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_mentions_ticker ON mentions(ticker, published);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT NOT NULL,
    payload     TEXT NOT NULL              -- JSON: ranking + insights
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT NOT NULL,
    engine      TEXT NOT NULL,             -- claude | rules
    payload     TEXT NOT NULL              -- JSON snapshot incl. insights
);

CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    ts          INTEGER NOT NULL,          -- bar open time, unix seconds UTC
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, ts)
);

CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    config      TEXT NOT NULL,             -- JSON StrategyConfig
    enabled     INTEGER NOT NULL DEFAULT 0,
    updated     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    bar_ts      INTEGER NOT NULL,          -- the closed bar that produced the signal
    action      TEXT NOT NULL,             -- BUY | SELL | SHORT | COVER
    reason      TEXT NOT NULL,
    price       REAL NOT NULL,
    created     TEXT NOT NULL,
    UNIQUE (strategy_id, symbol, interval, bar_ts, action)
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created);

CREATE TABLE IF NOT EXISTS alerts_cfg (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    config      TEXT NOT NULL,             -- JSON Alert
    active      INTEGER NOT NULL DEFAULT 1,
    created     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     TEXT PRIMARY KEY,
    created     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    ticker      TEXT NOT NULL,
    day         TEXT NOT NULL,
    PRIMARY KEY (ticker, day)
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- articles ----
    def add_article(self, art: dict[str, Any], tickers: set[str]) -> bool:
        """Insert article + its ticker mentions. Returns False if already present."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO articles(id, source, kind, title, summary, url, published, sentiment, engagement)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (art["id"], art["source"], art["kind"], art["title"], art.get("summary", ""), art.get("url", ""),
                 art["published"], art.get("sentiment", 0.0), art.get("engagement", 0.0)),
            )
            if cur.rowcount == 0:
                return False
            c.executemany(
                "INSERT OR IGNORE INTO mentions(article_id, ticker, published) VALUES (?,?,?)",
                [(art["id"], t, art["published"]) for t in tickers],
            )
            return True

    def mention_stats(self, since: datetime) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT m.ticker AS ticker,
                   COUNT(*) AS mentions,
                   COUNT(DISTINCT a.source) AS sources,
                   AVG(a.sentiment) AS sentiment,
                   SUM(CASE WHEN a.kind='news' THEN 1 ELSE 0 END) AS news,
                   SUM(CASE WHEN a.kind='social' THEN 1 ELSE 0 END) AS social,
                   SUM(a.engagement) AS engagement
            FROM mentions m JOIN articles a ON a.id = m.article_id
            WHERE m.published >= ?
            GROUP BY m.ticker
            """,
            (iso(since),),
        )

    def mention_counts_between(self, start: datetime, end: datetime) -> dict[str, int]:
        rows = self.query(
            "SELECT ticker, COUNT(*) AS n FROM mentions WHERE published >= ? AND published < ? GROUP BY ticker",
            (iso(start), iso(end)),
        )
        return {r["ticker"]: r["n"] for r in rows}

    def articles_for(self, ticker: str, since: datetime, limit: int = 20) -> list[dict]:
        rows = self.query(
            """
            SELECT a.* FROM articles a JOIN mentions m ON m.article_id = a.id
            WHERE m.ticker = ? AND a.published >= ?
            ORDER BY a.published DESC LIMIT ?
            """,
            (ticker, iso(since), limit),
        )
        return [dict(r) for r in rows]

    def latest_articles(self, limit: int = 50) -> list[dict]:
        rows = self.query(
            """
            SELECT a.*, GROUP_CONCAT(m.ticker) AS tickers
            FROM articles a LEFT JOIN mentions m ON m.article_id = a.id
            GROUP BY a.id ORDER BY a.published DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def prune(self, keep_days: int = 30) -> None:
        cutoff = iso(utcnow() - timedelta(days=keep_days))
        with self.tx() as c:
            c.execute("DELETE FROM mentions WHERE published < ?", (cutoff,))
            c.execute("DELETE FROM articles WHERE published < ?", (cutoff,))
            c.execute("DELETE FROM snapshots WHERE created < ?", (cutoff,))
            # intraday bars are bulky; daily/weekly history is kept
            c.execute("DELETE FROM bars WHERE interval IN ('1m','5m','15m','30m') AND ts < ?",
                      (int((utcnow() - timedelta(days=90)).timestamp()),))

    # ---- snapshots ----
    def save_snapshot(self, payload: dict) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO snapshots(created, payload) VALUES (?, ?)",
                            (payload["generated_at"], json.dumps(payload, ensure_ascii=False)))
            return int(cur.lastrowid)

    def latest_snapshot(self) -> dict | None:
        rows = self.query("SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1")
        return json.loads(rows[0]["payload"]) if rows else None

    def score_history(self, ticker: str, limit: int = 96) -> list[dict]:
        rows = self.query("SELECT created, payload FROM snapshots ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in reversed(rows):
            for item in json.loads(r["payload"]).get("ranking", []):
                if item["ticker"] == ticker:
                    out.append({"t": r["created"], "score": item["score"], "price": item.get("price")})
                    break
        return out

    # ---- reports ----
    def save_report(self, snap: dict) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO reports(created, engine, payload) VALUES (?, ?, ?)",
                            (snap["generated_at"], snap.get("insight_engine") or "rules",
                             json.dumps(snap, ensure_ascii=False)))
            return int(cur.lastrowid)

    def get_report(self, report_id: int | None = None) -> dict | None:
        if report_id is None:
            rows = self.query("SELECT id, payload FROM reports ORDER BY id DESC LIMIT 1")
        else:
            rows = self.query("SELECT id, payload FROM reports WHERE id = ?", (report_id,))
        if not rows:
            return None
        return {**json.loads(rows[0]["payload"]), "report_id": rows[0]["id"]}

    def list_reports(self, limit: int = 30) -> list[dict]:
        rows = self.query("SELECT id, created, engine FROM reports ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- strategies ----
    def save_strategy(self, cfg: dict) -> int:
        data = json.dumps(cfg, ensure_ascii=False)
        with self.tx() as c:
            if cfg.get("id"):
                cur = c.execute("UPDATE strategies SET name=?, config=?, enabled=?, updated=? WHERE id=?",
                                (cfg["name"], data, int(bool(cfg.get("enabled"))), iso(utcnow()), cfg["id"]))
                if cur.rowcount:
                    return int(cfg["id"])
            cur = c.execute("INSERT INTO strategies(name, config, enabled, updated) VALUES (?,?,?,?)",
                            (cfg["name"], data, int(bool(cfg.get("enabled"))), iso(utcnow())))
            new_id = int(cur.lastrowid)
            cfg = {**cfg, "id": new_id}
            c.execute("UPDATE strategies SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), new_id))
            return new_id

    def get_strategy(self, sid: int) -> dict | None:
        rows = self.query("SELECT config FROM strategies WHERE id=?", (sid,))
        return json.loads(rows[0]["config"]) if rows else None

    def list_strategies(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT config FROM strategies" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        return [json.loads(r["config"]) for r in self.query(sql)]

    def delete_strategy(self, sid: int) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM strategies WHERE id=?", (sid,)).rowcount > 0

    # ---- signals ----
    def add_signal(self, sig: dict) -> int | None:
        """Insert a signal; returns its id, or None if this exact signal was already recorded."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO signals(strategy_id, strategy, symbol, interval, bar_ts, action, reason, price, created)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (sig["strategy_id"], sig["strategy"], sig["symbol"], sig["interval"], sig["bar_ts"], sig["action"],
                 sig["reason"], sig["price"], iso(utcnow())))
            return int(cur.lastrowid) if cur.rowcount else None

    def list_signals(self, limit: int = 100, symbol: str | None = None) -> list[dict]:
        if symbol:
            rows = self.query("SELECT * FROM signals WHERE symbol=? ORDER BY bar_ts DESC, id DESC LIMIT ?", (symbol, limit))
        else:
            rows = self.query("SELECT * FROM signals ORDER BY created DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- price / condition alerts ----
    def save_alert(self, cfg: dict) -> int:
        with self.tx() as c:
            if cfg.get("id"):
                cur = c.execute("UPDATE alerts_cfg SET config=?, active=? WHERE id=?",
                                (json.dumps(cfg, ensure_ascii=False), int(bool(cfg.get("active", True))), cfg["id"]))
                if cur.rowcount:
                    return int(cfg["id"])
            cur = c.execute("INSERT INTO alerts_cfg(config, active, created) VALUES (?,?,?)",
                            ("{}", int(bool(cfg.get("active", True))), iso(utcnow())))
            new_id = int(cur.lastrowid)
            c.execute("UPDATE alerts_cfg SET config=? WHERE id=?", (json.dumps({**cfg, "id": new_id}, ensure_ascii=False), new_id))
            return new_id

    def list_alerts(self, active_only: bool = False, symbol: str | None = None) -> list[dict]:
        sql, params = "SELECT config, active FROM alerts_cfg", []
        conds = []
        if active_only:
            conds.append("active=1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        out = []
        for r in self.query(sql + " ORDER BY id DESC", params):
            d = {**json.loads(r["config"]), "active": bool(r["active"])}
            if symbol is None or d.get("symbol") == symbol:
                out.append(d)
        return out

    def mark_alert(self, alert_id: int, bar_ts: int, triggered: bool, deactivate: bool) -> None:
        rows = self.query("SELECT config FROM alerts_cfg WHERE id=?", (alert_id,))
        if not rows:
            return
        cfg = json.loads(rows[0]["config"])
        cfg["last_bar_ts"] = bar_ts
        if triggered:
            cfg["last_triggered"] = iso(utcnow())
        if deactivate:
            cfg["active"] = False
        with self.tx() as c:
            c.execute("UPDATE alerts_cfg SET config=?, active=? WHERE id=?",
                      (json.dumps(cfg, ensure_ascii=False), 0 if deactivate else int(bool(cfg.get("active", True))), alert_id))

    def delete_alert(self, alert_id: int) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM alerts_cfg WHERE id=?", (alert_id,)).rowcount > 0

    # ---- watchlist (chart page) ----
    def get_watchlist(self, default: list[str]) -> list[str]:
        raw = self.get_kv("chart_watchlist")
        return json.loads(raw) if raw else list(default)

    def set_watchlist(self, symbols: list[str]) -> None:
        self.set_kv("chart_watchlist", json.dumps(symbols))

    # ---- telegram ----
    def add_subscriber(self, chat_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO subscribers(chat_id, created) VALUES (?, ?)", (chat_id, iso(utcnow())))

    def remove_subscriber(self, chat_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    def subscribers(self) -> list[str]:
        return [r["chat_id"] for r in self.query("SELECT chat_id FROM subscribers")]

    def mark_alerted(self, ticker: str, day: str) -> bool:
        """Returns True if this is the first alert for ticker on that day."""
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO alerts(ticker, day) VALUES (?, ?)", (ticker, day))
            return cur.rowcount == 1

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_kv(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))
