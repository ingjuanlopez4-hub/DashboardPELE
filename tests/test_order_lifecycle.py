"""
Tests del módulo Order Lifecycle (src/execution/order_lifecycle.py).

Cubre:
- Place order with timeout
- Retry logic on failure
- Cancel-all on timeout/failure
- Stale order cancellation
- @timeout_cycle decorator
- OrderOpResult comparison
"""

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.order_lifecycle import (
    OrderLifecycleManager,
    OrderOpResult,
    timeout_cycle,
    DEFAULT_OP_TIMEOUT_S,
    DEFAULT_CYCLE_TIMEOUT_S,
    DEFAULT_MAX_RETRIES,
)


# =========================================================================
# OrderOpResult
# =========================================================================

class TestOrderOpResult:

    def test_success_bool_true(self):
        r = OrderOpResult(success=True, order_id="ord-1")
        assert bool(r) is True

    def test_failure_bool_false(self):
        r = OrderOpResult(success=False)
        assert bool(r) is False

    def test_comparison_by_latency(self):
        r1 = OrderOpResult(success=True, latency_ms=100)
        r2 = OrderOpResult(success=True, latency_ms=200)
        assert r1 < r2
        assert r2 > r1

    def test_equality_not_implemented(self):
        r = OrderOpResult(success=True)
        assert r.__eq__("not_a_result") is NotImplemented

    def test_default_values(self):
        r = OrderOpResult(success=True)
        assert r.order_id == ""
        assert r.error == ""
        assert r.latency_ms == 0.0
        assert r.payload == {}


# =========================================================================
# @timeout_cycle decorator
# =========================================================================

class TestTimeoutCycleDecorator:

    @pytest.mark.asyncio
    async def test_decorator_passes_on_success(self):
        """Decorator should pass through successful results."""

        class MockManager:
            _cancel_all_cb = AsyncMock()

            @timeout_cycle(timeout_s=5)
            async def do_thing(self):
                return "success"

        mgr = MockManager()
        result = await mgr.do_thing()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_decorator_cancels_on_timeout(self):
        """Decorator should cancel all orders on timeout."""

        cancel_called = False
        async def mock_cancel(*args, **kwargs):
            nonlocal cancel_called
            cancel_called = True

        class MockManager:
            _cancel_all_cb = mock_cancel

            @timeout_cycle(timeout_s=0.1)
            async def do_thing(self):
                await asyncio.sleep(3600)

        mgr = MockManager()
        with pytest.raises(asyncio.TimeoutError):
            await mgr.do_thing()
        assert cancel_called is True

    @pytest.mark.asyncio
    async def test_decorator_no_cancel_cb(self):
        """Decorator should not crash if no cancel_all callback."""

        class MockManager:
            @timeout_cycle(timeout_s=0.1)
            async def do_thing(self):
                await asyncio.sleep(3600)

        mgr = MockManager()
        with pytest.raises(asyncio.TimeoutError):
            await mgr.do_thing()


# =========================================================================
# OrderLifecycleManager — Place Order With Timeout
# =========================================================================

class TestPlaceOrderWithTimeout:

    @pytest.mark.asyncio
    async def test_successful_placement(self, order_lifecycle, mock_clob):
        """Successful order placement should return success."""
        order_data = {"test": "data"}
        signal = {"asset_id": "0xabc"}
        signature = "0xsig"

        result = await order_lifecycle.place_order_with_timeout(
            order_data, signature, signal,
        )
        assert result.success is True
        assert result.order_id.startswith("ord-")

    @pytest.mark.asyncio
    async def test_timeout_triggers_cancel_all(self, order_lifecycle, mock_clob):
        """Timeout on order placement should cancel all orders."""
        mock_clob._timeout_on.append("post_order")

        order_data = {"test": "data"}
        signal = {"asset_id": "0xabc"}
        signature = "0xsig"

        result = await order_lifecycle.place_order_with_timeout(
            order_data, signature, signal,
        )
        assert result.success is False
        assert "timeout" in result.error

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, order_lifecycle, mock_clob):
        """Should retry on failure up to max_retries."""
        mock_clob._fail_on["post_order"] = 1  # fail first attempt

        order_data = {"test": "data"}
        signal = {"asset_id": "0xabc"}
        signature = "0xsig"

        result = await order_lifecycle.place_order_with_timeout(
            order_data, signature, signal,
        )
        # Should succeed on retry
        assert result.success is True
        assert mock_clob._call_count.get("post_order", 0) == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, order_lifecycle, mock_clob):
        """All retries exhausted should return failure and cancel."""
        mock_clob._fail_on["post_order"] = 100  # always fail

        order_data = {"test": "data"}
        signal = {"asset_id": "123456"}
        signature = "0xsig"

        result = await order_lifecycle.place_order_with_timeout(
            order_data, signature, signal,
        )
        assert result.success is False
        # max_retries=2 means 2 attempts (range(1, 3))
        assert mock_clob._call_count.get("post_order", 0) == DEFAULT_MAX_RETRIES
        # cancel_all should have been called
        assert mock_clob._call_count.get("cancel_all", 0) >= 1

    @pytest.mark.asyncio
    async def test_latency_tracking(self, order_lifecycle, mock_clob):
        """Result should include latency in ms."""
        order_data = {"test": "data"}
        signal = {"asset_id": "0xabc"}
        signature = "0xsig"

        result = await order_lifecycle.place_order_with_timeout(
            order_data, signature, signal,
        )
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_cancel_all_safe_on_error(self, order_lifecycle, mock_clob):
        """_cancel_all_safe should not raise on error."""
        mock_clob.cancel_all_async = AsyncMock(side_effect=RuntimeError("API error"))

        class CustomLifecycle(OrderLifecycleManager):
            def __init__(self):
                super().__init__(cancel_all_func=mock_clob.cancel_all_async)

        cl = CustomLifecycle()
        # Should not raise
        await cl._cancel_all_safe()

    @pytest.mark.asyncio
    async def test_cancel_all_safe_timeout(self, order_lifecycle, mock_clob):
        """_cancel_all_safe timeout should be caught."""
        async def slow_cancel():
            await asyncio.sleep(3600)

        class CustomLifecycle(OrderLifecycleManager):
            def __init__(self):
                super().__init__(
                    cancel_all_func=slow_cancel,
                    op_timeout_s=0.1,
                )

        cl = CustomLifecycle()
        # Should not raise despite timeout
        await cl._cancel_all_safe()


# =========================================================================
# Cancel Stale Orders
# =========================================================================

class TestCancelStaleOrders:

    @pytest.mark.asyncio
    async def test_stale_order_cancelled(self, order_lifecycle, mock_clob):
        """Orders older than max_age_s should be cancelled."""
        # Inject a stale order into mock_clob
        mock_clob._orders["stale-ord"] = {"created_at": time.time() - 300}

        cancelled = await order_lifecycle.cancel_stale_orders(max_age_s=120)
        assert cancelled >= 1
        # The stale order should be in cancelled list
        assert "stale-ord" in mock_clob._cancelled

    @pytest.mark.asyncio
    async def test_fresh_order_not_cancelled(self, mock_clob):
        """Orders within max_age_s should NOT be cancelled."""
        # Use a custom lifecycle with a fetch that returns fresh orders
        async def fresh_fetch() -> list[tuple[str, float]]:
            return [("fresh-ord", time.time())]

        async def cancel_cb(oid: str) -> None:
            mock_clob.cancel_order(oid)

        ol = OrderLifecycleManager(
            fetch_open_orders_func=fresh_fetch,
            cancel_order_func=cancel_cb,
            op_timeout_s=0.5,
        )
        cancelled = await ol.cancel_stale_orders(max_age_s=120)
        assert cancelled == 0

    @pytest.mark.asyncio
    async def test_no_fetch_func_returns_zero(self):
        """Without fetch function, stale check returns 0."""
        ol = OrderLifecycleManager()
        cancelled = await ol.cancel_stale_orders()
        assert cancelled == 0

    @pytest.mark.asyncio
    async def test_fetch_error_returns_zero(self, order_lifecycle):
        """If fetch raises, stale check returns 0."""
        order_lifecycle._fetch_func = AsyncMock(side_effect=RuntimeError("fail"))
        cancelled = await order_lifecycle.cancel_stale_orders()
        assert cancelled == 0


# =========================================================================
# Full Trading Cycle
# =========================================================================

class TestTradingCycle:

    @pytest.mark.asyncio
    async def test_execute_trading_cycle_success(self, order_lifecycle, mock_clob):
        """Full cycle should return success on valid order."""
        signal = {"asset_id": "0xabc"}
        order_data = {"test": "data"}
        signature = "0xsig"

        result = await order_lifecycle.execute_trading_cycle(
            signal, order_data, signature,
        )
        assert result.success is True
        # Order should be tracked in open_orders
        assert result.order_id in order_lifecycle._open_orders

    @pytest.mark.asyncio
    async def test_execute_trading_cycle_failure(self, order_lifecycle, mock_clob):
        """Failed cycle should return failure and not track order."""
        mock_clob._fail_on["post_order"] = 100

        signal = {"asset_id": "0xabc"}
        order_data = {"test": "data"}
        signature = "0xsig"

        result = await order_lifecycle.execute_trading_cycle(
            signal, order_data, signature,
        )
        assert result.success is False
        assert result.order_id not in order_lifecycle._open_orders

    @pytest.mark.asyncio
    async def test_cycle_timeout_cancels_all(self, mock_clob):
        """Full cycle exceeding timeout should cancel all."""

        async def slow_place(od, sig, signal):
            await asyncio.sleep(3600)

        ol = OrderLifecycleManager(
            place_order_func=slow_place,
            cancel_all_func=mock_clob.cancel_all_async,
            cycle_timeout_s=0.1,
        )

        signal = {"asset_id": "0xabc"}
        order_data = {"test": "data"}
        signature = "0xsig"

        result = await ol.execute_trading_cycle(signal, order_data, signature)
        assert result.success is False
        # cancel_all should have been triggered by decorator
        assert mock_clob._call_count.get("cancel_all", 0) >= 1


# =========================================================================
# Cleanup
# =========================================================================

class TestCleanup:

    @pytest.mark.asyncio
    async def test_cleanup_on_startup(self, order_lifecycle, mock_clob):
        """Startup cleanup should cancel all and clear local state."""
        mock_clob._orders["residual"] = {"test": "data"}
        order_lifecycle._open_orders["residual"] = time.time()

        await order_lifecycle.cleanup_on_startup()
        assert len(order_lifecycle._open_orders) == 0

    @pytest.mark.asyncio
    async def test_cleanup_on_shutdown(self, order_lifecycle, mock_clob):
        """Shutdown cleanup should cancel all and clear local state."""
        order_lifecycle._open_orders["ord-1"] = time.time()

        await order_lifecycle.cleanup_on_shutdown()
        assert len(order_lifecycle._open_orders) == 0
        assert mock_clob._call_count.get("cancel_all", 0) >= 1


# =========================================================================
# Regression: issue #142 — float precision in order amounts
# =========================================================================

class TestRegressionDecimalPrecision:

    def test_maker_amount_exact_with_decimal(self):
        """Regression: float(0.29) / 0.01 = 28.999... With Decimal it's exact."""
        price = Decimal("0.29")
        size = Decimal("100.00")
        usdc_decimals = 6
        token_decimals = 6

        price_wei = int(price * Decimal(10 ** usdc_decimals))
        size_wei = int(size * Decimal(10 ** token_decimals))

        # makerAmount for BUY
        maker_amount = int(size_wei * price_wei // (10 ** usdc_decimals))

        # With float: 0.29 * 1_000_000 = 289999.999... -> 289999 (wrong!)
        # With Decimal: 0.29 * 1_000_000 = 290000 exactly
        assert price_wei == 290_000, f"Expected 290000, got {price_wei}"
        assert maker_amount == 29_000_000_000_000 // 1_000_000 == 29_000_000

    def test_taker_amount_exact(self):
        """size_wei conversion must be exact."""
        size = Decimal("100.00")
        size_wei = int(size * Decimal(10 ** 6))
        assert size_wei == 100_000_000

    def test_zero_amounts(self):
        """Zero price/size should not cause division errors."""
        price = Decimal("0")
        size = Decimal("0")
        price_wei = int(price * Decimal(10 ** 6))
        size_wei = int(size * Decimal(10 ** 6))
        maker_amount = int(size_wei * price_wei // (10 ** 6))
        assert maker_amount == 0
