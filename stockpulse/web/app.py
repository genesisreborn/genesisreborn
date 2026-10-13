"""FastAPI dashboard + JSON API."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..config import Settings, settings as default_settings
from ..db import utcnow
from ..scheduler import Engine

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    settings = settings or default_settings
    engine = Engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            engine.start()
        yield
        engine.shutdown()

    app = FastAPI(title="StockPulse", lifespan=lifespan)
    app.state.engine = engine
    refreshing = threading.Event()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/snapshot")
    def snapshot():
        snap = engine.db.latest_snapshot()
        if not snap:
            return {"generated_at": None, "ranking": [], "watchlist": [], "refreshing": refreshing.is_set()}
        return {**snap, "refreshing": refreshing.is_set()}

    @app.get("/api/stock/{ticker}")
    def stock(ticker: str):
        t = ticker.upper()
        snap = engine.db.latest_snapshot() or {}
        row = next((r for r in snap.get("ranking", []) + snap.get("watchlist", []) if r["ticker"] == t), None)
        arts = engine.db.articles_for(t, utcnow() - timedelta(days=3), limit=30)
        if row is None and not arts:
            raise HTTPException(404, f"{t} not tracked")
        return {"ticker": t, "current": row, "history": engine.db.score_history(t), "articles": arts}

    @app.get("/api/news")
    def news(limit: int = 60):
        return engine.db.latest_articles(min(limit, 200))

    @app.get("/api/status")
    def status():
        jobs = [{"id": j.id, "next_run": str(j.next_run_time)} for j in engine.scheduler.get_jobs()]
        return {
            "telegram": settings.telegram_enabled, "claude": settings.claude_enabled, "demo": settings.demo_mode,
            "subscribers": len(engine.notifier.recipients()), "refresh_minutes": settings.refresh_minutes,
            "daily_report_times": settings.daily_report_times, "timezone": settings.timezone, "jobs": jobs,
        }

    @app.post("/api/refresh")
    def refresh():
        if refreshing.is_set():
            return {"started": False, "reason": "already running"}

        def run():
            refreshing.set()
            try:
                engine.refresh()
            except Exception:  # noqa: BLE001 - logged by engine
                pass
            finally:
                refreshing.clear()

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @app.post("/api/telegram/test")
    def telegram_test():
        if not settings.telegram_enabled:
            raise HTTPException(400, "TELEGRAM_BOT_TOKEN not configured")
        return {"sent": engine.notifier.send_daily_report()}

    return app
