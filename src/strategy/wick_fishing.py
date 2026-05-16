"""
WickFishingAnalyzer — Detects wick-fishing manipulation patterns in order book
data for Polymarket prediction markets.

A wick-fishing pattern occurs when a large bid or ask is placed to create a false
impression of demand/supply, then suddenly removed. This analyzer examines
consecutive order book snapshots to detect such patterns and computes an
adjusted probability signal.

All monetary values use Decimal exclusively (never float in trading paths).
"""

import logging
from collections import deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger(__name__)

SIZE_PRECISION = Decimal("0.01")
DEFAULT_TICK_SIZE = Decimal("0.01")
MIN_SNAPSHOTS = 2
TOP_LEVELS = 5
INTENSITY_THRESHOLD = Decimal("3")
MAX_INTENSITY_CAP = Decimal("10")
MAX_SCORE = Decimal("1")
SIZE_DECIMAL = Decimal("0.01")


class BookSnapshot:
    """Snapshot of order book at a point in time."""

    __slots__ = ("bids", "asks", "timestamp")

    def __init__(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        timestamp: float,
    ) -> None:
        self.bids = bids
        self.asks = asks
        self.timestamp = timestamp


class WickFishingAnalyzer:
    """Analyzes order book snapshots for wick-fishing patterns.

    Parameters
    ----------
    min_snapshots : int
        Minimum consecutive snapshots needed for analysis.
    top_levels : int
        Number of bid/ask levels to examine.
    intensity_threshold : Decimal
        Size multiplier threshold to classify a removal as a wick event.
    """

    def __init__(
        self,
        min_snapshots: int = MIN_SNAPSHOTS,
        top_levels: int = TOP_LEVELS,
        intensity_threshold: Decimal = INTENSITY_THRESHOLD,
    ) -> None:
        self.min_snapshots = min_snapshots
        self.top_levels = top_levels
        self.intensity_threshold = intensity_threshold

    def analyze(
        self,
        snapshots: list[BookSnapshot] | deque,
        current_price: Decimal | None = None,
        spread: Decimal | None = None,
    ) -> dict[str, Any]:
        """Analyze snapshots for wick-fishing patterns.

        Parameters
        ----------
        snapshots : list[BookSnapshot] | deque
            Consecutive order book snapshots.
        current_price : Decimal | None
            Current mid-price or last traded price.
        spread : Decimal | None
            Current bid-ask spread.

        Returns
        -------
        dict with keys:
            score (Decimal) — normalized signal [0, 1]
            probability (Decimal) — implied probability adjusted for wick activity
            details (dict) — internal metrics
        """
        snap_list = list(snapshots)
        if len(snap_list) < self.min_snapshots:
            return self._empty_result(current_price, reason="insufficient_data")

        price = current_price if current_price is not None else Decimal("0.5")

        avg_ask_sizes: dict[int, Decimal] = {}
        avg_bid_sizes: dict[int, Decimal] = {}
        for level in range(self.top_levels):
            ask_avg = self.compute_level_averages(snap_list, "asks", level)
            if ask_avg > 0:
                avg_ask_sizes[level] = ask_avg
            bid_avg = self.compute_level_averages(snap_list, "bids", level)
            if bid_avg > 0:
                avg_bid_sizes[level] = bid_avg

        wick_events = 0
        total_checks = 0
        max_intensity = Decimal("0")

        for i in range(1, len(snap_list)):
            prev = snap_list[i - 1]
            curr = snap_list[i]

            we, tc, mi = self._compare_levels(prev, curr, "asks", avg_ask_sizes)
            wick_events += we
            total_checks += tc
            if mi > max_intensity:
                max_intensity = mi

            we, tc, mi = self._compare_levels(prev, curr, "bids", avg_bid_sizes)
            wick_events += we
            total_checks += tc
            if mi > max_intensity:
                max_intensity = mi

        if total_checks == 0:
            return self._empty_result(price, reason="no_comparisons")

        raw_ratio = Decimal(str(wick_events)) / Decimal(str(total_checks))
        intensity_factor = min(max_intensity / MAX_INTENSITY_CAP, MAX_SCORE)
        score = min(raw_ratio * Decimal("10"), MAX_SCORE)
        score = score * Decimal("0.7") + intensity_factor * Decimal("0.3")

        spread_adj = Decimal("0")
        if spread is not None and price > 0:
            spread_adj = (spread / price) * Decimal("0.1")

        wick_prob = price * (MAX_SCORE + (score - Decimal("0.5")) * spread_adj)
        wick_prob = max(Decimal("0.001"), min(wick_prob, Decimal("0.999")))

        return {
            "score": score.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "probability": wick_prob.quantize(DEFAULT_TICK_SIZE, rounding=ROUND_HALF_UP),
            "details": {
                "wick_events": wick_events,
                "total_checks": total_checks,
                "raw_ratio": float(raw_ratio),
                "max_intensity": float(max_intensity),
            },
        }

    def _compare_levels(
        self,
        prev: BookSnapshot,
        curr: BookSnapshot,
        side: str,
        avg_sizes: dict[int, Decimal],
    ) -> tuple[int, int, Decimal]:
        """Compare bid/ask levels between two snapshots for wick patterns.

        Returns (wick_events, total_checks, max_intensity).
        """
        prev_levels = prev.asks if side == "asks" else prev.bids
        curr_levels = curr.asks if side == "asks" else curr.bids

        wick_events = 0
        total_checks = 0
        max_intensity = Decimal("0")

        compare_levels = min(len(prev_levels), len(curr_levels), self.top_levels)
        for level in range(compare_levels):
            if level >= len(prev_levels) or level >= len(curr_levels):
                continue
            prev_p, prev_s = prev_levels[level]
            curr_p, curr_s = curr_levels[level]

            if prev_p != curr_p:
                continue
            avg = avg_sizes.get(level, Decimal("0"))
            if avg <= 0:
                continue
            if prev_s > 0 and curr_s == 0:
                if prev_s > avg * self.intensity_threshold:
                    wick_events += 1
                    intensity = prev_s / avg
                    if intensity > max_intensity:
                        max_intensity = intensity
            total_checks += 1

        return wick_events, total_checks, max_intensity

    @staticmethod
    def compute_level_averages(
        snapshots: list[BookSnapshot] | deque, side: str, level: int
    ) -> Decimal:
        """Compute average size at a given bid/ask level across all snapshots."""
        sizes = []
        for snap in snapshots:
            levels = snap.bids if side == "bids" else snap.asks
            if level < len(levels):
                sizes.append(levels[level][1])
        if not sizes:
            return Decimal("0")
        return sum(sizes, Decimal("0")) / Decimal(str(len(sizes)))

    @staticmethod
    def _empty_result(
        price: Decimal | None,
        reason: str = "insufficient_data",
    ) -> dict[str, Any]:
        return {
            "score": Decimal("0.5"),
            "probability": price or Decimal("0.5"),
            "details": {"reason": reason},
        }
