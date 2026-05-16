#!/usr/bin/env python3
"""
Pipeline entry point that orchestrates market discovery, selection,
and backtesting with real FinBERT sentiment analysis and news fetching.

Usage:
    python scripts/run_pipeline.py [--db DB_PATH] [--top-n TOP_N]
        [--min-score MIN_SCORE] [--concurrency N] [--log-level LOG_LEVEL]

Examples:
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --top-n 20 --log-level DEBUG
    python scripts/run_pipeline.py --db ./data/markets.db --concurrency 3
"""

import argparse
import asyncio
import logging
import os
import sys
import time

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
        description="Polymarket Pipeline with Real Sentiment Analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default="polymarket_universe.db",
        help="Path to SQLite database (default: polymarket_universe.db)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="Number of top markets to select (default: 50)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.4,
        help="Minimum liquidity score threshold (default: 0.4)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max parallel news fetches for sentiment (default: 5)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("pipeline")

    logger.info("=" * 60)
    logger.info("Polymarket Pipeline — Real Sentiment Backtest")
    logger.info("=" * 60)
    logger.info("Database:      %s", args.db)
    logger.info("Top N markets: %d", args.top_n)
    logger.info("Min score:     %.2f", args.min_score)
    logger.info("Concurrency:   %d", args.concurrency)

    # ── Phase 1: Database ────────────────────────────────────────────
    from src.data.database import PolymarketDatabase
    db = await PolymarketDatabase.create(args.db)
    logger.info("Database initialized")

    # ── Phase 2: Market Discovery ────────────────────────────────────
    from src.data.market_discovery import MarketDiscoveryManager
    discovery = MarketDiscoveryManager(db)
    markets = await discovery.discover_all_active_markets()
    logger.info("Discovered %d active markets", len(markets))

    # ── Phase 3: Market Tracking ─────────────────────────────────────
    from src.data.market_tracker import MarketTracker
    tracker = MarketTracker(db, discovery)
    await tracker.run_once()
    tracked = list(tracker.tracked_markets.values())
    logger.info("Tracked %d markets", len(tracked))

    # ── Phase 4: Market Selection ────────────────────────────────────
    from src.data.market_selector import MarketSelector
    selector = MarketSelector()
    sel_result = selector.select_top_markets(
        tracked,
        top_n=args.top_n,
        min_score=args.min_score,
    )
    selected = sel_result.selected
    logger.info("Selected %d markets for backtest", len(selected))

    for tm in selected[:5]:
        logger.info("  Top: %s (score=%.4f)", tm.market.question[:40], tm.liquidity_score)

    # ── Phase 5: Sentiment Analyzer & News Fetcher ───────────────────
    from src.strategy.finbert_sentiment import FinBERTSentimentAnalyzer
    from src.strategy.news_fetcher import NewsFetcher

    sentiment_analyzer = FinBERTSentimentAnalyzer()
    news_fetcher = NewsFetcher()

    logger.info("Sentiment analyzer and news fetcher created")

    # ── Phase 6: Backtest ────────────────────────────────────────────
    from src.whitepaper.strategy_runner import StrategyBacktestRunner

    runner = StrategyBacktestRunner(
        db=db,
        sentiment_analyzer=sentiment_analyzer,
        news_fetcher=news_fetcher,
        sentiment_concurrency=args.concurrency,
    )

    start_time = time.time()
    backtest_result = await runner.run_backtest(selected)
    elapsed = time.time() - start_time

    logger.info("=" * 60)
    logger.info("BACKTEST COMPLETE in %.1f seconds", elapsed)
    logger.info("  Net PnL:            %s", backtest_result.net_pnl)
    logger.info("  Sharpe ratio:       %.4f", backtest_result.sharpe_ratio)
    logger.info("  Sortino ratio:      %.4f", backtest_result.sortino_ratio)
    logger.info("  Max drawdown:       %s%%", backtest_result.max_drawdown_pct)
    logger.info("  Win rate:           %.1f%%", backtest_result.win_rate * 100)
    logger.info("  Total trades:       %d", backtest_result.total_trades)
    logger.info("  Profit factor:      %.4f", backtest_result.profit_factor)
    logger.info("=" * 60)

    # ── Cleanup ──────────────────────────────────────────────────────
    await news_fetcher.close()
    logger.info("News fetcher closed")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
