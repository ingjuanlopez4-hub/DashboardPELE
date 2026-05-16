#!/usr/bin/env python3
import argparse
import asyncio
import logging
import os
import sys
import time
from decimal import Decimal
from typing import Any

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

from src.data.database import MarketInfo, PolymarketDatabase  # noqa: E402


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
        description="Polymarket Market Analysis Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_market_analysis.py
  python scripts/run_market_analysis.py --db ./data/polymarket.db --output ./reports
  python scripts/run_market_analysis.py --top-n 20 --min-score 0.5 --log-level DEBUG
        """,
    )
    parser.add_argument(
        "--db",
        default="polymarket_universe.db",
        help="SQLite database path (default: polymarket_universe.db)",
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
        help="Number of top markets to select (default: 50)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.4,
        help="Minimum liquidity score threshold (default: 0.4)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=50000.0,
        help="Minimum volume in USDC (default: 50000)",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=25000.0,
        help="Minimum liquidity in USDC (default: 25000)",
    )
    parser.add_argument(
        "--min-days",
        type=int,
        default=14,
        help="Minimum days to resolution (default: 14)",
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
        help="Skip API calls, use sample data for testing",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("market_analysis")

    logger.info("=" * 60)
    logger.info("Polymarket Market Analysis Pipeline v1.0")
    logger.info("=" * 60)
    logger.info("Configuration:")
    logger.info("  DB:              %s", args.db)
    logger.info("  Output:          %s", args.output)
    logger.info("  Top N:           %d", args.top_n)
    logger.info("  Min score:       %.2f", args.min_score)
    logger.info("  Min volume:      $%.0f", args.min_volume)
    logger.info("  Min liquidity:   $%.0f", args.min_liquidity)
    logger.info("  Min days:        %d", args.min_days)
    logger.info("  Dry run:         %s", args.dry_run)

    import aiohttp

    from src.data.gamma_client import GammaClient
    from src.data.liquidity_analyzer import LiquidityAnalyzer
    from src.data.market_selector import MarketSelector
    from src.data.database import PolymarketDatabase
    from src.whitepaper.whitepaper_generator import MarketUniverseReportGenerator

    async with aiohttp.ClientSession() as session:
        start = time.time()

        if args.dry_run:
            markets = _sample_markets()
            logger.info("DRY RUN: Using %d sample markets", len(markets))
        else:
            gamma = GammaClient(session)
            raw_markets = await gamma.discover_all_active_markets()
            markets = [GammaClient.parse_market_basic(m) for m in raw_markets]
            logger.info("Fetched %d active markets from Gamma API", len(markets))

        analyzer = LiquidityAnalyzer()
        scored_markets = analyzer.score_markets(markets)
        logger.info("Scored %d markets", len(scored_markets))

        selector = MarketSelector()
        sel_result = selector.select_top_markets(
            scored_markets,
            top_n=args.top_n,
            min_score=args.min_score,
            min_volume=Decimal(str(args.min_volume)),
            min_liquidity=Decimal(str(args.min_liquidity)),
            min_days_to_resolution=args.min_days,
            price_range=(Decimal("0.30"), Decimal("0.70")),
        )
        selected = sel_result.selected
        logger.info("Selected %d markets passing all filters", len(selected))

        db = await PolymarketDatabase.create(args.db)
        try:
            await _save_to_db(db, scored_markets, selected)
            logger.info("Data persisted to %s", args.db)
        finally:
            await db.close()

        generator = MarketUniverseReportGenerator(db)
        report_path = generator.generate(
            all_markets=scored_markets,
            selected_markets=selected,
            output_dir=args.output,
        )

        elapsed = time.time() - start
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETE in %.1f seconds", elapsed)
        logger.info("  Markets discovered: %d", len(scored_markets))
        logger.info("  Markets selected:   %d", len(selected))
        logger.info("  Whitepaper:         %s", report_path)
        logger.info("=" * 60)

    return 0


async def _save_to_db(
    db: PolymarketDatabase,
    all_markets: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    for m in all_markets:
        try:
            info = MarketInfo(
                id=str(m.get("condition_id", m.get("id", ""))),
                condition_id=str(m.get("condition_id", m.get("id", ""))),
                question=str(m.get("question", "")),
                slug=str(m.get("slug", "")),
                category=str(m.get("category", "")),
                tags=m.get("tags", []),
                volume_num=Decimal(str(m.get("volume", "0"))),
                liquidity_num=Decimal(str(m.get("liquidity", "0"))),
                tick_size=(
                    Decimal(str(m.get("tick_size", "0.01")))
                    if not isinstance(m.get("tick_size"), Decimal)
                    else m["tick_size"]
                ),
                neg_risk=bool(m.get("neg_risk", False)),
                end_date=str(m.get("end_date", "")),
                active=bool(m.get("active", True)),
                closed=bool(m.get("closed", False)),
            )
            await db.upsert_market(info)
        except Exception:
            logger = logging.getLogger("market_analysis")
            logger.exception("Error saving market %s", m.get("id", "unknown"))


SAMPLE_MARKETS = [
    {
        "condition_id": "0xsample001",
        "question": "Will Bitcoin close above $100k on June 30?",
        "slug": "bitcoin-above-100k-june",
        "category": "crypto",
        "tags": ["bitcoin", "price"],
        "volume": 2500000.00,
        "liquidity": 500000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-06-30T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.52, 0.48],
        "clobTokenIds": ["0xtoken_yes_001", "0xtoken_no_001"],
        "events": [{"id": "0xevt001", "title": "Bitcoin Price"}],
    },
    {
        "condition_id": "0xsample002",
        "question": "Will ETH reach $5000 by July 15?",
        "slug": "eth-5000-july",
        "category": "crypto",
        "tags": ["ethereum", "price"],
        "volume": 1800000.00,
        "liquidity": 350000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-07-15T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.35, 0.65],
        "clobTokenIds": ["0xtoken_yes_002", "0xtoken_no_002"],
        "events": [{"id": "0xevt002", "title": "ETH Price"}],
    },
    {
        "condition_id": "0xsample003",
        "question": "Will the Fed cut rates in September?",
        "slug": "fed-rate-cut-sept",
        "category": "politics",
        "tags": ["fed", "interest rates"],
        "volume": 3200000.00,
        "liquidity": 750000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-09-20T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.45, 0.55],
        "clobTokenIds": ["0xtoken_yes_003", "0xtoken_no_003"],
        "events": [{"id": "0xevt003", "title": "Fed Rates"}],
    },
    {
        "condition_id": "0xsample004",
        "question": "Will SOL surpass $200 this month?",
        "slug": "sol-200-month",
        "category": "crypto",
        "tags": ["solana", "price"],
        "volume": 850000.00,
        "liquidity": 120000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-05-31T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.48, 0.52],
        "clobTokenIds": ["0xtoken_yes_004", "0xtoken_no_004"],
        "events": [{"id": "0xevt004", "title": "SOL Price"}],
    },
    {
        "condition_id": "0xsample005",
        "question": "Will a US state pass a Bitcoin reserve bill?",
        "slug": "state-bitcoin-reserve",
        "category": "politics",
        "tags": ["bitcoin", "regulation"],
        "volume": 750000.00,
        "liquidity": 95000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-12-31T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.62, 0.38],
        "clobTokenIds": ["0xtoken_yes_005", "0xtoken_no_005"],
        "events": [{"id": "0xevt005", "title": "Bitcoin Reserve"}],
    },
    {
        "condition_id": "0xsample006",
        "question": "Will the Lakers win the NBA Finals?",
        "slug": "lakers-nba-finals",
        "category": "sports",
        "tags": ["nba", "basketball"],
        "volume": 4200000.00,
        "liquidity": 890000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-06-20T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.58, 0.42],
        "clobTokenIds": ["0xtoken_yes_006", "0xtoken_no_006"],
        "events": [{"id": "0xevt006", "title": "NBA Finals"}],
    },
    {
        "condition_id": "0xsample007",
        "question": "Will Trump win the 2028 primaries?",
        "slug": "trump-2028-primaries",
        "category": "politics",
        "tags": ["election", "trump"],
        "volume": 5600000.00,
        "liquidity": 1200000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2027-06-01T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.42, 0.58],
        "clobTokenIds": ["0xtoken_yes_007", "0xtoken_no_007"],
        "events": [{"id": "0xevt007", "title": "2028 Primaries"}],
    },
    {
        "condition_id": "0xsample008",
        "question": "Will AI pass a medical licensing exam?",
        "slug": "ai-medical-license",
        "category": "technology",
        "tags": ["ai", "healthcare"],
        "volume": 650000.00,
        "liquidity": 80000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.001",
        "neg_risk": False,
        "endDate": "2026-08-15T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.15, 0.85],
        "clobTokenIds": ["0xtoken_yes_008", "0xtoken_no_008"],
        "events": [{"id": "0xevt008", "title": "AI Medical"}],
    },
    {
        "condition_id": "0xsample009",
        "question": "Will BTC be declared legal tender in a G20 country?",
        "slug": "btc-g20-legal-tender",
        "category": "geopolitics",
        "tags": ["bitcoin", "regulation"],
        "volume": 920000.00,
        "liquidity": 180000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-10-01T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.25, 0.75],
        "clobTokenIds": ["0xtoken_yes_009", "0xtoken_no_009"],
        "events": [{"id": "0xevt009", "title": "BTC Legal Tender"}],
    },
    {
        "condition_id": "0xsample010",
        "question": "Will the S&P 500 close above 6500 by year end?",
        "slug": "sp500-6500-year-end",
        "category": "finance",
        "tags": ["stocks", "sp500"],
        "volume": 2100000.00,
        "liquidity": 450000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-12-31T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.55, 0.45],
        "clobTokenIds": ["0xtoken_yes_010", "0xtoken_no_010"],
        "events": [{"id": "0xevt010", "title": "S&P 500"}],
    },
    {
        "condition_id": "0xsample011",
        "question": "Will a COVID-19 vaccine be approved for children under 5?",
        "slug": "covid-vaccine-children",
        "category": "health",
        "tags": ["covid", "vaccine"],
        "volume": 350000.00,
        "liquidity": 45000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2026-07-01T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.72, 0.28],
        "clobTokenIds": ["0xtoken_yes_011", "0xtoken_no_011"],
        "events": [{"id": "0xevt011", "title": "COVID Vaccine"}],
    },
    {
        "condition_id": "0xsample012",
        "question": "Will SpaceX land on Mars by 2028?",
        "slug": "spacex-mars-2028",
        "category": "technology",
        "tags": ["space", "spacex"],
        "volume": 1200000.00,
        "liquidity": 210000.00,
        "active": True,
        "closed": False,
        "enable_order_book": True,
        "tickSize": "0.01",
        "neg_risk": False,
        "endDate": "2028-01-01T23:59:59Z",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.08, 0.92],
        "clobTokenIds": ["0xtoken_yes_012", "0xtoken_no_012"],
        "events": [{"id": "0xevt012", "title": "SpaceX Mars"}],
    },
]


def _sample_markets() -> list[dict[str, Any]]:
    from copy import deepcopy
    return deepcopy(SAMPLE_MARKETS)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
