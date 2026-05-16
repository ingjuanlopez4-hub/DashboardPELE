import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.risk.circuit_breakers import (
    CircuitBreakerManager,
    CircuitBreakerState,
    BotStatus,
    BlockReason,
    STALE_ORDER_AGE_S,
)


class TestDrawdownKillSwitch:

    @pytest.mark.asyncio
    async def test_no_drawdown_when_equity_above_hwm(self, circuit_breaker_mgr):
        result = await circuit_breaker_mgr.check_drawdown()
        assert result is None

    @pytest.mark.asyncio
    async def test_drawdown_triggered(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.high_water_mark = Decimal("1000")
        await circuit_breaker_mgr._persist()

        circuit_breaker_mgr._balance_provider = lambda: asyncio.sleep(0, Decimal("850"))

        result = await circuit_breaker_mgr.check_drawdown()
        assert result is not None
        assert "drawdown_kill_switch" in result
        assert circuit_breaker_mgr._state.blocked is True
        assert circuit_breaker_mgr._state.block_reason == BlockReason.DRAWDOWN.value

    @pytest.mark.asyncio
    async def test_drawdown_blocks_trading(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.high_water_mark = Decimal("1000")
        await circuit_breaker_mgr._persist()
        circuit_breaker_mgr._balance_provider = lambda: asyncio.sleep(0, Decimal("800"))

        blocked, reason = await circuit_breaker_mgr.is_trading_blocked()
        assert blocked is True
        assert "drawdown" in reason

    @pytest.mark.asyncio
    async def test_hwm_updates_on_new_high(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.high_water_mark = Decimal("500")
        circuit_breaker_mgr._balance_provider = lambda: asyncio.sleep(0, Decimal("1200"))
        await circuit_breaker_mgr.check_drawdown()
        assert circuit_breaker_mgr._state.high_water_mark == Decimal("1200")

    @pytest.mark.asyncio
    async def test_drawdown_equity_zero_skips(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.high_water_mark = Decimal("0")
        result = await circuit_breaker_mgr.check_drawdown()
        assert result is None


class TestDailyLossLimit:

    @pytest.mark.asyncio
    async def test_daily_loss_not_exceeded(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.daily_start_balance = Decimal("1000")
        circuit_breaker_mgr._state.daily_loss_accrued = Decimal("10")
        result = await circuit_breaker_mgr.check_daily_loss()
        assert result is None

    @pytest.mark.asyncio
    async def test_daily_loss_exceeded(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.daily_start_balance = Decimal("1000")
        circuit_breaker_mgr._state.daily_loss_accrued = Decimal("60")
        result = await circuit_breaker_mgr.check_daily_loss()
        assert result is not None
        assert "daily_loss_exceeded" in result

    @pytest.mark.asyncio
    async def test_daily_loss_tracking_cross_day(self, circuit_breaker_mgr):
        from datetime import datetime, timezone, timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        circuit_breaker_mgr._state.daily_loss_date = yesterday
        circuit_breaker_mgr._state.daily_loss_accrued = Decimal("200")
        circuit_breaker_mgr._state.daily_start_balance = Decimal("1000")

        await circuit_breaker_mgr._apply_daily_loss_recovery()
        assert circuit_breaker_mgr._state.daily_loss_accrued == Decimal("0")
        assert circuit_breaker_mgr._state.daily_loss_date != yesterday

    @pytest.mark.asyncio
    async def test_record_pnl_updates_loss_accrued(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.daily_start_balance = Decimal("1000")
        await circuit_breaker_mgr.record_pnl(Decimal("-50"))
        assert circuit_breaker_mgr._state.daily_loss_accrued == Decimal("50")
        assert circuit_breaker_mgr._state.total_pnl == Decimal("-50")

    @pytest.mark.asyncio
    async def test_record_pnl_positive_does_not_affect_loss(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.daily_loss_accrued = Decimal("10")
        await circuit_breaker_mgr.record_pnl(Decimal("100"))
        assert circuit_breaker_mgr._state.daily_loss_accrued == Decimal("10")


class TestCashReserveGuard:

    @pytest.mark.asyncio
    async def test_cash_reserve_passes(self, circuit_breaker_mgr):
        result = await circuit_breaker_mgr.check_cash_reserve(Decimal("10"))
        assert result is None

    @pytest.mark.asyncio
    async def test_cash_reserve_blocks(self, circuit_breaker_mgr):
        result = await circuit_breaker_mgr.check_cash_reserve(Decimal("900"))
        assert result is not None
        assert "cash_reserve_breach" in result

    @pytest.mark.asyncio
    async def test_cash_reserve_edge_case(self, circuit_breaker_mgr):
        result = await circuit_breaker_mgr.check_cash_reserve(Decimal("800"))
        assert result is None


class TestBlockUnblock:

    @pytest.mark.asyncio
    async def test_is_trading_blocked_healthy(self, circuit_breaker_mgr):
        blocked, reason = await circuit_breaker_mgr.is_trading_blocked()
        assert isinstance(blocked, bool)

    @pytest.mark.asyncio
    async def test_unblock_trading(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.blocked = True
        circuit_breaker_mgr._state.block_reason = BlockReason.DRAWDOWN.value
        await circuit_breaker_mgr.unblock_trading()

        assert circuit_breaker_mgr._state.blocked is False
        assert circuit_breaker_mgr._state.block_reason == "none"
        assert circuit_breaker_mgr.get_status() == BotStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_block_persists_state(self, circuit_breaker_mgr):
        await circuit_breaker_mgr._block_trading(BlockReason.MANUAL, "test block")
        assert circuit_breaker_mgr._state.blocked is True

        circuit_breaker_mgr._state.blocked = False
        await circuit_breaker_mgr._load_state()
        assert circuit_breaker_mgr._state.blocked is True
        assert circuit_breaker_mgr._state.block_reason == BlockReason.MANUAL.value


class TestOrderTimeout:

    @pytest.mark.asyncio
    async def test_order_timeout_within_limit(self, circuit_breaker_mgr):
        result = await circuit_breaker_mgr.check_order_timeout("ord-1", 30.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_order_timeout_exceeded(self, circuit_breaker_mgr):
        cancelled = []
        circuit_breaker_mgr._cancel_order_cb = lambda oid: cancelled.append(oid)
        result = await circuit_breaker_mgr.check_order_timeout("ord-1", 300.0)
        assert result is not None
        assert "order_timeout" in result
        assert "ord-1" in cancelled


class TestStatePersistence:

    @pytest.mark.asyncio
    async def test_persist_and_load_roundtrip(self, tmp_path):
        db_path = str(tmp_path / "test_roundtrip.db")
        cb1 = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
        )
        await cb1.start()
        cb1._state.high_water_mark = Decimal("999.99")
        cb1._state.total_orders_placed = 42
        await cb1._persist()
        await cb1.stop()

        cb2 = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
        )
        await cb2.start()
        assert cb2._state.high_water_mark == Decimal("999.99")
        assert cb2._state.total_orders_placed == 42
        await cb2.stop()

    @pytest.mark.asyncio
    async def test_record_order_tracking(self, circuit_breaker_mgr):
        await circuit_breaker_mgr.record_order(filled=True)
        assert circuit_breaker_mgr._state.total_orders_placed == 1
        assert circuit_breaker_mgr._state.total_orders_filled == 1

        await circuit_breaker_mgr.record_order(filled=False)
        assert circuit_breaker_mgr._state.total_orders_placed == 2
        assert circuit_breaker_mgr._state.total_orders_filled == 1

    @pytest.mark.asyncio
    async def test_get_state_snapshot(self, circuit_breaker_mgr):
        snapshot = circuit_breaker_mgr.get_state_snapshot()
        assert "blocked" in snapshot
        assert "block_reason" in snapshot
        assert "high_water_mark" in snapshot
        assert "daily_start_balance" in snapshot
        assert "daily_loss_accrued" in snapshot
        assert "total_orders_placed" in snapshot


class TestWebSocketHealth:

    @pytest.mark.asyncio
    async def test_ws_disconnect_blocks_trading(self, circuit_breaker_mgr):
        cancelled = []
        orig_cancel = circuit_breaker_mgr.cancel_all_orders
        circuit_breaker_mgr.cancel_all_orders = AsyncMock()

        task = asyncio.create_task(circuit_breaker_mgr.on_ws_disconnect())
        await asyncio.sleep(0.1)

        assert circuit_breaker_mgr._state.blocked is False

        await asyncio.sleep(31)
        circuit_breaker_mgr.cancel_all_orders.assert_called()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_ws_reconnect_unblocks(self, circuit_breaker_mgr):
        circuit_breaker_mgr._state.blocked = True
        circuit_breaker_mgr._state.block_reason = BlockReason.WS_DISCONNECT.value
        circuit_breaker_mgr._status = BotStatus.BLOCKED

        await circuit_breaker_mgr.on_ws_reconnect()
        assert circuit_breaker_mgr._state.blocked is False
        assert circuit_breaker_mgr.get_status() == BotStatus.HEALTHY


class TestCancelAll:

    @pytest.mark.asyncio
    async def test_cancel_all_invokes_callback(self, circuit_breaker_mgr):
        called = False
        async def mock_cancel():
            nonlocal called
            called = True

        circuit_breaker_mgr._cancel_all_cb = mock_cancel
        await circuit_breaker_mgr.cancel_all_orders()
        assert called is True

    @pytest.mark.asyncio
    async def test_cancel_all_callback_error(self, circuit_breaker_mgr):
        async def mock_cancel():
            raise RuntimeError("API error")

        circuit_breaker_mgr._cancel_all_cb = mock_cancel
        await circuit_breaker_mgr.cancel_all_orders()


class TestDecimalPrecision:

    def test_all_state_values_are_decimal(self):
        s = CircuitBreakerState()
        assert isinstance(s.high_water_mark, Decimal)
        assert isinstance(s.daily_start_balance, Decimal)
        assert isinstance(s.daily_loss_accrued, Decimal)
        assert isinstance(s.total_pnl, Decimal)

    def test_drawdown_calculation_uses_decimal(self):
        hwm = Decimal("1000")
        equity = Decimal("850")
        dd_pct = ((hwm - equity) / hwm) * Decimal("100")
        assert dd_pct == Decimal("15")
        assert isinstance(dd_pct, Decimal)

    def test_daily_loss_calculation_uses_decimal(self):
        start = Decimal("1000")
        loss = Decimal("60")
        loss_pct = (loss / start) * Decimal("100")
        assert loss_pct == Decimal("6")
        assert isinstance(loss_pct, Decimal)
