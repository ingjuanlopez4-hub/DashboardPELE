"""
MarketQualifier — Evaluates trading signals before execution to ensure they
meet market quality criteria. Applied in the EjecutorOrdenes just before
circuit breaker checks (or in the strategy to filter before enqueueing).

Filters implemented:
  - Opportunity window: Only trade in last N seconds of each candle cycle.
  - Probability range: Reject markets outside [min_prob, max_prob].
  - Volume filter: Require minimum 24h volume.
  - Time-to-resolution: Only markets with > min_hours_to_resolution remaining.
  - Dynamic min_edge calibration: Uses PerformanceTracker to adjust edge threshold.
  - Position age limit: (delegated to PositionManager, not enforced here).

All monetary values use Decimal for precision.
"""

import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

logger = logging.getLogger("market_filter")


class MarketQualifier:
    """Evaluates signals against market quality criteria.

    Parameters
    ----------
    min_prob : Decimal
        Minimum probability to trade (default 0.30).
    max_prob : Decimal
        Maximum probability to trade (default 0.70).
    min_volume_24h : Decimal
        Minimum 24h volume in USDC (default 5000).
    min_hours_to_resolution : int
        Minimum hours until market resolution (default 336 = 14 days).
    opportunity_windows : dict
        Configuration for trade windows per market type.
    dynamic_min_edge_provider : Callable[[], Decimal] | None
        Optional callable that returns the current dynamic min_edge.
        If provided, this overrides the static min_edge.
    """

    def __init__(
        self,
        min_prob: Decimal = Decimal("0.30"),
        max_prob: Decimal = Decimal("0.70"),
        min_volume_24h: Decimal = Decimal("5000"),
        min_hours_to_resolution: int = 336,
        opportunity_windows: dict[str, dict[str, Any]] | None = None,
        dynamic_min_edge_provider: Callable[[], Decimal] | None = None,
    ) -> None:
        self._min_prob = min_prob
        self._max_prob = max_prob
        self._min_volume_24h = min_volume_24h
        self._min_hours_to_resolution = min_hours_to_resolution
        self._opportunity_windows = opportunity_windows or {
            "15m": {"window_before_end_s": 90},
            "5m": {"window_before_end_s": 30},
            "default": {"window_before_end_s": 60},
        }
        self._dynamic_edge_provider = dynamic_min_edge_provider

    def check_probability_range(self, probability: Decimal) -> str | None:
        """Check if probability is within the allowed range.

        Returns None if OK, or error string if filtered.
        """
        if probability < self._min_prob:
            return f"probability_below_min: {probability} < {self._min_prob}"
        if probability > self._max_prob:
            return f"probability_above_max: {probability} > {self._max_prob}"
        return None

    def check_opportunity_window(
        self,
        end_time_s: float | None,
        market_type: str = "default",
        current_time: float | None = None,
    ) -> str | None:
        """Check if we are within the trading window before market end.

        Only allows trading in the last N seconds of each candle cycle.

        Parameters
        ----------
        end_time_s : float | None
            Unix timestamp of market end time. If None, window check is skipped.
        market_type : str
            Type of market (15m, 5m, default).
        current_time : float | None
            Current time (defaults to time.time()).

        Returns None if OK, or error string if outside window.
        """
        if end_time_s is None or end_time_s <= 0:
            return None  # No window info available — allow

        now = current_time or time.time()
        remaining_s = end_time_s - now

        if remaining_s <= 0:
            return "market_already_ended"

        window_config = self._opportunity_windows.get(
            market_type, self._opportunity_windows["default"]
        )
        window_s = window_config.get("window_before_end_s", 60)

        if remaining_s > window_s:
            return (
                f"outside_opportunity_window: remaining={remaining_s:.0f}s > "
                f"window={window_s}s (market_type={market_type})"
            )

        return None

    def check_volume(self, volume_24h: Decimal | None) -> str | None:
        """Check if the market has sufficient 24h volume.

        Parameters
        ----------
        volume_24h : Decimal | None
            Trading volume in last 24h in USDC. If None, check is skipped.

        Returns None if OK, or error string if filtered.
        """
        if volume_24h is None:
            return None  # No volume data — skip check
        if volume_24h < self._min_volume_24h:
            return (
                f"volume_below_min: {volume_24h} < {self._min_volume_24h}"
            )
        return None

    def check_time_to_resolution(
        self,
        end_time_s: float | None,
        current_time: float | None = None,
    ) -> str | None:
        """Check if market has enough time remaining until resolution.

        Parameters
        ----------
        end_time_s : float | None
            Unix timestamp of market end time. If None, check is skipped.
        current_time : float | None
            Current time.

        Returns None if OK, or error string if filtered.
        """
        if end_time_s is None or end_time_s <= 0:
            return None  # No info — skip check

        now = current_time or time.time()
        remaining_hours = (end_time_s - now) / 3600

        if remaining_hours < self._min_hours_to_resolution:
            return (
                f"too_close_to_resolution: {remaining_hours:.1f}h < "
                f"{self._min_hours_to_resolution}h"
            )
        return None

    def get_min_edge(self, static_min_edge: Decimal) -> Decimal:
        """Get the effective minimum edge, potentially adjusted dynamically.

        Parameters
        ----------
        static_min_edge : Decimal
            The configured static minimum edge.

        Returns
        -------
        Decimal
            The effective minimum edge to use.
        """
        if self._dynamic_edge_provider is not None:
            try:
                dynamic_edge = self._dynamic_edge_provider()
                effective = max(static_min_edge, dynamic_edge)
                if effective != static_min_edge:
                    logger.debug(
                        "Dynamic min_edge adjustment: static=%s dynamic=%s effective=%s",
                        str(static_min_edge), str(dynamic_edge), str(effective),
                    )
                return effective
            except Exception:
                logger.exception("Error getting dynamic min_edge")
        return static_min_edge

    def check_signal(
        self,
        signal: dict[str, Any],
        market_meta: dict[str, Any] | None = None,
        static_min_edge: Decimal | None = None,
    ) -> str | None:
        """Run ALL market quality checks on a signal.

        This is the unified entry point. Returns None if all checks pass,
        or an error string describing the first failed check.

        Parameters
        ----------
        signal : dict
            The trading signal with keys: probability, asset_id, market_type, etc.
        market_meta : dict | None
            Optional market metadata (end_time, volume_24h, etc.).
        static_min_edge : Decimal | None
            Optional static minimum edge override.

        Checks performed:
          1. Probability range filter
          2. Volume filter
          3. Time-to-resolution filter
          4. Opportunity window filter
          5. Min edge (if static_min_edge provided, combined with dynamic)
        """
        probability = Decimal(str(signal.get("probability", "0.5")))

        # 1. Probability range
        reason = self.check_probability_range(probability)
        if reason is not None:
            return reason

        # 2. Volume
        if market_meta is not None:
            vol = market_meta.get("volume_24h")
            if vol is not None:
                vol_dec = Decimal(str(vol))
                reason = self.check_volume(vol_dec)
                if reason is not None:
                    return reason

        # 3. Time to resolution
        if market_meta is not None:
            end_time = market_meta.get("end_time_s", market_meta.get("end_time"))
            if end_time is not None:
                reason = self.check_time_to_resolution(float(end_time))
                if reason is not None:
                    return reason

        # 4. Opportunity window (uses candle_end_time_s if available, else end_time_s)
        if market_meta is not None:
            candle_end = market_meta.get(
                "candle_end_time_s",
                market_meta.get("end_time_s", market_meta.get("end_time")),
            )
            market_type = signal.get("market_type", "default")
            if candle_end is not None:
                reason = self.check_opportunity_window(float(candle_end), market_type)
                if reason is not None:
                    return reason

        # 5. Min edge
        if static_min_edge is not None:
            effective_edge = self.get_min_edge(static_min_edge)
            ev = Decimal(str(signal.get("ev", "0")))
            if abs(ev) < effective_edge:
                return (
                    f"edge_below_min: ev={ev} < effective_min_edge={effective_edge} "
                    f"(static={static_min_edge})"
                )

        return None
