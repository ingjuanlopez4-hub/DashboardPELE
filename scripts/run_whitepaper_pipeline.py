#!/usr/bin/env python3
"""
Polymarket Liquidity-Weighted Strategy — Whitepaper Pipeline

Orchestrates the complete pipeline:
  1. Market discovery via Gamma API
  2. Market tracking and order book analysis
  3. Liquidity-based market selection
  4. Strategy backtest (Wick-Fishing + FinBERT + Monte Carlo)
  5. Parameter sweep (grid search)
  6. Robustness analysis (permutation test, MC equity, OOS test)
  7. Professional whitepaper generation (HTML + Plotly)

Usage:
    python scripts/run_whitepaper_pipeline.py [--db DB_PATH] [--output OUTPUT_DIR]
        [--top-n TOP_N] [--min-score MIN_SCORE] [--no-sweep]
        [--log-level LOG_LEVEL]

Examples:
    # Run full pipeline with defaults (top 50 markets, >0.4 score)
    python scripts/run_whitepaper_pipeline.py

    # Quick run (top 10 markets, skip parameter sweep, debug logging)
    python scripts/run_whitepaper_pipeline.py --top-n 10 --no-sweep --log-level DEBUG

    # Custom paths
    python scripts/run_whitepaper_pipeline.py --db ./data/markets.db --output ./reports
"""

import argparse
import asyncio
import logging
import os
import sys
import time

# Ensure project root is on sys.path so `from src` imports work
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
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Liquidity-Weighted Strategy Whitepaper Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_whitepaper_pipeline.py
  python scripts/run_whitepaper_pipeline.py --top-n 10 --no-sweep --log-level DEBUG
  python scripts/run_whitepaper_pipeline.py --db ./data/markets.db --output ./reports
        """,
    )
    parser.add_argument(
        "--db",
        default="polymarket_universe.db",
        help="Path to SQLite database (default: polymarket_universe.db)",
    )
    parser.add_argument(
        "--output",
        default="./whitepaper_output",
        help="Output directory for whitepaper (default: ./whitepaper_output)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="Number of top markets to select for backtest (default: 50)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.4,
        help="Minimum liquidity score threshold (default: 0.4)",
    )
    parser.add_argument(
        "--no-sweep",
        action="store_true",
        help="Skip parameter sweep (speeds up pipeline)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate imports and configuration without running API calls",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("pipeline")

    logger.info("=" * 60)
    logger.info("Polymarket Strategy Whitepaper Pipeline v1.0")
    logger.info("=" * 60)
    logger.info("Configuration:")
    logger.info("  Database:       %s", args.db)
    logger.info("  Output:         %s", args.output)
    logger.info("  Top N markets:  %d", args.top_n)
    logger.info("  Min score:      %.2f", args.min_score)
    logger.info("  Parameter sweep: %s", "disabled" if args.no_sweep else "enabled")
    logger.info("  Dry run:        %s", args.dry_run)

    if args.dry_run:
        logger.info("Dry-run mode: validating imports and configuration...")

        required_modules = [
            "src.data.database",
            "src.data.market_discovery",
            "src.data.market_tracker",
            "src.data.market_selector",
            "src.whitepaper.strategy_runner",
            "src.whitepaper.parameter_sweep",
            "src.whitepaper.robustness_analyzer",
            "src.whitepaper.whitepaper_generator",
            "src.whitepaper.whitepaper_data_collector",
        ]

        import importlib
        for mod_name in required_modules:
            try:
                importlib.import_module(mod_name)
                logger.info("  [OK] %s", mod_name)
            except Exception as exc:
                logger.error("  [FAIL] %s: %s", mod_name, exc)
                return 1

        logger.info("Dry-run validation complete — all modules load successfully.")
        return 0

    from src.whitepaper.whitepaper_data_collector import WhitepaperDataCollector

    collector = WhitepaperDataCollector(
        db_path=args.db,
        output_dir=args.output,
        top_n=args.top_n,
        min_score=args.min_score,
        run_sweep=not args.no_sweep,
    )

    start_time = time.time()

    try:
        results = await collector.collect_all_data()
        elapsed = time.time() - start_time

        logger.info("=" * 60)
        logger.info("PIPELINE FINISHED in %.1f seconds", elapsed)
        logger.info("  Markets discovered: %d", results.markets_discovered)
        logger.info("  Markets tracked:    %d", results.markets_tracked)
        logger.info("  Markets selected:   %d", results.markets_selected)

        if results.backtest:
            bt = results.backtest
            logger.info("  Net PnL:            %s", bt.net_pnl)
            logger.info("  Sharpe ratio:       %.4f", bt.sharpe_ratio)
            logger.info("  Max drawdown:       %s%%", bt.max_drawdown_pct)
            logger.info("  Win rate:           %.1f%%", bt.win_rate * 100)
            logger.info("  Total trades:       %d", bt.total_trades)

        if results.sweep and results.sweep.optimal_params:
            logger.info("  Optimal params:     %s", results.sweep.optimal_params)

        if results.whitepaper_path:
            logger.info("  Whitepaper:         %s", results.whitepaper_path)

        if results.errors:
            logger.warning("  Errors (%d):", len(results.errors))
            for e in results.errors:
                logger.warning("    - %s", e)
            return 1

        logger.info("Pipeline completed successfully!")
        return 0

    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
