"""
RobustnessAnalyzer — Statistical tests to validate the backtest results.

Tests implemented:
  1. Permutation test: shuffle trade sequence 10,000 times to compute
     empirical p-value for the Sharpe ratio.
  2. Monte Carlo equity: bootstrap return sequences to produce confidence
     bands around the equity curve.
  3. Out-of-sample test: split markets into train/test sets to detect
     overfitting.
"""

import asyncio
import logging
import math
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

import numpy as np

from src.data.database import TradeRecord
from src.data.market_tracker import TrackedMarket
from src.whitepaper.strategy_runner import StrategyBacktestRunner, BacktestResults

logger = logging.getLogger("robustness_analyzer")


@dataclass
class RobustnessResults:
    """Container for all robustness analysis results."""
    permutation_p_value: float = 0.0
    permutation_sharpe_null: list[float] = field(default_factory=list)
    observed_sharpe: float = 0.0

    mc_equity_curves: list[list[float]] = field(default_factory=list)
    mc_upper_95: list[float] = field(default_factory=list)
    mc_lower_95: list[float] = field(default_factory=list)
    mc_upper_99: list[float] = field(default_factory=list)
    mc_lower_99: list[float] = field(default_factory=list)

    train_sharpe: float = 0.0
    test_sharpe: float = 0.0
    train_pnl: Decimal = Decimal("0")
    test_pnl: Decimal = Decimal("0")
    sharpe_drop: float = 0.0


class RobustnessAnalyzer:
    """Statistical robustness tests for backtest results."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)

    async def run_all(
        self,
        trades: list[TradeRecord],
        equity_curve: Optional[list[Decimal]] = None,
        markets: Optional[list[TrackedMarket]] = None,
        backtest_results: Optional[BacktestResults] = None,
        db: Any | None = None,
    ) -> RobustnessResults:
        """Run all robustness tests and return combined results.

        Parameters
        ----------
        trades : list[TradeRecord]
            The sequence of trades from the backtest.
        equity_curve : list[Decimal], optional
            The equity curve values.
        markets : list[TrackedMarket], optional
            Market list for out-of-sample test.
        backtest_results : BacktestResults, optional
            Full backtest results for out-of-sample test.

        Returns
        -------
        RobustnessResults
            Combined results from all tests.
        """
        results = RobustnessResults()
        obs_sharpe = self._compute_sharpe(trades)
        results.observed_sharpe = obs_sharpe

        perm_result = await self.permutation_test(trades, n_permutations=10000)
        results.permutation_p_value = perm_result["p_value"]
        results.permutation_sharpe_null = perm_result["sharpe_null"]

        if equity_curve:
            mc_result = await self.monte_carlo_equity(equity_curve, n_simulations=1000)
            results.mc_equity_curves = mc_result["curves"]
            results.mc_upper_95 = mc_result["upper_95"]
            results.mc_lower_95 = mc_result["lower_95"]
            results.mc_upper_99 = mc_result["upper_99"]
            results.mc_lower_99 = mc_result["lower_99"]

        if markets and backtest_results is not None:
            oos_result = await self.out_of_sample_test(markets, backtest_results, db=db)
            results.train_sharpe = oos_result["train_sharpe"]
            results.test_sharpe = oos_result["test_sharpe"]
            results.train_pnl = oos_result["train_pnl"]
            results.test_pnl = oos_result["test_pnl"]
            results.sharpe_drop = oos_result["sharpe_drop"]

        return results

    async def permutation_test(
        self,
        trades: list[TradeRecord],
        n_permutations: int = 10000,
    ) -> dict[str, Any]:
        """Permutation test for Sharpe ratio significance.

        Shuffles the trade return sequence many times to build a null
        distribution of Sharpe ratios, then computes the empirical p-value.

        Parameters
        ----------
        trades : list[TradeRecord]
            Trade sequence from the backtest.
        n_permutations : int
            Number of permutations (default 10000).

        Returns
        -------
        dict with keys: p_value, sharpe_null, observed_sharpe
        """
        logger.info("Running permutation test with %d permutations", n_permutations)

        if len(trades) < 3:
            logger.warning("Too few trades (%d) for permutation test", len(trades))
            return {
                "p_value": 1.0,
                "sharpe_null": [],
                "observed_sharpe": 0.0,
            }

        returns = self._trades_to_returns(trades)
        observed_sharpe = self._sharpe_from_returns(returns)

        sharpe_null: list[float] = []
        n = len(returns)

        for _ in range(n_permutations):
            self._rng.shuffle(returns)
            s = self._sharpe_from_returns(returns)
            sharpe_null.append(s)

        extreme_count = sum(1 for s in sharpe_null if s >= observed_sharpe)
        p_value = (extreme_count + 1) / (n_permutations + 1)

        logger.info("Permutation test: observed Sharpe=%.4f, p-value=%.6f", observed_sharpe, p_value)
        return {
            "p_value": p_value,
            "sharpe_null": sharpe_null,
            "observed_sharpe": observed_sharpe,
        }

    async def monte_carlo_equity(
        self,
        equity_curve: list[Decimal],
        n_simulations: int = 1000,
    ) -> dict[str, Any]:
        """Monte Carlo simulation of equity curves via bootstrap.

        Resamples returns with replacement to generate alternative equity
        curves, then computes confidence bands.

        Parameters
        ----------
        equity_curve : list[Decimal]
            Original equity curve values.
        n_simulations : int
            Number of bootstrapped equity curves (default 1000).

        Returns
        -------
        dict with keys: curves, upper_95, lower_95, upper_99, lower_99
        """
        logger.info("Running Monte Carlo equity simulation with %d simulations", n_simulations)

        if len(equity_curve) < 5:
            logger.warning("Too few equity points (%d) for MC simulation", len(equity_curve))
            return {
                "curves": [],
                "upper_95": [],
                "lower_95": [],
                "upper_99": [],
                "lower_99": [],
            }

        eq_float = [float(x) for x in equity_curve]
        returns = [eq_float[i] / eq_float[i - 1] - 1.0 for i in range(1, len(eq_float))]

        if not returns:
            return {
                "curves": [],
                "upper_95": [],
                "lower_95": [],
                "upper_99": [],
                "lower_99": [],
            }

        n_steps = len(returns)
        initial_eq = eq_float[0]
        curves: list[list[float]] = []

        for _ in range(n_simulations):
            sim_returns = [self._rng.choice(returns) for _ in range(n_steps)]
            sim_eq = [initial_eq]
            for r in sim_returns:
                sim_eq.append(sim_eq[-1] * (1 + r))
            curves.append(sim_eq)

        upper_95: list[float] = []
        lower_95: list[float] = []
        upper_99: list[float] = []
        lower_99: list[float] = []

        for step in range(n_steps + 1):
            values = [c[step] for c in curves]
            values.sort()
            upper_95.append(values[int(len(values) * 0.975)])
            lower_95.append(values[int(len(values) * 0.025)])
            upper_99.append(values[int(len(values) * 0.995)])
            lower_99.append(values[int(len(values) * 0.005)])

        logger.info("Monte Carlo equity simulation complete")
        return {
            "curves": curves,
            "upper_95": upper_95,
            "lower_95": lower_95,
            "upper_99": upper_99,
            "lower_99": lower_99,
        }

    async def out_of_sample_test(
        self,
        markets: list[TrackedMarket],
        backtest_results: BacktestResults,
        db: Any | None = None,
        train_ratio: float = 0.7,
    ) -> dict[str, Any]:
        """Out-of-sample test by splitting markets into train/test sets.

        Parameters
        ----------
        markets : list[TrackedMarket]
            Full list of markets.
        backtest_results : BacktestResults
            Full backtest results for comparison.
        train_ratio : float
            Fraction of markets to use for training (default 0.7).

        Returns
        -------
        dict with keys: train_sharpe, test_sharpe, train_pnl, test_pnl, sharpe_drop
        """
        logger.info("Running out-of-sample test with train_ratio=%.2f", train_ratio)

        if len(markets) < 4:
            logger.warning("Too few markets (%d) for OOS test", len(markets))
            return {
                "train_sharpe": 0.0,
                "test_sharpe": 0.0,
                "train_pnl": Decimal("0"),
                "test_pnl": Decimal("0"),
                "sharpe_drop": 0.0,
            }

        shuffled = list(markets)
        self._rng.shuffle(shuffled)
        split_idx = int(len(shuffled) * train_ratio)
        train_markets = shuffled[:split_idx]
        test_markets = shuffled[split_idx:]

        runner = StrategyBacktestRunner(
            db=db,
            params=backtest_results.params_used,
        )

        # If no db available, skip OOS backtest
        if db is None:
            logger.warning("No database available for OOS test — skipping")
            return {
                "train_sharpe": 0.0,
                "test_sharpe": 0.0,
                "train_pnl": Decimal("0"),
                "test_pnl": Decimal("0"),
                "sharpe_drop": 0.0,
            }

        train_result = await runner.run_backtest(train_markets)
        test_result = await runner.run_backtest(test_markets)

        train_sharpe = train_result.sharpe_ratio
        test_sharpe = test_result.sharpe_ratio
        sharpe_drop = train_sharpe - test_sharpe

        logger.info("OOS test: train Sharpe=%.4f, test Sharpe=%.4f, drop=%.4f",
                     train_sharpe, test_sharpe, sharpe_drop)

        return {
            "train_sharpe": train_sharpe,
            "test_sharpe": test_sharpe,
            "train_pnl": train_result.net_pnl,
            "test_pnl": test_result.net_pnl,
            "sharpe_drop": sharpe_drop,
        }

    def _compute_sharpe(self, trades: list[TradeRecord]) -> float:
        """Compute annualized Sharpe ratio from a trade sequence."""
        returns = self._trades_to_returns(trades)
        return self._sharpe_from_returns(returns)

    def _trades_to_returns(self, trades: list[TradeRecord]) -> list[float]:
        """Convert trade records to a return series."""
        if not trades:
            return []
        returns: list[float] = []
        cumulative = 10000.0
        for t in trades:
            prev = cumulative
            if t.side.startswith("BUY"):
                cumulative += float(t.usdc_amount)
            else:
                cumulative -= float(t.usdc_amount)
            if prev > 0:
                returns.append((cumulative - prev) / prev)
        return returns

    def _sharpe_from_returns(self, returns: list[float]) -> float:
        """Compute annualized Sharpe ratio from return series."""
        if len(returns) < 2:
            return 0.0
        mean_r = np.mean(returns)
        std_r = np.std(returns, ddof=1)
        if std_r < 1e-10:
            return 0.0
        return float(mean_r / std_r * math.sqrt(252))
