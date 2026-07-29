#!/usr/bin/env python3
"""
Entry point for the real-time Polymarket whitepaper pipeline.

Discovers active markets via the Gamma API, tracks order book data,
selects liquid markets, runs the multi-factor strategy backtest,
performs robustness analysis & parameter sweep, and generates
a professional HTML whitepaper with interactive Plotly charts.

Usage:
    python run_whitepaper_pipeline.py
    python run_whitepaper_pipeline.py --db custom_universe.db --output-dir ./reports
    python run_whitepaper_pipeline.py --top-n 30 --min-score 0.5 --no-sweep
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_project_root = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_project_root, ".env"))

from src.whitepaper.whitepaper_data_collector import WhitepaperDataCollector

logger = logging.getLogger("pipeline")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Real-time Polymarket whitepaper pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db", type=str, default="polymarket_universe.db",
        help="SQLite database path for market data",
    )
    parser.add_argument(
        "--output-dir", type=str, default="./whitepaper_output",
        help="Directory for generated whitepaper HTML/JSON",
    )
    parser.add_argument(
        "--top-n", type=int, default=50,
        help="Number of top markets to select for backtest",
    )
    parser.add_argument(
        "--min-score", type=float, default=0.4,
        help="Minimum liquidity score threshold (0.0 - 1.0)",
    )
    parser.add_argument(
        "--no-sweep", action="store_true",
        help="Skip the parameter sensitivity sweep",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    collector = WhitepaperDataCollector(
        db_path=args.db,
        output_dir=args.output_dir,
        top_n=args.top_n,
        min_score=args.min_score,
        run_sweep=not args.no_sweep,
    )

    results = await collector.collect_all_data()

    if results.errors:
        logger.error("Pipeline completed with %d error(s):", len(results.errors))
        for err in results.errors:
            logger.error("  - %s", err)
        return 1

    logger.info("=" * 60)
    logger.info("PIPELINE SUCCESS")
    logger.info("  Markets discovered: %d", results.markets_discovered)
    logger.info("  Markets tracked:    %d", results.markets_tracked)
    logger.info("  Markets selected:   %d", results.markets_selected)
    logger.info("  Whitepaper:         %s", results.whitepaper_path)
    if results.backtest:
        logger.info("  Backtest PnL:       %s", results.backtest.net_pnl)
        logger.info("  Sharpe ratio:       %.4f", results.backtest.sharpe_ratio)
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
