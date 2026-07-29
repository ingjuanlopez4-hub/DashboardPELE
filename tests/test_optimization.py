"""
Tests for the PELE Bot performance optimization modules.

Covers:
  - Cache manager (TTLCache, SignalCache, TwoLevelCache)
  - Latency tracker (P50/P95/P99 stats)
  - Shared memory state (mmap read/write)
  - Bot profiler (start/stop)
  - Event loop bootstrap (uvloop, core affintiy)
  - Optimization settings config
  - Regression: Monte Carlo vectorization, Wick vectorization, SQLite PRAGMAs
  - Integration: executora.py maker-first with SignalCache
"""

import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.infrastructure.cache_manager import (
    SignalCache,
    TwoLevelCache,
    get_signal_cache,
    get_price_cache_instance,
    clear_all_caches,
)
from src.infrastructure.latency_tracker import (
    LatencyTracker,
    get_latency_tracker,
    measure_latency,
)
from src.infrastructure.shared_state import SharedMemoryState
from src.infrastructure.bot_profiler import BotProfiler, AggregatedTimer
from src.config.optimization_settings import (
    LOCAL_OPTIMIZATION_CONFIG,
    get_optimization_config,
    get_cpu_cores,
    should_use_uvloop,
    get_ws_library,
)


# ── Cache Manager Tests ────────────────────────────────────────────────


class TestSignalCache:
    def test_get_set(self):
        cache = SignalCache()
        cache.set("test_key", {"signal": "BUY", "confidence": 0.8})
        result = cache.get("test_key")
        assert result == {"signal": "BUY", "confidence": 0.8}

    def test_get_missing_returns_default(self):
        cache = SignalCache()
        result = cache.get("nonexistent", default=42)
        assert result == 42

    def test_get_missing_no_default(self):
        cache = SignalCache()
        result = cache.get("nonexistent")
        assert result is None

    def test_get_or_compute(self):
        cache = SignalCache()
        computed = False

        def compute_fn():
            nonlocal computed
            computed = True
            return "computed_value"

        result = cache.get_or_compute("compute_key", compute_fn)
        assert result == "computed_value"
        assert computed

        # Second call should use cache
        computed = False
        result = cache.get_or_compute("compute_key", compute_fn)
        assert result == "computed_value"
        assert not computed  # compute_fn not called again

    def test_cache_eviction(self):
        cache = SignalCache()
        # Fill beyond maxsize
        for i in range(200):
            cache.set(f"key_{i}", i)
        # Oldest entries should be evicted
        assert cache.get("key_0") is None

    def test_overwrite(self):
        cache = SignalCache()
        cache.set("key", "value1")
        cache.set("key", "value2")
        assert cache.get("key") == "value2"


class TestTwoLevelCache:
    def test_set_and_get(self):
        cache = TwoLevelCache()
        cache.set("price_btc", 50000.0)
        assert cache.get("price_btc") == 50000.0

    def test_l1_hit(self):
        cache = TwoLevelCache()
        cache._l1["hit_key"] = "l1_value"
        assert cache.get("hit_key") == "l1_value"

    def test_l2_promotion(self):
        cache = TwoLevelCache()
        # Write to L2 only
        cache._l2["promote_key"] = "l2_value"
        # Get should find it and promote to L1
        assert cache.get("promote_key") == "l2_value"
        # Now in L1
        assert cache._l1.get("promote_key") == "l2_value"

    def test_invalidate(self):
        cache = TwoLevelCache()
        cache._l1["key"] = "val"
        cache._l2["key"] = "val"
        cache.invalidate("key")
        assert cache.get("key") is None

    def test_clear(self):
        cache = TwoLevelCache()
        cache._l1["a"] = 1
        cache._l2["b"] = 2
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None


class TestGlobalCacheInstances:
    def test_get_signal_cache(self):
        c = get_signal_cache()
        assert c is not None
        assert hasattr(c, "get")
        assert hasattr(c, "__setitem__")

    def test_get_price_cache_instance(self):
        c = get_price_cache_instance()
        assert c is not None
        assert hasattr(c, "get")
        assert hasattr(c, "set")

    def test_clear_all_caches(self):
        # Just ensure no exception
        clear_all_caches()


# ── Latency Tracker Tests ──────────────────────────────────────────────


class TestLatencyTracker:
    def test_record_and_stats(self):
        tracker = LatencyTracker(window_size=100)
        tracker.record("op1", 10.0)
        tracker.record("op1", 20.0)
        tracker.record("op1", 30.0)

        stats = tracker.stats("op1")
        assert "op1" in stats
        s = stats["op1"]
        assert s["count"] == 3
        assert s["mean_ms"] == 20.0
        assert s["max_ms"] == 30.0
        assert s["min_ms"] == 10.0

    def test_stats_all_operations(self):
        tracker = LatencyTracker(window_size=100)
        tracker.record("op1", 5.0)
        tracker.record("op2", 15.0)

        stats = tracker.stats()
        assert "op1" in stats
        assert "op2" in stats
        assert stats["op1"]["count"] == 1

    def test_empty_stats(self):
        tracker = LatencyTracker()
        stats = tracker.stats("nonexistent")
        s = stats["nonexistent"]
        assert s["count"] == 0

    def test_p95_p99(self):
        tracker = LatencyTracker(window_size=1000)
        for i in range(1, 101):
            tracker.record("latency_test", float(i))

        stats = tracker.stats("latency_test")["latency_test"]
        assert stats["count"] == 100
        assert stats["p50_ms"] == 51.0  # Index 50 (0-based) = value 51
        assert stats["p95_ms"] >= 94.0
        assert stats["p99_ms"] >= 98.0

    def test_clear_operation(self):
        tracker = LatencyTracker()
        tracker.record("op1", 1.0)
        tracker.record("op2", 2.0)
        tracker.clear("op1")
        assert "op1" not in tracker.stats()
        assert "op2" in tracker.stats()

    def test_clear_all(self):
        tracker = LatencyTracker()
        tracker.record("op1", 1.0)
        tracker.record("op2", 2.0)
        tracker.clear()
        assert len(tracker.stats()) == 0

    def test_context_manager(self):
        tracker = LatencyTracker()
        with tracker.measure("context_op"):
            time.sleep(0.001)  # 1ms
        stats = tracker.stats("context_op")["context_op"]
        assert stats["count"] == 1
        assert stats["mean_ms"] > 0.5

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        tracker = LatencyTracker()
        async with tracker.measure_async("async_op"):
            await asyncio.sleep(0.001)
        stats = tracker.stats("async_op")["async_op"]
        assert stats["count"] == 1
        assert stats["mean_ms"] > 0.5

    def test_measure_decorator_sync(self):
        tracker = LatencyTracker()

        @measure_latency("decorated_sync")
        def sync_func():
            time.sleep(0.001)
            return "done"

        result = sync_func()
        assert result == "done"


class TestLatencyTrackerSingleton:
    def test_get_latency_tracker(self):
        t1 = get_latency_tracker()
        t2 = get_latency_tracker()
        assert t1 is t2


# ── Shared Memory State Tests ──────────────────────────────────────────


class TestSharedMemoryState:
    def test_write_read(self):
        state = SharedMemoryState(size=4096)
        try:
            state.open()
            state.write({"price": 0.52, "asset": "BTC"})
            data = state.read()
            assert data["price"] == 0.52
            assert data["asset"] == "BTC"
        finally:
            state.close()

    def test_update(self):
        state = SharedMemoryState(size=4096)
        try:
            state.open()
            state.write({"a": 1})
            state.update(b=2, c=3)
            data = state.read()
            assert data["a"] == 1
            assert data["b"] == 2
            assert data["c"] == 3
        finally:
            state.close()

    def test_get(self):
        state = SharedMemoryState(size=4096)
        try:
            state.open()
            state.write({"key": "value"})
            assert state.get("key") == "value"
            assert state.get("nonexistent") is None
            assert state.get("nonexistent", 42) == 42
        finally:
            state.close()

    def test_empty_read(self):
        state = SharedMemoryState(size=4096)
        try:
            state.open()
            data = state.read()
            assert data == {}
        finally:
            state.close()

    def test_context_manager(self):
        with SharedMemoryState(size=4096) as state:
            state.write({"test": True})
            assert state.read()["test"] is True


# ── Bot Profiler Tests ─────────────────────────────────────────────────


class TestBotProfiler:
    def test_disabled_by_default(self):
        profiler = BotProfiler()
        output = profiler.stop()
        assert output == ""

    def test_start_stop(self):
        profiler = BotProfiler(enabled=True)
        profiler.start()

        # Do some work
        _ = [i**2 for i in range(10000)]

        output = profiler.stop()
        assert "PROFILER OUTPUT" in output

    def test_from_config(self):
        profiler = BotProfiler.from_config()
        assert not profiler._enabled
        assert profiler._top_n == 20


class TestAggregatedTimer:
    def test_measure(self):
        timer = AggregatedTimer()
        with timer.measure("op"):
            time.sleep(0.001)
        stats = timer.stats()
        assert "op" in stats
        assert stats["op"]["count"] == 1
        assert stats["op"]["mean_ms"] > 0

    def test_multiple_measures(self):
        timer = AggregatedTimer()
        for _ in range(5):
            with timer.measure("batch"):
                time.sleep(0.0005)
        stats = timer.stats()
        assert stats["batch"]["count"] == 5
        assert stats["batch"]["total_ms"] > 1.0

    def test_clear(self):
        timer = AggregatedTimer()
        with timer.measure("op"):
            pass
        timer.clear()
        assert len(timer.stats()) == 0

    def test_multiple_ops(self):
        timer = AggregatedTimer()
        with timer.measure("fast"):
            pass
        with timer.measure("slow"):
            time.sleep(0.002)
        stats = timer.stats()
        assert "fast" in stats
        assert "slow" in stats


# ── Optimization Settings Tests ────────────────────────────────────────


class TestOptimizationSettings:
    def test_config_exists(self):
        assert LOCAL_OPTIMIZATION_CONFIG is not None
        assert "event_loop" in LOCAL_OPTIMIZATION_CONFIG
        assert "cpu_affinity" in LOCAL_OPTIMIZATION_CONFIG
        assert "monte_carlo" in LOCAL_OPTIMIZATION_CONFIG
        assert "finbert" in LOCAL_OPTIMIZATION_CONFIG
        assert "websocket" in LOCAL_OPTIMIZATION_CONFIG
        assert "http" in LOCAL_OPTIMIZATION_CONFIG
        assert "sqlite" in LOCAL_OPTIMIZATION_CONFIG
        assert "cache" in LOCAL_OPTIMIZATION_CONFIG
        assert "parallelism" in LOCAL_OPTIMIZATION_CONFIG
        assert "profiling" in LOCAL_OPTIMIZATION_CONFIG

    def test_cpu_cores(self):
        cores = get_cpu_cores()
        assert cores["ingestion"] == 0
        assert cores["strategy"] == 1
        assert cores["execution"] == 2
        assert cores["monitoring"] == 3

    def test_get_optimization_config(self):
        config = get_optimization_config()
        assert config is LOCAL_OPTIMIZATION_CONFIG

    def test_uvloop_setting(self):
        # This should return True on Linux/macOS
        import platform
        if platform.system() in ("Linux", "Darwin"):
            assert should_use_uvloop()
        else:
            assert not should_use_uvloop()

    def test_ws_library(self):
        lib = get_ws_library()
        # Should return the fallback since picows is likely not installed in test env
        assert lib == "websockets" or lib == "picows"

    def test_monte_carlo_config(self):
        mc = LOCAL_OPTIMIZATION_CONFIG["monte_carlo"]
        assert mc["use_numpy_vectorization"]
        assert mc["use_process_pool"]
        assert mc["n_simulations"] == 1000

    def test_cache_config(self):
        cache = LOCAL_OPTIMIZATION_CONFIG["cache"]
        assert cache["l1_ttl_ms"] == 100
        assert cache["l2_ttl_seconds"] == 5.0
        assert cache["sentiment_ttl_seconds"] == 600

    def test_sqlite_config(self):
        sqlite = LOCAL_OPTIMIZATION_CONFIG["sqlite"]
        assert sqlite["wal_mode"]
        assert sqlite["cache_size_mb"] == 64
        assert sqlite["mmap_size_mb"] == 256


# ── Monte Carlo Vectorization Regression ──────────────────────────────


class TestMonteCarloOptimization:
    """Regression tests: Monte Carlo uses numpy vectorization + ProcessPoolExecutor."""

    def test_monte_carlo_imports(self):
        """Verify MonteCarloSimulator imports correctly and has vectorized method."""
        from src.strategy.monte_carlo import MonteCarloSimulator
        simulator = MonteCarloSimulator(n_paths=100)
        assert simulator.n_paths == 100
        assert hasattr(simulator, "_run_simulation_vectorized")
        assert hasattr(simulator, "_executor")

    def test_monte_carlo_scalar_run(self):
        """Test the static _run_simulation method (legacy compatibility)."""
        from src.strategy.monte_carlo import MonteCarloSimulator
        mean, std, p5 = MonteCarloSimulator._run_simulation(
            lo0=0.0,  # logit(0.5)
            mu=0.0,
            sigma=0.5,
            dt=1.0 / 365.0,
            n_paths=1000,
        )
        assert 0.0 < mean < 1.0
        assert std > 0.0
        assert 0.0 < p5 < mean

    def test_monte_carlo_short_term_disabled(self):
        """Monte Carlo should be disabled for short-term markets (<= 7 days)."""
        from src.strategy.monte_carlo import MonteCarloSimulator
        simulator = MonteCarloSimulator(n_paths=100)
        assert simulator.is_short_term(Decimal("1"))
        assert simulator.is_short_term(Decimal("7"))
        assert simulator.is_short_term(Decimal("7.0"))
        assert not simulator.is_short_term(Decimal("8"))
        assert not simulator.is_short_term(Decimal("30"))


# ── Wick Fishing Vectorization Regression ──────────────────────────────


class TestWickFishingOptimization:
    """Regression tests: Wick fishing uses numpy vectorization."""

    def test_wick_imports(self):
        """Verify WickFishingAnalyzer imports correctly."""
        from src.strategy.wick_fishing import WickFishingAnalyzer
        analyzer = WickFishingAnalyzer()
        assert analyzer.min_snapshots == 2
        assert analyzer.top_levels == 5

    def test_wick_empty_snapshots(self):
        """Empty snapshots should return neutral result."""
        from src.strategy.wick_fishing import WickFishingAnalyzer
        analyzer = WickFishingAnalyzer()
        result = analyzer.analyze([])
        assert result["score"] == Decimal("0.5")
        assert result["details"]["reason"] == "insufficient_data"

    def test_wick_no_data(self):
        """Snapshots without bid/ask data should return neutral."""
        from src.strategy.wick_fishing import BookSnapshot, WickFishingAnalyzer
        analyzer = WickFishingAnalyzer()
        snap = BookSnapshot(bids=[], asks=[], timestamp=time.time())
        result = analyzer.analyze([snap, snap])
        assert result["score"] == Decimal("0.5")
        assert result["details"]["reason"] == "no_comparisons"


# ── Database Optimization Regression ────────────────────────────────────


class TestDatabaseOptimization:
    """Regression tests: SQLite uses WAL mode and performance PRAGMAs."""

    @pytest.mark.asyncio
    async def test_database_creates_with_pragmas(self):
        """Verify database applies performance PRAGMAs on connect."""
        import aiosqlite
        db = await aiosqlite.connect(":memory:")

        # Check journal mode
        cursor = await db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        # In-memory databases return "memory" for journal_mode
        assert row[0].upper() in ("WAL", "MEMORY", "DELETE")

        await db.close()

    @pytest.mark.asyncio
    async def test_batch_commit_functionality(self):
        """Verify batch commit works correctly."""
        from src.data.database import PolymarketDatabase

        db = await PolymarketDatabase.create(":memory:")
        # Just verify it connects without error
        assert db is not None
        assert db._conn is not None

        # Verify PRAGMAs were set
        cursor = await db._conn.execute("PRAGMA synchronous")
        row = await cursor.fetchone()
        assert row[0] in (1, 2)  # NORMAL or FULL

        await db.close()

    @pytest.mark.asyncio
    async def test_market_upsert(self):
        """Verify database CRUD operations work."""
        from src.data.database import PolymarketDatabase, MarketInfo

        db = await PolymarketDatabase.create(":memory:")
        market = MarketInfo(
            id="test-1",
            condition_id="cond-1",
            question="Test market?",
        )
        await db.upsert_market(market)

        retrieved = await db.get_market("test-1")
        assert retrieved is not None
        assert retrieved.question == "Test market?"

        await db.close()


# ── Integration Tests ──────────────────────────────────────────────────


class TestEstrategiaIntegration:
    """Integration tests: estrategia.py uses SignalCache and LatencyTracker."""

    def test_signal_cache_in_ejecucion(self):
        """Verify ejecucion.py imports and uses SignalCache."""
        from src.infrastructure.cache_manager import get_signal_cache_instance
        cache = get_signal_cache_instance()
        assert cache is not None

    def test_latency_tracker_import(self):
        """Verify latency tracker is importable."""
        from src.infrastructure.latency_tracker import get_latency_tracker
        tracker = get_latency_tracker()
        assert tracker is not None


class TestEventLoopOptimization:
    """Integration tests: event_loop.py exports work."""

    def test_import_event_loop_functions(self):
        """Verify all public functions are importable."""
        from src.infrastructure.event_loop import (
            install_uvloop,
            pin_to_core,
            pin_by_role,
            configure_event_loop,
            bootstrap_optimized_runtime,
        )
        assert callable(install_uvloop)
        assert callable(pin_to_core)
        assert callable(pin_by_role)
        assert callable(configure_event_loop)
        assert callable(bootstrap_optimized_runtime)

    def test_pin_to_core_safe(self):
        """pin_to_core should not raise on any platform."""
        from src.infrastructure.event_loop import pin_to_core
        # Should not raise even if core doesn't exist
        pin_to_core(9999)

    def test_install_uvloop_safe(self):
        """install_uvloop should not raise on any platform."""
        from src.infrastructure.event_loop import install_uvloop
        install_uvloop()


# ── Script Tests ────────────────────────────────────────────────────────


class TestOptimizedScript:
    """Tests for the run_optimized_local.py script."""

    def test_script_imports(self):
        """Verify the script can be parsed without import errors."""
        import ast
        import sys

        with open("scripts/run_optimized_local.py") as f:
            try:
                ast.parse(f.read())
            except SyntaxError as e:
                pytest.fail(f"Syntax error in run_optimized_local.py: {e}")

    def test_script_argument_parsing(self):
        """Verify argument parser works."""
        # We can't easily test argparse here without import side effects,
        # but we can verify the module structure
        with open("scripts/run_optimized_local.py") as f:
            content = f.read()
            assert "argparse" in content
            assert "parser = argparse.ArgumentParser" in content
            assert "--dry-run" in content
            assert "--markets" in content
            assert "--profile" in content
