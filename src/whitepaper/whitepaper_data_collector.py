import asyncio
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from src.data.database import PolymarketDatabase, MarketInfo
from src.data.market_discovery import MarketDiscoveryManager
from src.data.market_tracker import MarketTracker, TrackedMarket
from src.data.market_selector import MarketSelector
from src.strategy.finbert_sentiment import FinBERTSentimentAnalyzer
from src.strategy.news_fetcher import NewsFetcher
from src.whitepaper.strategy_runner import StrategyBacktestRunner, BacktestResults
from src.whitepaper.parameter_sweep import ParameterSweeper, SweepResults
from src.whitepaper.robustness_analyzer import RobustnessAnalyzer, RobustnessResults
from src.whitepaper.whitepaper_generator import WhitepaperGenerator

logger = logging.getLogger("whitepaper_data_collector")


@dataclass
class PipelineResults:
    db_path: str = ""
    markets_discovered: int = 0
    markets_tracked: int = 0
    markets_selected: int = 0
    backtest: Optional[BacktestResults] = None
    sweep: Optional[SweepResults] = None
    robustness: Optional[RobustnessResults] = None
    whitepaper_path: str = ""
    errors: list[str] = field(default_factory=list)
    timing: dict[str, float] = field(default_factory=dict)


class WhitepaperDataCollector:
    def __init__(
        self,
        db_path: str = "polymarket_universe.db",
        output_dir: str = "./whitepaper_output",
        top_n: int = 50,
        min_score: float = 0.4,
        run_sweep: bool = True,
    ) -> None:
        self._db_path = db_path
        self._output_dir = output_dir
        self._top_n = top_n
        self._min_score = min_score
        self._run_sweep = run_sweep
        self._db: Optional[PolymarketDatabase] = None

    async def collect_all_data(self) -> PipelineResults:
        results = PipelineResults(db_path=self._db_path)
        errors: list[str] = []
        timing: dict[str, float] = {}

        logger.info("=" * 60)
        logger.info("STARTING POLYMARKET WHITEPAPER PIPELINE")
        logger.info("=" * 60)

        try:
            t0 = asyncio.get_event_loop().time()
            self._db = await PolymarketDatabase.create(self._db_path)
            timing["db_init"] = asyncio.get_event_loop().time() - t0
            logger.info("Database initialized: %s", self._db_path)
        except Exception as exc:
            errors.append(f"Database init failed: {exc}")
            logger.exception("Database init failed")
            results.errors = errors
            return results

        try:
            try:
                t0 = asyncio.get_event_loop().time()
                discovery = MarketDiscoveryManager(self._db)
                markets = await discovery.discover_all_active_markets()
                results.markets_discovered = len(markets)
                timing["discovery"] = asyncio.get_event_loop().time() - t0
                logger.info("Discovered %d active markets", len(markets))
            except Exception as exc:
                errors.append(f"Market discovery failed: {exc}")
                logger.exception("Market discovery failed")
                results.errors = errors
                return results

            try:
                t0 = asyncio.get_event_loop().time()
                tracker = MarketTracker(self._db, discovery)
                await tracker.run_once()
                results.markets_tracked = len(tracker.tracked_markets)
                timing["tracking"] = asyncio.get_event_loop().time() - t0
                logger.info("Tracked %d markets", len(tracker.tracked_markets))
            except Exception as exc:
                errors.append(f"Market tracking failed: {exc}")
                logger.exception("Market tracking failed")
                results.errors = errors
                return results

            try:
                t0 = asyncio.get_event_loop().time()
                selector = MarketSelector()
                tracked_markets = list(tracker.tracked_markets.values())
                market_dicts = [m.to_dict() if isinstance(m, TrackedMarket) else m for m in tracked_markets]
                sel_result = selector.select_top_markets(
                    market_dicts,
                    top_n=self._top_n,
                    min_score=Decimal(str(self._min_score)),
                )
                selected = sel_result.selected
                results.markets_selected = len(selected)
                timing["selection"] = asyncio.get_event_loop().time() - t0
                logger.info("Selected %d markets for backtest", len(selected))

                for m in selected[:5]:
                    d = m if isinstance(m, dict) else m.to_dict() if hasattr(m, "to_dict") else {}
                    logger.info("  Top market: %s (score=%s)", d.get("question", "?")[:40], d.get("liquidity_score", "?"))
            except Exception as exc:
                errors.append(f"Market selection failed: {exc}")
                logger.exception("Market selection failed")
                results.errors = errors
                return results

            news_fetcher: Optional[NewsFetcher] = None
            try:
                t0 = asyncio.get_event_loop().time()
                sentiment_analyzer = await FinBERTSentimentAnalyzer.get_instance()
                news_fetcher = NewsFetcher()
                runner = StrategyBacktestRunner(
                    db=self._db,
                    sentiment_analyzer=sentiment_analyzer,
                    news_fetcher=news_fetcher,
                )
                backtest_result = await runner.run_backtest(selected)
                results.backtest = backtest_result
                timing["backtest"] = asyncio.get_event_loop().time() - t0
                logger.info("Backtest complete: PnL=%s, Sharpe=%.4f, WinRate=%.1f%%",
                            backtest_result.net_pnl, backtest_result.sharpe_ratio,
                            backtest_result.win_rate * 100)
            except Exception as exc:
                errors.append(f"Backtest failed: {exc}")
                logger.exception("Backtest failed")
                results.errors = errors
                return results
            finally:
                if news_fetcher is not None:
                    await news_fetcher.close()

            if self._run_sweep and selected:
                try:
                    t0 = asyncio.get_event_loop().time()
                    sweeper = ParameterSweeper(self._db, max_combinations=100)
                    sweep_results = await sweeper.run_sweep(selected[:10])
                    results.sweep = sweep_results
                    timing["sweep"] = asyncio.get_event_loop().time() - t0
                    logger.info("Parameter sweep complete: optimal Sharpe=%.4f",
                                sweep_results.optimal_sharpe)
                except Exception as exc:
                    errors.append(f"Parameter sweep failed: {exc}")
                    logger.warning("Parameter sweep failed, continuing: %s", exc)
            else:
                logger.info("Parameter sweep skipped")

            try:
                t0 = asyncio.get_event_loop().time()
                analyzer = RobustnessAnalyzer()
                robustness_results = await analyzer.run_all(
                    trades=backtest_result.trades,
                    equity_curve=backtest_result.equity_curve,
                    markets=selected,
                    backtest_results=backtest_result,
                )
                results.robustness = robustness_results
                timing["robustness"] = asyncio.get_event_loop().time() - t0
                logger.info("Robustness analysis: p-value=%.6f, Sharpe drop=%.4f",
                            robustness_results.permutation_p_value,
                            robustness_results.sharpe_drop)
            except Exception as exc:
                errors.append(f"Robustness analysis failed: {exc}")
                logger.warning("Robustness analysis failed, continuing: %s", exc)

            try:
                t0 = asyncio.get_event_loop().time()
                generator = WhitepaperGenerator()
                report_path = await generator.generate(
                    markets=tracked_markets,
                    backtest_results=backtest_result,
                    sweep_results=results.sweep,
                    robustness_results=results.robustness,
                    output_dir=self._output_dir,
                )
                results.whitepaper_path = report_path
                timing["whitepaper"] = asyncio.get_event_loop().time() - t0
                logger.info("Whitepaper generated: %s", report_path)
            except Exception as exc:
                errors.append(f"Whitepaper generation failed: {exc}")
                logger.exception("Whitepaper generation failed")

            results.errors = errors
            results.timing = timing

            logger.info("=" * 60)
            logger.info("PIPELINE COMPLETE")
            logger.info("  Markets discovered: %d", results.markets_discovered)
            logger.info("  Markets tracked:    %d", results.markets_tracked)
            logger.info("  Markets selected:   %d", results.markets_selected)
            logger.info("  Backtest PnL:       %s", backtest_result.net_pnl if backtest_result else "N/A")
            logger.info("  Whitepaper:         %s", results.whitepaper_path)
            logger.info("  Errors:             %d", len(errors))
            for stage, secs in timing.items():
                logger.info("    %s: %.1fs", stage, secs)
            logger.info("=" * 60)
        finally:
            if self._db:
                await self._db.close()
                await asyncio.sleep(0.1)

        return results

