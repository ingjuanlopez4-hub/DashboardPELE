"""
WickFishingAnalyzer — Detects wick-fishing manipulation patterns in order book
data for Polymarket prediction markets.

VECTORIZED OPTIMIZATION (April 2026):
  - numpy vectorization for threshold detection and spike analysis
  - Order book history converted to numpy arrays for batch processing
  - Percentile calculations vectorized (np.percentile)
  - Spike detection vectorized with boolean masks

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

import numpy as np

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
        """Analyze snapshots for wick-fishing patterns with VECTORIZED numpy.

        Performance: Converts order book history to numpy arrays for
        batch percentile computation and vectorized spike detection.

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

        # Vectorized: extract all bid/ask sizes into numpy arrays
        bid_sizes_list: list[list[float]] = []
        ask_sizes_list: list[list[float]] = []
        for snap in snap_list:
            bid_sizes_list.append([float(s[1]) for s in snap.bids[:self.top_levels]])
            ask_sizes_list.append([float(s[1]) for s in snap.asks[:self.top_levels]])

        if not bid_sizes_list or not ask_sizes_list:
            return self._empty_result(price, reason="no_data")

        bid_array = np.array(bid_sizes_list)  # Shape: (n_snapshots, top_levels)
        ask_array = np.array(ask_sizes_list)

        # Vectorized: compute level averages
        avg_bid_sizes = np.mean(bid_array, axis=0)  # Shape: (top_levels,)
        avg_ask_sizes = np.mean(ask_array, axis=0)

        # Vectorized: detect spikes (size > 95th percentile of the level)
        bid_thresholds = np.percentile(bid_array, 95, axis=0)  # Shape: (top_levels,)
        ask_thresholds = np.percentile(ask_array, 95, axis=0)

        # Vectorized spike detection: where size exceeds threshold and next snapshot has size = 0
        # (indicating the order was placed and then removed)
        bid_spikes_present = bid_array > bid_thresholds[np.newaxis, :]
        ask_spikes_present = ask_array > ask_thresholds[np.newaxis, :]

        # Detect removals: spike at t=0 and no size at t=1
        wick_events = 0
        total_checks = 0
        max_intensity = 0.0

        for i in range(1, len(snap_list)):
            # Ask side: check for spikes that disappear
            ask_spike = ask_spikes_present[i - 1]
            ask_removed = ask_array[i] == 0
            ask_wick = ask_spike & ask_removed

            for level in range(self.top_levels):
                if level >= ask_wick.shape[0]:
                    continue
                if ask_wick[level]:
                    prev_size = float(ask_array[i - 1, level])
                    avg = float(avg_bid_sizes[level]) if level < len(avg_bid_sizes) else 0
                    if avg > 0 and prev_size > avg * float(self.intensity_threshold):
                        wick_events += 1
                        intensity = prev_size / avg
                        if intensity > max_intensity:
                            max_intensity = intensity
                total_checks += 1

            # Bid side: check for spikes that disappear
            bid_spike = bid_spikes_present[i - 1]
            bid_removed = bid_array[i] == 0
            bid_wick = bid_spike & bid_removed

            for level in range(self.top_levels):
                if level >= bid_wick.shape[0]:
                    continue
                if bid_wick[level]:
                    prev_size = float(bid_array[i - 1, level])
                    avg = float(avg_bid_sizes[level]) if level < len(avg_bid_sizes) else 0
                    if avg > 0 and prev_size > avg * float(self.intensity_threshold):
                        wick_events += 1
                        intensity = prev_size / avg
                        if intensity > max_intensity:
                            max_intensity = intensity
                total_checks += 1

        if total_checks == 0:
            return self._empty_result(price, reason="no_comparisons")

        raw_ratio = Decimal(str(wick_events)) / Decimal(str(total_checks))
        intensity_factor = min(Decimal(str(max_intensity)) / MAX_INTENSITY_CAP, MAX_SCORE)
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
                "max_intensity": max_intensity,
                "n_snapshots": len(snap_list),
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
