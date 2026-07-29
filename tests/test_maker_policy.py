"""
Tests for the MakerPolicy module (maker-first execution strategy).

Covers:
  - compute_maker_price: price improvement logic for BUY/SELL
  - should_cross_spread: edge vs fee vs spread decision
  - execute_maker_order: end-to-end flow with mocked callbacks
  - execute_forced_close: market order for stop-loss
"""

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.execution.maker_policy import (
    MakerPolicy,
    DEFAULT_MAKER_TIMEOUT_S,
    DEFAULT_CROSS_SPREAD_EDGE_THRESHOLD,
    DEFAULT_TICK_SIZE,
)


class TestComputeMakerPrice:
    """Tests for MakerPolicy.compute_maker_price."""

    def setup_method(self) -> None:
        self.policy = MakerPolicy()

    def test_buy_improves_bid_by_one_tick(self) -> None:
        """BUY order places at best_bid + 1 tick."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
            side="BUY",
        )
        assert price == Decimal("0.51")  # best_bid + 0.01

    def test_sell_improves_ask_by_one_tick(self) -> None:
        """SELL order places at best_ask - 1 tick."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
            side="SELL",
        )
        assert price == Decimal("0.54")  # best_ask - 0.01

    def test_buy_with_no_bid_uses_ask_minus_2_ticks(self) -> None:
        """When no bid exists, BUY uses conservative ask - 2*tick."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0"),
            best_ask=Decimal("0.60"),
            side="BUY",
        )
        assert price == Decimal("0.58")  # 0.60 - 0.02

    def test_sell_with_no_ask_uses_bid_plus_2_ticks(self) -> None:
        """When no ask exists, SELL uses conservative bid + 2*tick."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0.40"),
            best_ask=Decimal("0"),
            side="SELL",
        )
        assert price == Decimal("0.42")  # 0.40 + 0.02

    def test_buy_with_no_bid_and_no_ask(self) -> None:
        """When both are zero, BUY uses minimum price (0.01)."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0"),
            best_ask=Decimal("0"),
            side="BUY",
        )
        assert price == Decimal("0.01")

    def test_sell_with_no_bid_and_no_ask(self) -> None:
        """When both are zero, SELL uses near-maximum price (0.99)."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0"),
            best_ask=Decimal("0"),
            side="SELL",
        )
        assert price == Decimal("0.99")

    def test_price_clamped_to_valid_range(self) -> None:
        """Maker price cannot be below tick_size or above 1 - tick_size."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0.99"),
            best_ask=Decimal("0.99"),
            side="BUY",
        )
        assert price >= DEFAULT_TICK_SIZE
        assert price <= Decimal("1") - DEFAULT_TICK_SIZE

    def test_custom_tick_size(self) -> None:
        """Tick size parameter changes the improve increment."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
            side="BUY",
            tick_size=Decimal("0.001"),
        )
        assert price == Decimal("0.501")  # best_bid + 0.001

    def test_side_case_insensitive(self) -> None:
        """Side parameter should work with lowercase."""
        price = self.policy.compute_maker_price(
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
            side="buy",
        )
        assert price == Decimal("0.51")


class TestShouldCrossSpread:
    """Tests for MakerPolicy.should_cross_spread."""

    def setup_method(self) -> None:
        self.policy = MakerPolicy()

    def test_high_edge_crosses_spread(self) -> None:
        """Large edge justifies crossing spread."""
        should, reason = self.policy.should_cross_spread(
            edge=Decimal("0.05"),  # 5% edge
            probability=Decimal("0.50"),
            spread_pct=Decimal("1.0"),  # 1% spread
        )
        assert should
        assert "edge" in reason.lower()

    def test_low_edge_does_not_cross(self) -> None:
        """Small edge below fee+spread does not cross."""
        should, _ = self.policy.should_cross_spread(
            edge=Decimal("0.005"),
            probability=Decimal("0.50"),
            spread_pct=Decimal("2.0"),
        )
        assert not should

    def test_edge_below_threshold_does_not_cross(self) -> None:
        """Edge below configured threshold does not cross even if > fee."""
        policy = MakerPolicy(cross_spread_edge_threshold=Decimal("0.10"))
        should, _ = policy.should_cross_spread(
            edge=Decimal("0.05"),
            probability=Decimal("0.50"),
            spread_pct=Decimal("0.1"),
        )
        assert not should

    def test_fee_varies_with_probability(self) -> None:
        """Fee at 50% is higher than at 90%, affecting cross decision."""
        should_mid, _ = self.policy.should_cross_spread(
            edge=Decimal("0.04"),
            probability=Decimal("0.50"),
            spread_pct=Decimal("0.5"),
        )
        should_extreme, _ = self.policy.should_cross_spread(
            edge=Decimal("0.04"),
            probability=Decimal("0.90"),  # Lower fee
            spread_pct=Decimal("0.5"),
        )
        # At 50%, fee ≈ 1.56% which is higher than at 90% (fee ≈ 0.09%)
        # Fee at 0.5: C * 0.25 * (0.5 * 0.5)^2 = 0.0156 = 1.56%
        # Total cost at 0.5: 1.56% + 0.5% = 2.06%
        # Edge 4% > 2.06% → should cross at 50%
        # Fee at 0.9: C * 0.25 * (0.9 * 0.1)^2 = 0.0009 = 0.09%
        # Total cost at 0.9: 0.09% + 0.5% = 0.59%
        # Edge 4% > 0.59% → should cross at 90%
        assert should_mid
        assert should_extreme

    def test_reason_includes_details(self) -> None:
        """Reason string should contain edge and threshold info."""
        _, reason = self.policy.should_cross_spread(
            edge=Decimal("0.05"),
            probability=Decimal("0.50"),
            spread_pct=Decimal("1.0"),
        )
        assert "edge" in reason or "threshold" in reason


class TestExecuteMakerOrder:
    """Tests for MakerPolicy.execute_maker_order with mocked callbacks."""

    @pytest.mark.asyncio
    async def test_maker_order_fills(self) -> None:
        """Maker order that fills returns success status."""
        place_cb = AsyncMock(return_value={"success": True, "order_id": "order_123"})
        status_cb = AsyncMock(side_effect=[
            {"status": "PENDING"},
            {"status": "FILLED"},  # Second check finds it filled
        ])
        cancel_cb = AsyncMock()

        policy = MakerPolicy(
            maker_timeout_s=5.0,
            place_limit_order_cb=place_cb,
            get_order_status_cb=status_cb,
            cancel_order_cb=cancel_cb,
        )

        order_data = {
            "makerAmount": 1000000,
            "takerAmount": 1000000,
            "side": 0,
            "tokenId": 12345,
        }

        result = await policy.execute_maker_order(
            order_data=order_data,
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
        )

        assert result["status"] == "filled"
        assert result["order_id"] == "order_123"

    @pytest.mark.asyncio
    async def test_maker_order_times_out(self) -> None:
        """Maker order that doesn't fill times out and is cancelled."""
        place_cb = AsyncMock(return_value={"success": True, "order_id": "order_456"})
        status_cb = AsyncMock(return_value={"status": "PENDING"})  # Never fills
        cancel_cb = AsyncMock()

        policy = MakerPolicy(
            maker_timeout_s=1.0,  # Short timeout for test speed
            place_limit_order_cb=place_cb,
            get_order_status_cb=status_cb,
            cancel_order_cb=cancel_cb,
        )

        order_data = {
            "makerAmount": 1000000,
            "takerAmount": 1000000,
            "side": 0,
            "tokenId": 12345,
        }

        result = await policy.execute_maker_order(
            order_data=order_data,
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
        )

        assert result["status"] == "timed_out"
        cancel_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_placement_failure(self) -> None:
        """If place limit order fails, return error status."""
        place_cb = AsyncMock(return_value={
            "success": False,
            "error": "rate_limit_exceeded",
        })

        policy = MakerPolicy(
            maker_timeout_s=5.0,
            place_limit_order_cb=place_cb,
        )

        order_data = {
            "makerAmount": 1000000,
            "takerAmount": 1000000,
            "side": 0,
            "tokenId": 12345,
        }

        result = await policy.execute_maker_order(
            order_data=order_data,
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
        )

        assert result["status"] == "placement_failed"
        assert "error" in result


class TestExecuteForcedClose:
    """Tests for MakerPolicy.execute_forced_close."""

    @pytest.mark.asyncio
    async def test_forced_close_with_callback(self) -> None:
        """Forced close sends market order via callback."""
        market_cb = AsyncMock(return_value={
            "success": True,
            "order_id": "market_789",
        })

        policy = MakerPolicy(place_market_order_cb=market_cb)

        result = await policy.execute_forced_close({
            "makerAmount": 1000000,
            "takerAmount": 1000000,
            "side": 1,
            "tokenId": 12345,
        })

        assert result["success"]
        assert result["order_id"] == "market_789"
        assert result["status"] == "market_close"

    @pytest.mark.asyncio
    async def test_forced_close_without_callback(self) -> None:
        """Without callback, forced close returns error."""
        policy = MakerPolicy()

        result = await policy.execute_forced_close({
            "makerAmount": 1000000,
            "takerAmount": 1000000,
            "side": 1,
            "tokenId": 12345,
        })

        assert not result["success"]
        assert result["status"] == "no_market_callback"
