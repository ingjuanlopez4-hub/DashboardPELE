"""
MonteCarloSimulator — Simulates price paths using Geometric Brownian Motion
on log-odds space for Polymarket prediction markets.

Estimates expected value by running N simulations of future price trajectories
and computing the distribution of outcomes. Uses logit transform to map
probabilities [0,1] to real line for GBM, then sigmoid to map back.

All monetary values use Decimal exclusively (never float in trading paths).
"""

import asyncio
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_VOLATILITY = Decimal("0.50")
DEFAULT_DAYS_TO_EXPIRY = Decimal("7")
DEFAULT_PATHS = 10000
MIN_VOL_HISTORY = 5


class MonteCarloSimulator:
    """Monte Carlo price simulator using GBM on log-odds.

    Parameters
    ----------
    n_paths : int
        Number of simulation paths (default 10000).
    """

    def __init__(self, n_paths: int = DEFAULT_PATHS) -> None:
        self.n_paths = n_paths

    async def simulate(
        self,
        current_price: Decimal,
        volatility: Decimal | None = None,
        days_to_expiry: Decimal | None = None,
        tick_size: Decimal = Decimal("0.01"),
        price_history: list[Decimal] | None = None,
        spread: Decimal | None = None,
        mid_price: Decimal | None = None,
    ) -> dict[str, Any]:
        """Run Monte Carlo simulation.

        Parameters
        ----------
        current_price : Decimal
            Current probability (mid-price or last traded price).
        volatility : Decimal | None
            Annualized volatility. If None, estimated from price_history + spread.
        days_to_expiry : Decimal | None
            Days until market resolution.
        tick_size : Decimal
            Market tick size for quantization.
        price_history : list[Decimal] | None
            Historical prices for volatility estimation.
        spread : Decimal | None
            Current bid-ask spread.
        mid_price : Decimal | None
            Current mid-price.

        Returns
        -------
        dict with keys:
            score (Decimal) — simulated probability
            probability (Decimal) — final estimated probability
            ev (Decimal) — expected value vs current price
            details (dict) — simulation parameters
        """
        if current_price <= 0 or current_price >= 1:
            return {
                "score": current_price,
                "probability": current_price,
                "ev": Decimal("0"),
                "details": {"reason": "price_boundary"},
            }

        if volatility is None:
            volatility = self._estimate_volatility(
                price_history or [],
                spread,
                mid_price,
            )

        if days_to_expiry is None:
            days_to_expiry = DEFAULT_DAYS_TO_EXPIRY

        T = float(days_to_expiry) / 365.0
        if T <= 0:
            T = 1.0 / 365.0

        lo0 = self._logit(current_price)
        mu = 0.0
        sigma_f = float(volatility)

        try:
            mean_p, std_p, p5 = await asyncio.to_thread(
                self._run_simulation, lo0, mu, sigma_f, T, self.n_paths
            )
        except Exception:
            logger.exception("Monte Carlo simulation failed")
            return {
                "score": current_price,
                "probability": current_price,
                "ev": Decimal("0"),
                "details": {"error": "simulation_failed"},
            }

        mc_prob = Decimal(str(mean_p))
        mc_prob = max(Decimal("0.001"), min(mc_prob, Decimal("0.999")))
        mc_prob = mc_prob.quantize(tick_size, rounding=ROUND_HALF_UP)

        ev = mc_prob - current_price

        return {
            "score": mc_prob,
            "probability": mc_prob,
            "ev": ev.quantize(tick_size, rounding=ROUND_HALF_UP),
            "details": {
                "sigma": str(volatility),
                "days_to_expiry": str(days_to_expiry),
                "T_years": round(T, 6),
                "std_p": round(std_p, 6),
                "p5": round(p5, 6),
                "n_paths": self.n_paths,
            },
        }

    @staticmethod
    def _run_simulation(
        lo0: float,
        mu: float,
        sigma: float,
        dt: float,
        n_paths: int,
    ) -> tuple[float, float, float]:
        """Run GBM simulation on log-odds space.

        Returns (mean_price, std_price, percentile_5).
        """
        rng = np.random.default_rng()
        z = rng.standard_normal(n_paths)
        lo_T = lo0 + (mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z
        lo_T_clipped = np.clip(lo_T, -700, 700)
        p_T = 1.0 / (1.0 + np.exp(-lo_T_clipped))
        p_T = np.clip(p_T, 1e-12, 1 - 1e-12)
        mean_p = float(np.mean(p_T))
        std_p = float(np.std(p_T))
        p5 = float(np.percentile(p_T, 5))
        return mean_p, std_p, p5

    @staticmethod
    def _estimate_volatility(
        price_history: list[Decimal],
        spread: Decimal | None = None,
        mid_price: Decimal | None = None,
    ) -> Decimal:
        """Estimate volatility from price history and/or spread."""
        vols: list[float] = []

        if len(price_history) >= 10:
            prices_f = [float(p) for p in price_history]
            log_rets = [
                math.log(prices_f[i] / prices_f[i - 1])
                for i in range(1, len(prices_f))
                if prices_f[i - 1] > 0 and prices_f[i] > 0
            ]
            if len(log_rets) >= MIN_VOL_HISTORY:
                hist_vol = float(np.std(log_rets)) * math.sqrt(252 * 24)
                vols.append(min(hist_vol, 5.0))

        if spread is not None and mid_price is not None and mid_price > 0:
            spread_pct = float(spread / mid_price)
            spread_vol = spread_pct * math.sqrt(252 * 24)
            vols.append(min(spread_vol, 5.0))

        if not vols:
            return DEFAULT_VOLATILITY

        return Decimal(str(float(np.mean(vols))))

    @staticmethod
    def _logit(p: Decimal) -> float:
        p_f = min(max(float(p), 1e-12), 1 - 1e-12)
        return math.log(p_f / (1 - p_f))

    @staticmethod
    def days_to_expiry(
        end_date_str: str | None,
        default_days: Decimal = DEFAULT_DAYS_TO_EXPIRY,
    ) -> Decimal:
        """Compute days until market resolution from ISO date string."""
        if not end_date_str:
            return default_days
        try:
            end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            remaining = (end - datetime.now(timezone.utc)).total_seconds()
            return max(Decimal(str(remaining / 86400)), Decimal("0.001"))
        except (ValueError, TypeError):
            return default_days
