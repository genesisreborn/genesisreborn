"""CLI entry point.

    python -m stockpulse serve      # dashboard + scheduler + Telegram bot (default)
    python -m stockpulse once       # run one analysis cycle and print the top list
    python -m stockpulse report     # push the latest brief to Telegram now
"""
from __future__ import annotations

import argparse
import logging
import os

from .config import settings


def main() -> None:
    p = argparse.ArgumentParser(prog="stockpulse")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run dashboard, scheduler and Telegram bot")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("once", help="run one analysis cycle and print results")
    sub.add_parser("report", help="send the latest brief to Telegram subscribers")
    p.add_argument("--demo", action="store_true", help="offline demo data (no network)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.demo:
        settings.demo_mode = True
    if settings.demo_mode and not os.getenv("DB_PATH"):
        settings.db_path = "data/demo.db"  # keep sample data out of the real database

    cmd = args.cmd or "serve"
    if cmd == "serve":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(settings), host=getattr(args, "host", "0.0.0.0"), port=getattr(args, "port", 8000))
    elif cmd == "once":
        from .scheduler import Engine
        from .telegram_bot import format_report

        snap = Engine(settings).refresh()
        print(format_report(snap, limit=15).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    elif cmd == "report":
        from .scheduler import Engine

        eng = Engine(settings)
        print(f"sent to {eng.notifier.send_daily_report()} chat(s)")


if __name__ == "__main__":
    main()
