"""
Tests for the v2 production improvements — circuit breakers, order guard,
data resilience, market filters, performance tracker, position manager,
monitor, and alerting modules.

Run with: pytest tests/test_live_improvements.py -v
"""

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

# ── Circuit Breaker v2 Tests ──────────────────────────────────────────


class TestCircuitBreakerV2:
    """Test the enhanced CircuitBreakerManager (total drawdown, exposure, cooldown)."""

    @pytest.mark.asyncio
    async def test_total_drawdown_permanent_block(self, tmp_path):
        """Test that total drawdown > max_total_drawdown_pct permanently blocks."""
        from src.risk.circuit_breakers import CircuitBreakerManager

        db_path = str(tmp_path / "test_total_dd.db")
        balance = Decimal("1000")
        peak_seen = Decimal("1000")

        async def balance_provider():
            return balance

        cb = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=balance_provider,
            max_total_drawdown_pct=Decimal("25.0"),
        )
        await cb.start()

        # Set peak by having balance at peak first
        result = await cb.check_total_drawdown()
        assert result is None, f"Expected no drawdown initially: {result}"

        # "Lose" 30% — should trigger permanent block
        global_balance_ref = balance

        async def reduced_balance():
            return Decimal("700")  # 30% loss

        cb._balance_provider = reduced_balance
        result = await cb.check_total_drawdown()
        assert result is not None, "Expected total drawdown to trigger"
        assert "total_drawdown" in result.lower()

        # Verify permanent block
        assert cb._state.total_drawdown_blocked is True
        blocked, reason = await cb.is_trading_blocked()
        assert blocked is True
        assert "total_drawdown" in reason

        await cb.stop()

    @pytest.mark.asyncio
    async def test_exposure_per_market_limit(self, tmp_path):
        """Test that per-market exposure cap rejects orders exceeding it."""
        from src.risk.circuit_breakers import CircuitBreakerManager

        db_path = str(tmp_path / "test_exposure.db")
        balance = Decimal("1000")

        async def balance_provider():
            return balance

        cb = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=balance_provider,
            max_exposure_per_market_pct=Decimal("10.0"),  # 10% of 1000 = 100
        )
        await cb.start()

        # Order of 50 USDC should be OK (50 < 100)
        result = await cb.check_exposure_per_market("asset-1", Decimal("50"))
        assert result is None, f"Expected OK: {result}"

        # Order of 150 USDC should fail (150 > 100)
        result = await cb.check_exposure_per_market("asset-1", Decimal("150"))
        assert result is not None, "Expected exposure limit to trigger"

        await cb.stop()

    @pytest.mark.asyncio
    async def test_total_exposure_limit(self, tmp_path):
        """Test that total exposure cap rejects orders exceeding it."""
        from src.risk.circuit_breakers import CircuitBreakerManager

        db_path = str(tmp_path / "test_total_exp.db")
        balance = Decimal("1000")

        async def balance_provider():
            return balance

        cb = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=balance_provider,
            max_total_exposure_pct=Decimal("50.0"),  # 50% of 1000 = 500
        )
        await cb.start()

        # Order of 300 USDC should be OK
        result = await cb.check_total_exposure(Decimal("300"))
        assert result is None, f"Expected OK: {result}"

        # Order of 600 USDC should fail
        result = await cb.check_total_exposure(Decimal("600"))
        assert result is not None, "Expected total exposure limit to trigger"

        await cb.stop()

    @pytest.mark.asyncio
    async def test_failure_cooldown(self, tmp_path):
        """Test that max_consecutive_failures triggers cooldown."""
        from src.risk.circuit_breakers import CircuitBreakerManager

        db_path = str(tmp_path / "test_cooldown.db")
        balance = Decimal("1000")

        async def balance_provider():
            return balance

        cb = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=balance_provider,
            max_consecutive_failures=3,
            failure_window_seconds=60,
            cooldown_seconds=5,
        )
        await cb.start()

        # Record 3 failures
        for _ in range(3):
            cb.record_failure()

        # Cooldown should be active
        result = await cb.check_failure_cooldown()
        assert result is not None, "Expected cooldown to be active"

        # Wait for cooldown to expire
        await asyncio.sleep(5.5)

        # Cooldown should be over
        result = await cb.check_failure_cooldown()
        assert result is None, f"Expected cooldown to expire: {result}"

        await cb.stop()

    @pytest.mark.asyncio
    async def test_check_all_breakers_unified(self, tmp_path):
        """Test the unified check_all_breakers gate."""
        from src.risk.circuit_breakers import CircuitBreakerManager

        db_path = str(tmp_path / "test_unified.db")
        balance = Decimal("1000")

        async def balance_provider():
            return balance

        cb = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=balance_provider,
            max_exposure_per_market_pct=Decimal("10.0"),
            max_total_exposure_pct=Decimal("50.0"),
            reserve_pct=Decimal("20.0"),
        )
        await cb.start()

        # Small order should pass all checks
        allowed, reason = await cb.check_all_breakers("asset-1", Decimal("10"))
        assert allowed is True, f"Expected allowed: {reason}"

        # Large order should fail exposure
        allowed, reason = await cb.check_all_breakers("asset-1", Decimal("200"))
        assert allowed is False, "Expected blocked by exposure limit"

        await cb.stop()

    @pytest.mark.asyncio
    async def test_available_cash_tracking(self, tmp_path):
        """Test get_available_cash with pending buys."""
        from src.risk.circuit_breakers import CircuitBreakerManager

        db_path = str(tmp_path / "test_cash.db")
        balance = Decimal("1000")

        async def balance_provider():
            return balance

        cb = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=balance_provider,
        )
        await cb.start()

        available = await cb.get_available_cash()
        assert available == Decimal("1000"), "No pending buys yet"

        # Track a pending buy
        cb.track_pending_buy("order-1", Decimal("200"))
        available = await cb.get_available_cash()
        assert available == Decimal("800"), f"Expected 800, got {available}"

        # Remove pending buy
        cb.untrack_pending_buy("order-1")
        available = await cb.get_available_cash()
        assert available == Decimal("1000"), f"Expected 1000, got {available}"

        await cb.stop()


# ── OrderGuard Tests ──────────────────────────────────────────────────


class TestOrderGuard:
    """Test the OrderGuard (clean_start, watchdog, WS disconnect)."""

    @pytest.mark.asyncio
    async def test_clean_start_cancels_all(self):
        """Test that clean_start cancels all orders."""
        from src.live.order_guard import OrderGuard

        cancelled = []

        async def cancel_all_cb():
            cancelled.append("all")

        async def cancel_order_cb(oid):
            cancelled.append(oid)

        async def fetch_open_cb():
            return [("ord-1", time.time()), ("ord-2", time.time())]

        guard = OrderGuard(cancel_all_cb, cancel_order_cb, fetch_open_cb)
        await guard.clean_start()
        assert "all" in cancelled

    @pytest.mark.asyncio
    async def test_ws_disconnect_pauses_trading(self):
        """Test that WS disconnect pauses trading."""
        from src.live.order_guard import OrderGuard

        async def cancel_all_cb():
            pass

        async def cancel_order_cb(oid):
            pass

        async def fetch_open_cb():
            return []

        guard = OrderGuard(cancel_all_cb, cancel_order_cb, fetch_open_cb)

        assert await guard.is_trading_paused() is False
        await guard.on_ws_disconnect()
        assert await guard.is_trading_paused() is True

        await guard.on_ws_reconnect()
        assert await guard.is_trading_paused() is True  # Still paused until book sync

        await guard.resume_trading()
        assert await guard.is_trading_paused() is False

    @pytest.mark.asyncio
    async def test_watchdog_cancels_stale_orders(self):
        """Test that OrderWatchdog cancels stale orders."""
        from src.live.order_guard import OrderWatchdog

        cancelled = []

        async def cancel_cb(oid):
            cancelled.append(oid)

        # Return an order that is 200 seconds old (past default 120s max age)
        now = time.time()

        async def fetch_cb():
            return [("stale-ord", now - 200)]

        watchdog = OrderWatchdog(cancel_cb, fetch_cb, max_age_s=120, interval_s=1)
        watchdog._running = True

        # Manually trigger check (instead of waiting for interval)
        await watchdog._check_and_cancel()
        assert "stale-ord" in cancelled, "Stale order should have been cancelled"

        watchdog._running = False

    @pytest.mark.asyncio
    async def test_watchdog_ignores_fresh_orders(self):
        """Test that OrderWatchdog does NOT cancel fresh orders."""
        from src.live.order_guard import OrderWatchdog

        cancelled = []

        async def cancel_cb(oid):
            cancelled.append(oid)

        now = time.time()

        async def fetch_cb():
            return [("fresh-ord", now - 10)]  # Only 10 seconds old

        watchdog = OrderWatchdog(cancel_cb, fetch_cb, max_age_s=120, interval_s=1)
        watchdog._running = True
        await watchdog._check_and_cancel()
        assert len(cancelled) == 0, "Fresh order should NOT be cancelled"

        watchdog._running = False


# ── Data Resilience Tests ─────────────────────────────────────────────


class TestResilientWebSocket:
    """Test the ResilientWebSocketClient (zombie, dedup, book sync)."""

    @pytest.mark.asyncio
    async def test_event_deduplication(self):
        """Test that duplicate events are detected and skipped."""
        from src.live.data_resilience import EventDeduplicator

        dedup = EventDeduplicator(max_hashes=100)

        # First time — not a duplicate
        assert dedup.is_duplicate("hash-1") is False
        assert dedup.is_duplicate("hash-2") is False

        # Second time — IS a duplicate
        assert dedup.is_duplicate("hash-1") is True
        assert dedup.is_duplicate("hash-2") is True

        # Unknown hash — not a duplicate
        assert dedup.is_duplicate("hash-3") is False

        dedup.clear()
        assert dedup.is_duplicate("hash-1") is False  # Cache cleared

    @pytest.mark.asyncio
    async def test_book_snapshot_fetcher_timeout(self):
        """Test BookSnapshotFetcher handles timeouts gracefully."""
        from src.live.data_resilience import BookSnapshotFetcher

        fetcher = BookSnapshotFetcher(clob_api_base="https://nonexistent.example.com")
        result = await fetcher.fetch_snapshot("token-123")
        assert result is None, "Should return None on timeout/error"

    @pytest.mark.asyncio
    async def test_connection_health_tracking(self):
        """Test ConnectionHealth dataclass fields."""
        from src.live.data_resilience import ConnectionHealth

        health = ConnectionHealth()
        assert health.connected is False
        assert health.book_synced is False
        assert health.reconnect_count == 0
        assert health.zombie_count == 0

        health.connected = True
        health.book_synced = True
        health.reconnect_count = 3
        assert health.connected is True
        assert health.book_synced is True


# ── Market Qualifier Tests ────────────────────────────────────────────


class TestMarketQualifier:
    """Test the MarketQualifier filters."""

    def setup_method(self):
        from src.live.market_filter import MarketQualifier
        self.qualifier = MarketQualifier(
            min_prob=Decimal("0.30"),
            max_prob=Decimal("0.70"),
            min_volume_24h=Decimal("5000"),
            min_hours_to_resolution=336,
        )

    def test_probability_out_of_range_low(self):
        """Test that probability below min_prob is filtered."""
        result = self.qualifier.check_probability_range(Decimal("0.10"))
        assert result is not None
        assert "probability_below_min" in result

    def test_probability_out_of_range_high(self):
        """Test that probability above max_prob is filtered."""
        result = self.qualifier.check_probability_range(Decimal("0.90"))
        assert result is not None
        assert "probability_above_max" in result

    def test_probability_in_range(self):
        """Test that probability in range is accepted."""
        result = self.qualifier.check_probability_range(Decimal("0.50"))
        assert result is None

    def test_opportunity_window_outside(self):
        """Test that outside the opportunity window is filtered."""
        now = time.time()
        # Market ends in 10 minutes (600s), window is 90s — too early
        end_time = now + 600
        result = self.qualifier.check_opportunity_window(end_time, "15m", now)
        assert result is not None
        assert "outside_opportunity_window" in result

    def test_opportunity_window_inside(self):
        """Test that inside the opportunity window is accepted."""
        now = time.time()
        # Market ends in 30 seconds, window is 90s — inside window
        end_time = now + 30
        result = self.qualifier.check_opportunity_window(end_time, "15m", now)
        assert result is None

    def test_volume_below_min(self):
        """Test that volume below minimum is filtered."""
        result = self.qualifier.check_volume(Decimal("100"))
        assert result is not None
        assert "volume_below_min" in result

    def test_volume_above_min(self):
        """Test that volume above minimum is accepted."""
        result = self.qualifier.check_volume(Decimal("10000"))
        assert result is None

    def test_time_to_resolution_too_close(self):
        """Test that markets too close to resolution are filtered."""
        now = time.time()
        # Market ends in 24 hours (86400s) — less than 336h requirement
        end_time = now + 86400
        result = self.qualifier.check_time_to_resolution(end_time, now)
        assert result is not None
        assert "too_close_to_resolution" in result

    def test_time_to_resolution_far_enough(self):
        """Test that markets far enough from resolution are accepted."""
        now = time.time()
        # Market ends in 500 hours (1,800,000s) — more than 336h
        end_time = now + 1800000
        result = self.qualifier.check_time_to_resolution(end_time, now)
        assert result is None

    def test_dynamic_min_edge_provider(self):
        """Test dynamic min_edge adjustment via provider."""
        from src.live.market_filter import MarketQualifier

        calls = []

        def dynamic_provider():
            calls.append(1)
            return Decimal("0.08")

        qualifier = MarketQualifier(
            min_prob=Decimal("0.30"),
            max_prob=Decimal("0.70"),
            dynamic_min_edge_provider=dynamic_provider,
        )

        effective = qualifier.get_min_edge(Decimal("0.05"))
        assert effective == Decimal("0.08"), "Should use dynamic edge (max of static and dynamic)"
        assert len(calls) == 1

    def test_check_signal_all_filters(self):
        """Test the unified check_signal method with all filters."""
        now = time.time()
        signal = {
            "probability": "0.50",
            "ev": "0.10",
            "asset_id": "test-asset",
            "market_type": "15m",
        }
        # Market ends far in future (>336h) for resolution check,
        # but candle ends within 90s for opportunity window
        far_future = now + (400 * 3600)  # 400 hours > 336h requirement
        market_meta = {
            "volume_24h": Decimal("10000"),
            "end_time_s": far_future,              # Market resolution time
            "candle_end_time_s": now + 30,          # Candle end (within 90s window)
        }
        result = self.qualifier.check_signal(signal, market_meta, Decimal("0.05"))
        assert result is None, f"Expected all filters to pass: {result}"


# ── Performance Tracker Tests ─────────────────────────────────────────


class TestPerformanceTracker:
    """Test the PerformanceTracker (MAE, dynamic min_edge)."""

    @pytest.mark.asyncio
    async def test_mae_calculation(self, tmp_path):
        """Test that MAE is correctly calculated from recorded predictions."""
        from src.live.performance_tracker import PerformanceTracker

        db_path = str(tmp_path / "test_perf.db")
        tracker = PerformanceTracker(db_path=db_path, window_size=10)
        await tracker.start()

        # Record predictions with known errors
        await tracker.record_prediction("asset-1", Decimal("0.60"), True)   # error = 0.40
        await tracker.record_prediction("asset-2", Decimal("0.40"), False)  # error = 0.40
        await tracker.record_prediction("asset-3", Decimal("0.80"), True)   # error = 0.20

        # MAE = (0.40 + 0.40 + 0.20) / 3 = 0.3333...
        expected_mae = (Decimal("0.40") + Decimal("0.40") + Decimal("0.20")) / Decimal("3")
        assert tracker.prediction_count == 3
        assert tracker.mae == expected_mae, f"Expected {expected_mae}, got {tracker.mae}"

        await tracker.stop()

    @pytest.mark.asyncio
    async def test_adjusted_min_edge_increases_with_mae(self, tmp_path):
        """Test that min_edge increases as MAE grows."""
        from src.live.performance_tracker import PerformanceTracker

        db_path = str(tmp_path / "test_edge.db")
        tracker = PerformanceTracker(
            db_path=db_path,
            base_min_edge=Decimal("0.05"),
            mae_adjustment_factor=Decimal("1.0"),
            max_min_edge=Decimal("0.20"),
        )
        await tracker.start()

        # No predictions yet — MAE is 0
        assert tracker.adjusted_min_edge == Decimal("0.05")

        # Add predictions with 20% MAE
        await tracker.record_prediction("a", Decimal("0.60"), True)  # error = 0.40
        await tracker.record_prediction("b", Decimal("0.40"), False)  # error = 0.40

        # MAE = 0.40, adjustment = 0.40 * 1.0 = 0.40, cap at 0.20
        assert tracker.adjusted_min_edge == Decimal("0.20"), f"Got {tracker.adjusted_min_edge}"

        await tracker.stop()

    @pytest.mark.asyncio
    async def test_mae_capped_at_max(self, tmp_path):
        """Test that adjusted_min_edge cannot exceed max_min_edge."""
        from src.live.performance_tracker import PerformanceTracker

        db_path = str(tmp_path / "test_cap.db")
        tracker = PerformanceTracker(
            db_path=db_path,
            base_min_edge=Decimal("0.05"),
            mae_adjustment_factor=Decimal("5.0"),
            max_min_edge=Decimal("0.15"),
        )
        await tracker.start()

        # Very bad predictions
        await tracker.record_prediction("a", Decimal("0.90"), False)  # error = 0.90
        await tracker.record_prediction("b", Decimal("0.10"), True)   # error = 0.90

        # MAE = 0.90, adjustment = 0.90 * 5.0 = 4.50, capped at 0.15
        assert tracker.adjusted_min_edge == Decimal("0.15"), f"Got {tracker.adjusted_min_edge}"

        await tracker.stop()


# ── Position Manager Tests ────────────────────────────────────────────


class TestPositionManager:
    """Test the PositionManager (TP/SL, position age limits)."""

    @pytest.mark.asyncio
    async def test_open_and_close_position(self):
        """Test opening and closing a position."""
        from src.live.position_manager import PositionManager

        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(force_close_cb=force_close_cb)
        await pm.start()

        pm.open_position(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )

        assert pm.get_position("asset-1") is not None
        assert len(pm.get_all_positions()) == 1

        pm.close_position("asset-1")
        assert pm.get_position("asset-1") is None

        await pm.stop()

    @pytest.mark.asyncio
    async def test_stop_loss_triggers_force_close(self):
        """Test that stop-loss level triggers force close."""
        from src.live.position_manager import PositionManager

        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            stop_loss_pct=Decimal("20.0"),  # 20% SL
        )
        await pm.start()

        pm.open_position(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )

        # Price drops 30% (to 0.35) — should trigger SL at 0.40
        result = await pm.update_price("asset-1", Decimal("0.35"))
        assert result == "stop_loss", f"Expected stop_loss, got {result}"
        assert len(force_closed) == 1
        assert force_closed[0]["force_close_reason"] == "stop_loss"

        # Position should be removed
        assert pm.get_position("asset-1") is None

        await pm.stop()

    @pytest.mark.asyncio
    async def test_take_profit_triggers_force_close(self):
        """Test that take-profit level triggers force close."""
        from src.live.position_manager import PositionManager

        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            take_profit_pct=Decimal("50.0"),  # 50% TP
        )
        await pm.start()

        pm.open_position(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )

        # Price rises to 0.80 — TP at 0.75 (0.50 * 1.5)
        result = await pm.update_price("asset-1", Decimal("0.80"))
        assert result == "take_profit", f"Expected take_profit, got {result}"
        assert len(force_closed) == 1
        assert force_closed[0]["force_close_reason"] == "take_profit"

        await pm.stop()

    @pytest.mark.asyncio
    async def test_position_age_limit(self):
        """Test that positions exceeding max age are force-closed."""
        from src.live.position_manager import PositionManager

        force_closed = []
        frozen_time = time.time() - 120  # 2 minutes ago (more than 1 cycle of 1 min)

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            max_position_age_cycles=1,
            cycle_duration_minutes=1,  # 1 minute per cycle
        )
        await pm.start()

        pm.open_position(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )
        # Override entry time to be in the past
        pos = pm.get_position("asset-1")
        if pos:
            object.__setattr__(pos, "entry_time", frozen_time)

        closed = await pm.check_age_limit()
        assert "asset-1" in closed, f"Expected asset-1 in {closed}"
        assert len(force_closed) == 1

        await pm.stop()

    @pytest.mark.asyncio
    async def test_no_tp_sl_disabled(self):
        """Test positions without TP/SL don't trigger price checks."""
        from src.live.position_manager import PositionManager

        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            take_profit_pct=Decimal("0"),   # TP disabled
            stop_loss_pct=Decimal("0"),     # SL disabled
        )
        await pm.start()

        pm.open_position(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )

        # Extreme price moves should NOT trigger anything
        result = await pm.update_price("asset-1", Decimal("0.99"))
        assert result is None, f"Expected no action, got {result}"

        result = await pm.update_price("asset-1", Decimal("0.01"))
        assert result is None, f"Expected no action, got {result}"

        assert len(force_closed) == 0

        await pm.stop()


# ── Market Qualifier Signal Tests ─────────────────────────────────────


class TestMarketQualifierSignal:
    """Test the unified check_signal method."""

    def test_signal_accepted_all_good(self):
        """Test that a good signal passes all filters."""
        from src.live.market_filter import MarketQualifier

        q = MarketQualifier(
            min_prob=Decimal("0.30"),
            max_prob=Decimal("0.70"),
            opportunity_windows={
                "15m": {"window_before_end_s": 90},
                "default": {"window_before_end_s": 60},
            },
        )

        now = time.time()
        signal = {
            "probability": "0.50",
            "ev": "0.10",
            "asset_id": "good-asset",
            "market_type": "15m",
        }
        far_future = now + (400 * 3600)
        meta = {
            "volume_24h": Decimal("10000"),
            "end_time_s": far_future,                   # Market resolution >336h
            "candle_end_time_s": now + 30,              # Candle ends within 90s window
        }
        result = q.check_signal(signal, meta, Decimal("0.05"))
        assert result is None

    def test_signal_rejected_low_prob(self):
        """Test that signal with low probability is rejected."""
        from src.live.market_filter import MarketQualifier

        q = MarketQualifier(min_prob=Decimal("0.30"), max_prob=Decimal("0.70"))
        signal = {"probability": "0.10", "ev": "0.10"}
        result = q.check_signal(signal, static_min_edge=Decimal("0.05"))
        assert result is not None
        assert "probability_below_min" in result

    def test_signal_rejected_low_edge(self):
        """Test that signal with low edge is rejected."""
        from src.live.market_filter import MarketQualifier

        q = MarketQualifier(min_prob=Decimal("0.30"), max_prob=Decimal("0.70"))
        signal = {"probability": "0.50", "ev": "0.01"}
        result = q.check_signal(signal, static_min_edge=Decimal("0.05"))
        assert result is not None
        assert "edge_below_min" in result

    def test_signal_no_meta_skips_volume_and_window(self):
        """Test that missing market_meta skips volume/window checks."""
        from src.live.market_filter import MarketQualifier

        q = MarketQualifier(min_prob=Decimal("0.30"), max_prob=Decimal("0.70"))
        signal = {"probability": "0.50", "ev": "0.10"}
        result = q.check_signal(signal, market_meta=None, static_min_edge=Decimal("0.05"))
        assert result is None


# ── CronMonitor Tests ─────────────────────────────────────────────────


class TestCronMonitor:
    """Test the CronMonitor."""

    @pytest.mark.asyncio
    async def test_balance_check_ok(self, tmp_path):
        """Test that balance check passes when on-chain matches expected."""
        from src.live.monitor import CronMonitor

        db_path = str(tmp_path / "test_monitor.db")
        on_chain = Decimal("1000")
        expected = Decimal("1000")

        async def balance_provider():
            return on_chain

        async def expected_provider():
            return expected

        monitor = CronMonitor(
            db_path=db_path,
            balance_provider=balance_provider,
            expected_balance_provider=expected_provider,
            balance_discrepancy_threshold=Decimal("1.0"),
        )
        await monitor.start()

        result = await monitor.check_balance()
        assert result["status"] == "ok", f"Expected ok: {result}"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_balance_discrepancy_triggers_alert(self, tmp_path):
        """Test that large balance discrepancy triggers alert."""
        from src.live.monitor import CronMonitor

        db_path = str(tmp_path / "test_monitor2.db")
        on_chain = Decimal("1000")
        expected = Decimal("800")
        alerted = []

        async def balance_provider():
            return on_chain

        async def expected_provider():
            return expected

        async def critical_cb(msg):
            alerted.append(msg)

        monitor = CronMonitor(
            db_path=db_path,
            balance_provider=balance_provider,
            expected_balance_provider=expected_provider,
            balance_discrepancy_threshold=Decimal("1.0"),
            on_critical_cb=critical_cb,
        )
        await monitor.start()

        result = await monitor.check_balance()
        assert result["status"] == "discrepancy", f"Expected discrepancy: {result}"
        assert len(alerted) == 1

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_reconciliation_skipped_no_callback(self, tmp_path):
        """Test that reconciliation is skipped when no fetch callback."""
        from src.live.monitor import CronMonitor

        db_path = str(tmp_path / "test_monitor3.db")
        monitor = CronMonitor(db_path=db_path)
        await monitor.start()

        result = await monitor.reconcile_positions()
        assert result["status"] == "skipped"

        await monitor.stop()

    def test_health_response_building(self):
        """Test the build_health_response method."""
        from src.live.monitor import CronMonitor

        monitor = CronMonitor()

        health = monitor.build_health_response(
            circuit_breaker_snapshot={
                "status": "HEALTHY",
                "blocked": False,
                "block_reason": "none",
                "daily_loss_pct": "0.00",
                "total_drawdown_blocked": False,
                "total_drawdown_peak_balance": "0",
                "cooldown_active": False,
                "cooldown_remaining_s": 0,
                "failure_count": 0,
                "total_orders_placed": 10,
                "total_orders_filled": 5,
                "daily_start_balance": "1000",
                "total_pnl": "50",
            },
            ws_health={
                "connected": True,
                "book_synced": True,
                "syncing": False,
            },
            order_guard_paused=False,
            position_stats={"open_positions": 2},
            performance_stats={"mae": "0.05", "adjusted_min_edge": "0.05"},
            extra={"dry_run": False, "uptime_seconds": 3600, "last_error": ""},
        )

        assert health["status"] == "OK"
        assert health["circuit_breakers"]["blocked"] is False
        assert health["websocket"]["connected"] is True
        assert health["positions"]["open_positions"] == 2


# ── AlertManager Tests ────────────────────────────────────────────────


class TestAlertManager:
    """Test the AlertManager."""

    @pytest.mark.asyncio
    async def test_alert_rate_limiting(self):
        """Test that same-type alerts are rate-limited."""
        from src.live.alerting import AlertManager

        manager = AlertManager()
        await manager.start()

        # First alert should be queued
        await manager.send_alert("CRITICAL", "Test1", "Msg1", alert_type="test")
        assert manager._alert_queue.qsize() == 1

        # Second alert of same type should be rate-limited
        await manager.send_alert("CRITICAL", "Test2", "Msg2", alert_type="test")
        assert manager._alert_queue.qsize() == 1  # Rate limited, not queued

        await manager.stop()

    @pytest.mark.asyncio
    async def test_warning_filter(self):
        """Test that WARNING alerts are filtered when disabled."""
        from src.live.alerting import AlertManager

        manager = AlertManager(alert_on_warning=False)
        await manager.start()

        await manager.send_alert("WARNING", "Test", "Msg", alert_type="warn")
        assert manager._alert_queue.qsize() == 0  # Filtered

        await manager.stop()

    @pytest.mark.asyncio
    async def test_different_types_not_rate_limited(self):
        """Test that different alert types are not rate-limited together."""
        from src.live.alerting import AlertManager

        manager = AlertManager()
        await manager.start()

        await manager.send_alert("CRITICAL", "A", "Msg", alert_type="type_a")
        await manager.send_alert("CRITICAL", "B", "Msg", alert_type="type_b")
        assert manager._alert_queue.qsize() == 2  # Both queued (different types)

        await manager.stop()
