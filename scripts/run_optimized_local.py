#!/usr/bin/env python3
"""
run_optimized_local.py — Optimized PELE Bot startup script.

Loads all performance optimizations before starting the bot:
  1. uvloop event loop (fallback to asyncio on Windows)
  2. CPU core affinity (pin processes to specific cores)
  3. OS-level performance variables (PYTHONHASHSEED, MALLOC, etc.)
  4. High-performance logging configuration
  5. System resource limits (ulimit -n 65536)

Usage:
    python scripts/run_optimized_local.py [--dry-run] [--markets BTC,ETH,SOL]
"""

import argparse
import logging
import os
import platform
import signal
import sys
from decimal import Decimal

# ── Add project root to path ──────────────────────────────────────────
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

# ── Step 1: Set OS-level optimization variables BEFORE any imports ────
os.environ.setdefault("PYTHONHASHSEED", "0")
os.environ.setdefault("PYTHONOPTIMIZE", "2")
os.environ.setdefault("PYTHONFAULTHANDLER", "1")
os.environ.setdefault("PYTHONMALLOC", "malloc")
os.environ.setdefault("MALLOC_TRIM_THRESHOLD_", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# ── Step 2: Install uvloop (Linux/macOS only; fallback to asyncio) ────
if platform.system() != "Windows":
    try:
        import uvloop

        uvloop.install()
        print("[OPTIMIZATION] uvloop installed — event loop throughput 2-4x")
    except ImportError:
        print("[OPTIMIZATION] uvloop not available — using standard asyncio")
else:
    print("[OPTIMIZATION] Windows detected — skipping uvloop, using standard asyncio")

# ── Step 3: Now safe to import the rest ───────────────────────────────
import asyncio

from dotenv import load_dotenv

load_dotenv(os.path.join(_project_root, ".env"))

from src.infrastructure.event_loop import (
    pin_by_role,
    configure_event_loop,
    apply_system_limits,
)
from src.config.optimization_settings import LOCAL_OPTIMIZATION_CONFIG
from estrategia import MotorEstrategia
from ejecucion import EjecutorOrdenes
from ingesta import IngestaCLOB
from src.config.live_settings import get_live_config, RISK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    force=True,
)
logger = logging.getLogger("run_optimized")

# Suppress noisy loggers
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PELE Optimized Bot — Polymarket Event Liquidity Engine",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate orders without sending to CLOB",
    )
    parser.add_argument(
        "--markets",
        type=str,
        default="",
        help="Comma-separated asset IDs (default: all via discovery)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="bot_state.db",
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--no-core-affinity",
        action="store_true",
        help="Disable CPU core affinity",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Enable cProfile profiling (writes profile.prof on exit)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    """Main async entry point with all optimizations applied."""
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("ingesta").setLevel(logging.DEBUG)
        logging.getLogger("ejecucion").setLevel(logging.DEBUG)
        logging.getLogger("estrategia").setLevel(logging.DEBUG)

    # ── Core affinity (pin to cores 0-3 by default) ──────────────────
    if not args.no_core_affinity:
        pin_by_role("ingestion")
    else:
        logger.info("Core affinity disabled via --no-core-affinity")

    # ── Configure system limits ───────────────────────────────────────
    apply_system_limits()

    # ── Optimize asyncio event loop settings ──────────────────────────
    configure_event_loop()

    # ── Parse market IDs ──────────────────────────────────────────────
    asset_ids: list[str] | None = None
    if args.markets:
        asset_ids = [m.strip() for m in args.markets.split(",") if m.strip()]
        logger.info("Markets: %d asset(s) specified", len(asset_ids))

    # ── Setup signal queues ───────────────────────────────────────────
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    execution_log_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)

    # ── Ingesta (WebSocket data ingestion) ────────────────────────────
    ingesta = IngestaCLOB(
        asset_ids=asset_ids,
        private_key=os.environ.get("PRIVATE_KEY"),
        chain_id=int(os.environ.get("POLYGON_CHAIN_ID", "137")),
    )

    # ── Estrategia (strategy engine with dynamic weights) ─────────────
    estrategia = MotorEstrategia(
        event_queue=ingesta.queue,
        signal_queue=signal_queue,
        execution_log_queue=execution_log_queue,
    )

    # ── Ejecucion (maker-first order execution) ──────────────────────
    ejecutor = EjecutorOrdenes(
        signal_queue=signal_queue,
        dry_run=args.dry_run,
        execution_log_queue=execution_log_queue,
        db_path=args.db,
    )

    # Wire ingesta disconnect/reconnect callbacks to execution
    ingesta.set_disconnect_callback(
        lambda: ejecutor.update_ws_health({"connected": False, "book_synced": False})
    )
    ingesta.set_reconnect_callback(
        lambda: ejecutor.update_ws_health({"connected": True, "book_synced": True})
    )

    # ── Profiling (optional) ──────────────────────────────────────────
    profiler = None
    if args.profile:
        from src.infrastructure.bot_profiler import BotProfiler

        profiler = BotProfiler(enabled=True)
        profiler.start()
        logger.info("cProfile profiling enabled — writing profile.prof on exit")

    # ── Startup banner ────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PELE BOT — OPTIMIZED LOCAL STARTUP")
    logger.info(f"  Dry run:      {args.dry_run}")
    logger.info(f"  DB path:      {args.db}")
    logger.info(f"  Markets:      {asset_ids or 'auto-discovery'}")
    logger.info(f"  Core affinity: {'enabled' if not args.no_core_affinity else 'disabled'}")
    logger.info(f"  Event loop:   {'uvloop' if 'uvloop' in sys.modules else 'asyncio'}")
    logger.info(f"  Platform:     {platform.system()} {platform.machine()}")
    logger.info(f"  Python:       {sys.version}")
    logger.info("=" * 60)

    # ── Startup validation ────────────────────────────────────────────
    if args.dry_run:
        logger.info("DRY RUN mode — no real orders will be placed")
    else:
        missing: list[str] = []
        if not os.environ.get("PRIVATE_KEY"):
            missing.append("PRIVATE_KEY")
        if not os.environ.get("POLYMARKET_API_KEY"):
            missing.append("POLYMARKET_API_KEY")
        if not os.environ.get("POLYMARKET_SECRET"):
            missing.append("POLYMARKET_SECRET")
        if not os.environ.get("POLYMARKET_PASSPHRASE"):
            missing.append("POLYMARKET_PASSPHRASE")
        if missing:
            logger.error("Missing required env vars: %s", ", ".join(missing))
            logger.error("Run with --dry-run to test without live funds")
            return 1

    # ── Run all components concurrently ───────────────────────────────
    tasks = [
        asyncio.create_task(ingesta.run(), name="ingesta"),
        asyncio.create_task(estrategia.run(), name="estrategia"),
        asyncio.create_task(ejecutor.run(), name="ejecucion"),
    ]

    # ── Graceful shutdown ─────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Signal received — initiating graceful shutdown...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            logger.debug("Signal handler not available for %s", sig)

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down all components...")

        # Stop components in order
        ingesta.stop()
        estrategia.stop()
        ejecutor.stop()

        # Wait for tasks to finish with timeout
        done, pending = await asyncio.wait(tasks, timeout=10.0)
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if profiler:
            profile_output = profiler.stop()
            with open("profile.prof", "w") as f:
                f.write(profile_output)
            logger.info("Profile written to profile.prof")
            # Print top functions
            print("\n=== TOP 20 SLOWEST FUNCTIONS ===")
            print(profile_output[:5000])

        logger.info("Shutdown complete. Goodbye.")
        return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
