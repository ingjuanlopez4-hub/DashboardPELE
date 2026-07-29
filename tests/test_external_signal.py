"""
Tests for the external signal module (Chainlink/Binance price feeds and SignalAggregator).

Covers:
  - StrikePriceTracker: strike updates, distance calculation, edge cases.
  - SignalAggregator: signal caching, direction logic, confidence scaling.
  - ExternalSignal dataclass structure.
"""

from decimal import Decimal
import time
from typing import Any

import pytest

from src.strategy.external_signal import (
    StrikePriceTracker,
    SignalAggregator,
    ExternalSignal,
    SIGNAL_UP,
    SIGNAL_DOWN,
    SIGNAL_NEUTRAL,
    DEFAULT_MIN_DISTANCE_PCT,
)


class TestStrikePriceTracker:
    """Tests for StrikePriceTracker."""

    def test_initial_strike_is_zero(self) -> None:
        tracker = StrikePriceTracker()
        assert tracker.get_strike("btcusdt") == Decimal("0")

    def test_update_and_get_strike(self) -> None:
        tracker = StrikePriceTracker()
        tracker.update_strike("btcusdt", Decimal("50000"))
        assert tracker.get_strike("btcusdt") == Decimal("50000")

    def test_zero_distance_when_no_strike(self) -> None:
        tracker = StrikePriceTracker()
        distance = tracker.get_distance_pct("btcusdt", Decimal("55000"))
        assert distance == Decimal("0")

    def test_positive_distance(self) -> None:
        tracker = StrikePriceTracker()
        tracker.update_strike("btcusdt", Decimal("50000"))
        distance = tracker.get_distance_pct("btcusdt", Decimal("55000"))
        # (55000 - 50000) / 50000 * 100 = 10%
        assert distance == Decimal("10.00")

    def test_negative_distance(self) -> None:
        tracker = StrikePriceTracker()
        tracker.update_strike("btcusdt", Decimal("50000"))
        distance = tracker.get_distance_pct("btcusdt", Decimal("45000"))
        # (45000 - 50000) / 50000 * 100 = -10%
        assert distance == Decimal("-10.00")

    def test_zero_current_price_returns_zero(self) -> None:
        tracker = StrikePriceTracker()
        tracker.update_strike("btcusdt", Decimal("50000"))
        distance = tracker.get_distance_pct("btcusdt", Decimal("0"))
        assert distance == Decimal("0")

    def test_multiple_symbols(self) -> None:
        tracker = StrikePriceTracker()
        tracker.update_strike("btcusdt", Decimal("50000"))
        tracker.update_strike("ethusdt", Decimal("3000"))
        assert tracker.get_strike("btcusdt") == Decimal("50000")
        assert tracker.get_strike("ethusdt") == Decimal("3000")
        assert tracker.get_distance_pct("btcusdt", Decimal("55000")) == Decimal("10.00")
        assert tracker.get_distance_pct("ethusdt", Decimal("3300")) == Decimal("10.00")


class MockPriceFeed:
    """Mock price feed for testing SignalAggregator."""

    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = prices

    async def get_price(self, symbol: str) -> Decimal | None:
        return self._prices.get(symbol)


class TestSignalAggregator:
    """Tests for SignalAggregator."""

    @pytest.mark.asyncio
    async def test_neutral_when_no_price(self) -> None:
        """Aggregator returns NEUTRAL when no price feeds are configured."""
        aggregator = SignalAggregator()
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt")
        assert signal.direction == SIGNAL_NEUTRAL
        assert signal.source == "none"
        assert signal.confidence == Decimal("0")

    @pytest.mark.asyncio
    async def test_up_signal_from_binance(self) -> None:
        """Price above strike produces UP signal."""
        feed = MockPriceFeed({"btcusdt": Decimal("50500")})
        aggregator = SignalAggregator(
            binance_feed=feed,  # type: ignore
            min_distance_pct=Decimal("0.15"),
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt")
        assert signal.direction == SIGNAL_UP
        assert signal.source == "binance"
        # distance = (50500 - 50000) / 50000 * 100 = 1.0% > 0.15%
        assert signal.distance_pct >= DEFAULT_MIN_DISTANCE_PCT

    @pytest.mark.asyncio
    async def test_down_signal_from_chainlink(self) -> None:
        """Price below strike produces DOWN signal."""
        feed = MockPriceFeed({"btcusdt": Decimal("49500")})
        aggregator = SignalAggregator(
            chainlink_feed=feed,  # type: ignore
            min_distance_pct=Decimal("0.15"),
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt")
        assert signal.direction == SIGNAL_DOWN
        assert signal.distance_pct < Decimal("0")

    @pytest.mark.asyncio
    async def test_neutral_when_below_threshold(self) -> None:
        """Small distance below threshold produces NEUTRAL signal."""
        feed = MockPriceFeed({"btcusdt": Decimal("50050")})
        aggregator = SignalAggregator(
            binance_feed=feed,  # type: ignore
            min_distance_pct=Decimal("1.0"),  # 1% threshold
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt")
        # distance = 0.1% < 1% threshold
        assert signal.direction == SIGNAL_NEUTRAL

    @pytest.mark.asyncio
    async def test_confidence_scales_with_distance(self) -> None:
        """Confidence should be proportional to distance / (2 * threshold)."""
        feed = MockPriceFeed({"btcusdt": Decimal("51000")})
        aggregator = SignalAggregator(
            binance_feed=feed,  # type: ignore
            min_distance_pct=Decimal("0.5"),
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt")
        # distance = 2.0%, threshold = 0.5%
        # confidence = min(2.0 / (0.5 * 2), 1.0) = 1.0
        assert signal.confidence == Decimal("1.00")

    @pytest.mark.asyncio
    async def test_signal_caching(self) -> None:
        """Repeated calls within TTL return cached signal."""
        feed = MockPriceFeed({"btcusdt": Decimal("50500")})
        aggregator = SignalAggregator(
            binance_feed=feed,  # type: ignore
            cache_ttl_ms=10000,  # 10 second cache
            min_distance_pct=Decimal("0.15"),
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal1 = await aggregator.get_signal("btcusdt")
        assert signal1.direction == SIGNAL_UP

        # Change price — but cache should still return old signal
        feed._prices["btcusdt"] = Decimal("49500")
        signal2 = await aggregator.get_signal("btcusdt")
        assert signal2.direction == SIGNAL_UP  # Still cached

    @pytest.mark.asyncio
    async def test_register_market_and_get_market_signal(self) -> None:
        """Registering an asset-symbol mapping allows lookup by asset_id."""
        feed = MockPriceFeed({"btcusdt": Decimal("50500")})
        aggregator = SignalAggregator(
            binance_feed=feed,  # type: ignore
            min_distance_pct=Decimal("0.15"),
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        aggregator.register_market("asset_123", "btcusdt")
        _ = await aggregator.get_signal("btcusdt")

        market_signal = aggregator.get_market_signal("asset_123")
        assert market_signal is not None
        assert market_signal.direction == SIGNAL_UP

    @pytest.mark.asyncio
    async def test_get_latest_signal_without_computing(self) -> None:
        """get_latest_signal returns None if no signal yet computed."""
        aggregator = SignalAggregator()
        assert aggregator.get_latest_signal("btcusdt") is None

    @pytest.mark.asyncio
    async def test_custom_min_distance(self) -> None:
        """Custom min_distance_pct changes signal threshold."""
        feed = MockPriceFeed({"btcusdt": Decimal("50020")})
        aggregator = SignalAggregator(
            binance_feed=feed,  # type: ignore
            min_distance_pct=Decimal("0.01"),  # 0.01% threshold
        )
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt")
        # distance = 0.04% > 0.01% threshold
        assert signal.direction == SIGNAL_UP

    @pytest.mark.asyncio
    async def test_get_signal_with_explicit_price(self) -> None:
        """Providing current_price directly should override feeds."""
        aggregator = SignalAggregator(min_distance_pct=Decimal("0.15"))
        aggregator.set_strike_price("btcusdt", Decimal("50000"))
        signal = await aggregator.get_signal("btcusdt", current_price=Decimal("55000"))
        assert signal.direction == SIGNAL_UP
        assert signal.distance_pct == Decimal("10.00")


class TestExternalSignalDataclass:
    """Tests for the ExternalSignal dataclass structure."""

    def test_create_signal(self) -> None:
        signal = ExternalSignal(
            direction=SIGNAL_UP,
            distance_pct=Decimal("1.5"),
            source="binance",
            current_price=Decimal("50750"),
            strike_price=Decimal("50000"),
            symbol="btcusdt",
            confidence=Decimal("0.75"),
            timestamp=1234567890.0,
        )
        assert signal.direction == "UP"
        assert signal.distance_pct == Decimal("1.5")
        assert signal.source == "binance"
        assert signal.current_price == Decimal("50750")
        assert signal.strike_price == Decimal("50000")
        assert signal.symbol == "btcusdt"
        assert signal.confidence == Decimal("0.75")
