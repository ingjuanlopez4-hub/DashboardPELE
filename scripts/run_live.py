#!/usr/bin/env python3
"""
PELE — Live Trading Entry Point.

Orchestrates the three modules:
  Module A (IngestaCLOB) — WebSocket event ingestion
  Module B (MotorEstrategia) — Signal generation
  Module C (EjecutorOrdenes) — Order execution

Usage:
  python scripts/run_live.py --dry-run          # simulated orders
  python scripts/run_live.py                    # live orders (requires env vars)
  python scripts/run_live.py --db ./bot.db --log-level DEBUG

Exit codes:
  0: Normal shutdown
  1: Startup error
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal

from dotenv import load_dotenv

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

load_dotenv(os.path.join(_project_root, ".env"))

from estrategia import MotorEstrategia
from ejecucion import EjecutorOrdenes
from ingesta import IngestaCLOB

logger = logging.getLogger("run_live")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PELE — Polymarket live trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_live.py --dry-run
  python scripts/run_live.py --db ./data/bot_state.db --log-level DEBUG
  python scripts/run_live.py
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run in dry-run mode (default: True; set DRY_RUN=false env var for live)",
    )
    parser.add_argument(
        "--db",
        default="bot_state.db",
        help="SQLite path for circuit breaker state (default: bot_state.db)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    dry_run = args.dry_run
    env_dry_run = os.environ.get("DRY_RUN", "true").lower()
    if env_dry_run in ("false", "0", "no"):
        dry_run = False

    if not dry_run:
        missing = [
            v
            for v in [
                "PRIVATE_KEY",
                "POLYMARKET_API_KEY",
                "POLYMARKET_SECRET",
                "POLYMARKET_PASSPHRASE",
            ]
            if not os.environ.get(v)
        ]
        if missing:
            logger.error(
                "Missing required env vars for live trading: %s", ", ".join(missing)
            )
            logger.error(
                "PRIVATE_KEY is needed for WebSocket L1 auth; "
                "API_KEY/SECRET/PASSPHRASE are needed for REST order execution"
            )
            logger.error("Run with --dry-run to test without live funds")
            return 1

    logger.info("=" * 50)
    logger.info("PELE — Starting live trading bot")
    logger.info("  Dry run: %s", dry_run)
    logger.info("  DB path: %s", args.db)
    logger.info("=" * 50)

    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    ingesta = IngestaCLOB()
    estrategia = MotorEstrategia(
        event_queue=ingesta.queue,
        signal_queue=signal_queue,
        history_db_path=args.db,
    )
    ejecutor = EjecutorOrdenes(
        signal_queue=signal_queue,
        dry_run=dry_run,
        db_path=args.db,
    )

    async def shutdown(sig: str) -> None:
        logger.info("Received signal %s — shutting down gracefully...", sig)
        ingesta.stop()
        estrategia.stop()
        ejecutor.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(shutdown(s.name))
            )
        except NotImplementedError:
            pass

    try:
        tasks = [
            asyncio.create_task(ingesta.run(), name="ingesta"),
            asyncio.create_task(estrategia.run(), name="estrategia"),
            asyncio.create_task(ejecutor.run(), name="ejecutor"),
        ]

        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )

        for t in done:
            exc = t.exception()
            if exc:
                logger.error("Task %s failed: %s", t.get_name(), exc)

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    except asyncio.CancelledError:
        pass
    finally:
        ingesta.stop()
        estrategia.stop()
        ejecutor.stop()
        logger.info("PELE shutdown complete")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
