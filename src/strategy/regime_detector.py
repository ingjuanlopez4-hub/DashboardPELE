"""
Regime Detector — Market regime identification and weight recalibration.

Detects shifts in market microstructure (volatility, liquidity, trend)
and triggers recalibration of signal weights and trading parameters.
Uses rolling windows and statistical tests to classify the current regime.

Regimes:
  - LOW_VOL: low volatility, high liquidity — normal operation
  - HIGH_VOL: high volatility — reduce position sizes, favor wick signals
  - LOW_LIQ: low liquidity — favor maker orders, wider edges
  - TRENDING: strong directional move — favor external/montecarlo signals
  - CHAOTIC: extreme conditions — reduce exposure, favor defensive signals
"""

import logging
import time
from collections import deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger(__name__)

REGIME_LOW_VOL = "LOW_VOL"
REGIME_HIGH_VOL = "HIGH_VOL"
REGIME_LOW_LIQ = "LOW_LIQ"
REGIME_TRENDING = "TRENDING"
REGIME_CHAOTIC = "CHAOTIC"

DEFAULT_CONFIG: dict[str, Any] = {
    "vol_window": 50,
    "liq_window": 50,
    "trend_window": 20,
    "vol_threshold_high": Decimal("0.03"),
    "vol_threshold_chaotic": Decimal("0.06"),
    "liq_threshold_low": Decimal("2000"),
    "trend_threshold": Decimal("0.015"),
    "min_samples_for_detection": 10,
    "recalibration_cooldown_seconds": 3600,
    "regime_weights": {
        REGIME_LOW_VOL: {
            "wick": Decimal("0.15"),
            "external": Decimal("0.15"),
            "finbert": Decimal("0.40"),
            "montecarlo": Decimal("0.30"),
        },
        REGIME_HIGH_VOL: {
            "wick": Decimal("0.35"),
            "external": Decimal("0.25"),
            "finbert": Decimal("0.10"),
            "montecarlo": Decimal("0.30"),
        },
        REGIME_LOW_LIQ: {
            "wick": Decimal("0.10"),
            "external": Decimal("0.10"),
            "finbert": Decimal("0.50"),
            "montecarlo": Decimal("0.30"),
        },
        REGIME_TRENDING: {
            "wick": Decimal("0.10"),
            "external": Decimal("0.50"),
            "finbert": Decimal("0.10"),
            "montecarlo": Decimal("0.30"),
        },
        REGIME_CHAOTIC: {
            "wick": Decimal("0.25"),
            "external": Decimal("0.10"),
            "finbert": Decimal("0.15"),
            "montecarlo": Decimal("0.50"),
        },
    },
}


class RegimeDetector:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = config or DEFAULT_CONFIG

        self._price_changes: deque[Decimal] = deque(maxlen=self._cfg["vol_window"])
        self._bid_depths: deque[Decimal] = deque(maxlen=self._cfg["liq_window"])
        self._ask_depths: deque[Decimal] = deque(maxlen=self._cfg["liq_window"])
        self._trend_prices: deque[Decimal] = deque(maxlen=self._cfg["trend_window"])

        self._current_regime: str = REGIME_LOW_VOL
        self._last_recalibration_time: float = 0.0
        self._regime_start_time: float = time.time()
        self._regime_duration: float = 0.0

    @property
    def current_regime(self) -> str:
        return self._current_regime

    def update_price(self, price: Decimal) -> None:
        if self._trend_prices:
            last = self._trend_prices[-1]
            change = abs(price - last) / max(last, Decimal("0.001"))
            self._price_changes.append(change)
        self._trend_prices.append(price)

    def update_liquidity(
        self,
        bid_depth: Decimal,
        ask_depth: Decimal,
    ) -> None:
        self._bid_depths.append(bid_depth)
        self._ask_depths.append(ask_depth)

    def detect_regime(self) -> str:
        if len(self._price_changes) < self._cfg["min_samples_for_detection"]:
            return self._current_regime

        avg_volatility = (
            sum(self._price_changes) / len(self._price_changes)
            if self._price_changes
            else Decimal("0")
        )

        avg_bid_depth = (
            sum(self._bid_depths) / len(self._bid_depths)
            if self._bid_depths
            else Decimal("999999")
        )
        avg_ask_depth = (
            sum(self._ask_depths) / len(self._ask_depths)
            if self._ask_depths
            else Decimal("999999")
        )
        avg_liquidity = (avg_bid_depth + avg_ask_depth) / Decimal("2")

        trend_direction = Decimal("0")
        if len(self._trend_prices) >= 5:
            first = self._trend_prices[0]
            last = self._trend_prices[-1]
            trend_direction = (last - first) / max(first, Decimal("0.001"))

        new_regime = self._classify(
            avg_volatility, avg_liquidity, trend_direction
        )

        if new_regime != self._current_regime:
            logger.info(
                "Regime change: %s → %s (vol=%.4f liq=%s trend=%.4f)",
                self._current_regime, new_regime,
                float(avg_volatility), str(avg_liquidity),
                float(trend_direction),
            )
            self._current_regime = new_regime
            self._regime_start_time = time.time()

        self._regime_duration = time.time() - self._regime_start_time
        return self._current_regime

    def _classify(
        self,
        volatility: Decimal,
        liquidity: Decimal,
        trend: Decimal,
    ) -> str:
        if volatility >= self._cfg["vol_threshold_chaotic"]:
            return REGIME_CHAOTIC

        if abs(trend) >= self._cfg["trend_threshold"]:
            return REGIME_TRENDING

        if liquidity <= self._cfg["liq_threshold_low"]:
            return REGIME_LOW_LIQ

        if volatility >= self._cfg["vol_threshold_high"]:
            return REGIME_HIGH_VOL

        return REGIME_LOW_VOL

    def get_regime_weights(self) -> dict[str, Decimal]:
        return dict(self._cfg["regime_weights"].get(
            self._current_regime,
            self._cfg["regime_weights"][REGIME_LOW_VOL],
        ))

    def should_recalibrate(self) -> bool:
        now = time.time()
        if now - self._last_recalibration_time >= self._cfg["recalibration_cooldown_seconds"]:
            self._last_recalibration_time = now
            return True
        return False

    def get_regime_summary(self) -> dict[str, Any]:
        return {
            "regime": self._current_regime,
            "duration_seconds": round(self._regime_duration),
            "avg_volatility": (
                float(sum(self._price_changes) / len(self._price_changes))
                if self._price_changes else 0.0
            ),
            "avg_liquidity": (
                float(
                    (sum(self._bid_depths) + sum(self._ask_depths))
                    / (len(self._bid_depths) + len(self._ask_depths) + 1)
                    * 2
                )
                if self._bid_depths else 0.0
            ),
            "regime_weights": self.get_regime_weights(),
        }
