"""FastAPI dashboard + JSON API."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, settings as default_settings
from ..db import utcnow
from ..report import ReportCooldown, render_markdown
from ..scheduler import Engine
from .trading import build_router

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

    @app.get("/chart")
    def chart_page():
        return FileResponse(STATIC / "chart.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def token_dep(authorization: str | None = Header(None), token: str | None = Query(None)) -> None:
        check_token(authorization, token)

    app.include_router(build_router(engine, Depends(token_dep)))

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
            "report_token_required": bool(settings.report_api_token),
            "last_report": (engine.db.list_reports(1) or [None])[0],
            "market_data": engine.signals.store.provider,
            "signal_refresh_minutes": settings.signal_refresh_minutes,
            "active_strategies": len(engine.db.list_strategies(enabled_only=True)),
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

    def check_token(authorization: str | None, token: str | None) -> None:
        expected = settings.report_api_token
        if not expected:
            return
        given = token or (authorization or "").removeprefix("Bearer ").strip()
        if given != expected:
            raise HTTPException(401, "invalid or missing report API token")

    @app.post("/api/report")
    def create_report(notify: bool = False, force: bool = False,
                      authorization: str | None = Header(None), token: str | None = Query(None)):
        """Fresh data + one Claude call -> stored report. Blocks until done (~10-60s)."""
        check_token(authorization, token)
        try:
            return engine.report(force=force, notify=notify)
        except ReportCooldown as exc:
            return JSONResponse(status_code=429, content={
                "detail": f"上一份报告生成于 {exc.latest['generated_at']}，冷却中",
                "retry_after_s": exc.retry_after_s, "latest_report_id": exc.latest["report_id"]})

    @app.get("/api/reports")
    def reports(limit: int = 30):
        return engine.db.list_reports(min(limit, 200))

    @app.get("/api/report/latest")
    def latest_report():
        rep = engine.db.get_report()
        if not rep:
            raise HTTPException(404, "no report yet")
        return rep

    @app.get("/api/report/{report_id}")
    def get_report(report_id: int, format: str = "json"):
        rep = engine.db.get_report(report_id)
        if not rep:
            raise HTTPException(404, "report not found")
        if format == "md":
            return PlainTextResponse(render_markdown(rep), media_type="text/markdown; charset=utf-8")
        return rep

    @app.post("/api/telegram/test")
    def telegram_test():
        if not settings.telegram_enabled:
            raise HTTPException(400, "TELEGRAM_BOT_TOKEN not configured")
        return {"sent": engine.notifier.send_report()}

    return app
