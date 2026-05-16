#!/usr/bin/env python3
"""
PELE — Predictive Event Liquidity Engine
CLI para Polymarket trading bot.

Uso:
    pele whitepaper [opciones]    # Ejecuta pipeline de whitepaper
    pele live [opciones]          # Ejecuta trading en vivo
    pele backtest [opciones]      # Ejecuta backtest
    pele check [opciones]         # Ejecuta pre-flight checklist

Ejemplos:
    pele whitepaper --top-n 20 --no-sweep
    pele live --dry-run
    pele backtest --db ./data/markets.db --top-n 30
    pele check
"""

import argparse
import asyncio
import logging
import os
import sys


_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PELE — Polymarket Predictive Event Liquidity Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Comando a ejecutar")

    # ── whitepaper ────────────────────────────────────────────────────
    wp = subparsers.add_parser("whitepaper", help="Ejecuta pipeline completo de whitepaper")
    wp.add_argument("--db", default="polymarket_universe.db", help="Ruta a SQLite")
    wp.add_argument("--output", default="./whitepaper_output", help="Directorio de salida")
    wp.add_argument("--top-n", type=int, default=50, help="Top N mercados")
    wp.add_argument("--min-score", type=float, default=0.4, help="Score mínimo")
    wp.add_argument("--no-sweep", action="store_true", help="Saltar parameter sweep")
    wp.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    wp.add_argument("--dry-run", action="store_true", help="Validar imports sin ejecutar")

    # ── live ──────────────────────────────────────────────────────────
    lv = subparsers.add_parser("live", help="Inicia modo trading en vivo")
    lv.add_argument("--dry-run", action="store_true", default=True, help="Órdenes simuladas")
    lv.add_argument("--db", default="bot_state.db", help="Ruta a SQLite")
    lv.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # ── backtest ──────────────────────────────────────────────────────
    bt = subparsers.add_parser("backtest", help="Ejecuta backtest con datos reales")
    bt.add_argument("--db", default="polymarket_universe.db", help="Ruta a SQLite")
    bt.add_argument("--top-n", type=int, default=50, help="Top N mercados")
    bt.add_argument("--min-score", type=float, default=0.4, help="Score mínimo")
    bt.add_argument("--concurrency", type=int, default=5, help="News fetcher concurrencia")
    bt.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # ── check ─────────────────────────────────────────────────────────
    ch = subparsers.add_parser("check", help="Ejecuta pre-flight checklist")
    ch.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ch.add_argument("--skip-ws", action="store_true", help="Saltar verificación WebSocket")
    ch.add_argument("--skip-chain", action="store_true", help="Saltar verificaciones on-chain")

    return parser


async def cmd_whitepaper(args: argparse.Namespace) -> int:
    from src.whitepaper.whitepaper_data_collector import WhitepaperDataCollector

    collector = WhitepaperDataCollector(
        db_path=args.db,
        output_dir=args.output,
        top_n=args.top_n,
        min_score=args.min_score,
        run_sweep=not args.no_sweep,
    )
    if args.dry_run:
        logging.getLogger("pipeline").info("Dry-run: --dry-run no soportado en whitepaper, usa run_whitepaper_pipeline.py")
        return 0
    results = await collector.collect_all_data()
    if results.errors:
        for e in results.errors:
            logging.error("Error: %s", e)
        return 1
    return 0


async def cmd_live(args: argparse.Namespace) -> int:
    dry_run = args.dry_run
    env_dry_run = os.environ.get("DRY_RUN", "true").lower()
    if env_dry_run in ("false", "0", "no"):
        dry_run = False

    from src.live.preflight import run_preflight, print_preflight_summary

    if not dry_run:
        preflight = await run_preflight()
        print(print_preflight_summary(preflight))
        if not preflight.passed:
            return 2

    from scripts.run_live import main as live_main
    return await live_main()


async def cmd_backtest(args: argparse.Namespace) -> int:
    from scripts.run_pipeline import main as pipeline_main
    sys.argv = ["run_pipeline", "--db", args.db, "--top-n", str(args.top_n),
                "--min-score", str(args.min_score), "--concurrency", str(args.concurrency),
                "--log-level", args.log_level]
    return await pipeline_main()


async def cmd_check(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    from src.live.preflight import run_preflight, print_preflight_summary
    result = await run_preflight()
    print(print_preflight_summary(result))
    return 0 if result.passed else 2


COMMANDS = {
    "whitepaper": cmd_whitepaper,
    "live": cmd_live,
    "backtest": cmd_backtest,
    "check": cmd_check,
}


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    setup_logging(getattr(args, "log_level", "INFO"))

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return await handler(args)
    except KeyboardInterrupt:
        logging.getLogger("pele").info("Interrupción del usuario")
        return 130
    except Exception as exc:
        logging.getLogger("pele").exception("Error en comando %s: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
