"""
ParameterSweeper — performs grid search over strategy parameters to find
the optimal combination. Generates heatmap-ready data for visualization
in the whitepaper.

Swept parameters:
  - min_edge: 0.01 to 0.15 (step 0.02)
  - kelly_fraction: 0.1 to 0.5 (step 0.1)
  - max_position_size_pct: 1.0 to 10.0 (step 1.0)
  - w_wick, w_sentiment, w_montecarlo: weight combinations
"""

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import product
from typing import Any, Optional

import numpy as np

from src.data.database import PolymarketDatabase
from src.data.market_tracker import TrackedMarket
from src.whitepaper.strategy_runner import StrategyBacktestRunner, BacktestResults

logger = logging.getLogger("parameter_sweep")


@dataclass
class SweepResults:
    """Results from a full parameter sweep."""
    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    all_results: list[dict[str, Any]] = field(default_factory=list)
    optimal_params: dict[str, Any] = field(default_factory=dict)
    optimal_sharpe: float = 0.0
    optimal_pnl: Decimal = Decimal("0")
    optimal_drawdown: Decimal = Decimal("0")
    heatmap_data: dict[str, Any] = field(default_factory=dict)

    def best_by_sharpe(self) -> dict[str, Any]:
        if not self.all_results:
            return {}
        best = max(self.all_results, key=lambda r: r.get("sharpe", -999))
        return best

    def best_by_pnl(self) -> dict[str, Any]:
        if not self.all_results:
            return {}
        best = max(self.all_results, key=lambda r: float(r.get("net_pnl", "-999")))
        return best

    def best_by_calmar(self) -> dict[str, Any]:
        if not self.all_results:
            return {}
        best = max(self.all_results, key=lambda r: r.get("calmar", -999))
        return best


DEFAULT_PARAM_GRID: dict[str, list[Any]] = {
    "min_edge": [Decimal("0.01"), Decimal("0.03"), Decimal("0.05"), Decimal("0.07"),
                  Decimal("0.09"), Decimal("0.11"), Decimal("0.13"), Decimal("0.15")],
    "kelly_fraction": [Decimal("0.1"), Decimal("0.2"), Decimal("0.25"),
                        Decimal("0.3"), Decimal("0.4"), Decimal("0.5")],
    "max_position_size_pct": [Decimal("1.0"), Decimal("2.0"), Decimal("3.0"),
                                Decimal("5.0"), Decimal("7.0"), Decimal("10.0")],
    "w_wick": [Decimal("0.2"), Decimal("0.3"), Decimal("0.4"), Decimal("0.5")],
    "w_sentiment": [Decimal("0.2"), Decimal("0.3"), Decimal("0.4")],
}


class ParameterSweeper:
    """Performs grid search over strategy parameters.

    Parameters
    ----------
    db : PolymarketDatabase
        Database instance.
    max_combinations : int
        Maximum parameter combinations to evaluate (default 200).
    """

    def __init__(
        self,
        db: PolymarketDatabase,
        max_combinations: int = 200,
    ) -> None:
        self._db = db
        self._max_combinations = max_combinations

    async def run_sweep(
        self,
        markets: list[TrackedMarket],
        param_grid: Optional[dict[str, list[Any]]] = None,
    ) -> SweepResults:
        """Run a full parameter sweep over the given markets.

        Parameters
        ----------
        markets : list[TrackedMarket]
            Markets to sweep over.
        param_grid : dict, optional
            Override the default parameter grid.

        Returns
        -------
        SweepResults
            All results plus optimal parameter identification.
        """
        grid = param_grid or DEFAULT_PARAM_GRID

        keys = list(grid.keys())
        value_lists = list(grid.values())
        combinations = list(product(*value_lists))

        if len(combinations) > self._max_combinations:
            stride = max(1, len(combinations) // self._max_combinations)
            combinations = combinations[::stride]
            logger.info("Trimmed to %d combinations (stride=%d)", len(combinations), stride)

        logger.info("Running parameter sweep with %d combinations over %d markets",
                     len(combinations), len(markets))

        results: list[dict[str, Any]] = []
        heatmap_x: list[float] = []
        heatmap_y: list[float] = []
        heatmap_z_sharpe: list[float] = []
        heatmap_z_pnl: list[float] = []

        for idx, combo in enumerate(combinations):
            params = dict(zip(keys, combo))

            runner = StrategyBacktestRunner(self._db, params=params)
            bt_result = await runner.run_backtest(markets)

            entry = {
                "params": {k: str(v) if isinstance(v, Decimal) else v for k, v in params.items()},
                "sharpe": bt_result.sharpe_ratio,
                "net_pnl": bt_result.net_pnl,
                "max_drawdown_pct": bt_result.max_drawdown_pct,
                "win_rate": bt_result.win_rate,
                "profit_factor": bt_result.profit_factor,
                "total_trades": bt_result.total_trades,
                "calmar": bt_result.calmar_ratio,
                "sortino": bt_result.sortino_ratio,
            }
            results.append(entry)

            min_edge_val = float(params.get("min_edge", Decimal("0.05")))
            kelly_val = float(params.get("kelly_fraction", Decimal("0.25")))
            heatmap_x.append(min_edge_val)
            heatmap_y.append(kelly_val)
            heatmap_z_sharpe.append(bt_result.sharpe_ratio)
            heatmap_z_pnl.append(float(bt_result.net_pnl))

            logger.info("Sweep %d/%d: min_edge=%.2f kelly=%.2f sharpe=%.2f pnl=%s",
                        idx + 1, len(combinations), min_edge_val, kelly_val,
                        bt_result.sharpe_ratio, bt_result.net_pnl)

        sweep_result = SweepResults(
            param_grid={k: [str(v) if isinstance(v, Decimal) else v for v in vl] for k, vl in grid.items()},
            all_results=results,
            heatmap_data={
                "x": heatmap_x,
                "y": heatmap_y,
                "z_sharpe": heatmap_z_sharpe,
                "z_pnl": heatmap_z_pnl,
            },
        )

        best = sweep_result.best_by_sharpe()
        if best:
            sweep_result.optimal_params = best.get("params", {})
            sweep_result.optimal_sharpe = best.get("sharpe", 0.0)
            sweep_result.optimal_pnl = Decimal(str(best.get("net_pnl", "0")))
            sweep_result.optimal_drawdown = Decimal(str(best.get("max_drawdown_pct", "0")))

        logger.info("Sweep complete: optimal sharpe=%.4f with params=%s",
                     sweep_result.optimal_sharpe, sweep_result.optimal_params)
        return sweep_result

    async def run_sweep_single_param(
        self,
        markets: list[TrackedMarket],
        param_name: str,
        param_values: list[Any],
        fixed_params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Sweep a single parameter while keeping others fixed.

        Useful for generating line plots of Sharpe vs parameter value.
        """
        fixed = fixed_params or {}
        results: list[dict[str, Any]] = []

        for val in param_values:
            params = {**fixed, param_name: val}
            runner = StrategyBacktestRunner(self._db, params=params)
            bt_result = await runner.run_backtest(markets)

            results.append({
                "param_name": param_name,
                "param_value": str(val) if isinstance(val, Decimal) else val,
                "sharpe": bt_result.sharpe_ratio,
                "net_pnl": str(bt_result.net_pnl),
                "max_drawdown": str(bt_result.max_drawdown_pct),
                "win_rate": bt_result.win_rate,
                "total_trades": bt_result.total_trades,
            })

            logger.info("Single sweep: %s=%s sharpe=%.4f", param_name, val, bt_result.sharpe_ratio)

        return results
