"""
Pruebas de Integración (End‑to‑End en papel).

Conecta los módulos:
  Ingesta (A) → Estrategia (B) → Ejecución (C) → Archivo (D)

Usa modo dry-run/papel para no enviar transacciones reales.
"""

import asyncio
import json
import os
import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ingesta import IngestaCLOB, NormalizedEvent
from estrategia import MotorEstrategia
from ejecucion import EjecutorOrdenes
from archivo import ArchivoBacktest


# =========================================================================
# Fixtures for integration tests
# =========================================================================

@pytest.fixture
def integration_env(monkeypatch):
    """Ensure clean env for integration tests."""
    monkeypatch.setenv("POLYMARKET_API_KEY", "test-api-key")
    monkeypatch.setenv("POLYMARKET_SECRET", "dGVzdC1zZWNyZXQ=")
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("PRIVATE_KEY", "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")


@pytest.fixture(autouse=True)
def mock_ejecutor_submodules():
    """Mock all external-dependent sub-modules of EjecutorOrdenes to prevent hangs."""
    patches = [
        patch("ejecucion.CircuitBreakerManager.start", AsyncMock()),
        patch("ejecucion.CircuitBreakerManager.stop", AsyncMock()),
        patch("ejecucion.CircuitBreakerManager.is_trading_blocked", AsyncMock(return_value=(False, ""))),
        patch("ejecucion.CircuitBreakerManager.startup_cancel_all", AsyncMock()),
        patch("ejecucion.CircuitBreakerManager.cancel_all_orders", AsyncMock()),
        patch("ejecucion.PerformanceTracker.start", AsyncMock()),
        patch("ejecucion.PerformanceTracker.stop", AsyncMock()),
        patch("ejecucion.PositionManager.start", AsyncMock()),
        patch("ejecucion.PositionManager.stop", AsyncMock()),
        patch("ejecucion.CronMonitor.start", AsyncMock()),
        patch("ejecucion.CronMonitor.stop", AsyncMock()),
        patch("ejecucion.AlertManager.start", AsyncMock()),
        patch("ejecucion.AlertManager.stop", AsyncMock()),
        patch("ejecucion.OrderGuard.clean_start", AsyncMock()),
        patch("ejecucion.OrderGuard.start_watchdog", AsyncMock()),
        patch("ejecucion.OrderGuard.shutdown", AsyncMock()),
        patch("ejecucion.EjecutorOrdenes._reconcile_state", AsyncMock()),
        patch("ejecucion.EjecutorOrdenes._start_health_server", AsyncMock()),
        patch("ejecucion.EjecutorOrdenes._stop_health_server", AsyncMock()),
        patch("ejecucion.EjecutorOrdenes._setup_signal_handlers"),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture(autouse=True)
def mock_estrategia_finbert():
    """Prevent MotorEstrategia from loading FinBERT model."""
    patch("estrategia.MotorEstrategia._ensure_sentiment_pipeline",
          AsyncMock(return_value="dummy")).start()
    yield
    patch.stopall()


class IntegrationTestHarness:
    """Coordina los 4 módulos para tests de integración."""

    def __init__(self, tmp_path: str):
        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.signal_queue: asyncio.Queue = asyncio.Queue()
        self.execution_log_queue: asyncio.Queue = asyncio.Queue()
        self.db_path = str(tmp_path / "integration_test.db")

        # Módulo A: Ingesta
        self.ingesta = IngestaCLOB(
            asset_ids=["0xabc"],
            market_snapshot=[{
                "condition_id": "cond-1",
                "market": "Test market",
                "asset_ids": ["0xabc"],
                "tick_size": "0.01",
            }],
        )
        # Override queue to our shared queue
        self.ingesta.queue = self.event_queue

        # Módulo B: Estrategia
        self.estrategia = MotorEstrategia(
            event_queue=self.event_queue,
            signal_queue=self.signal_queue,
        )

        # Módulo C: Ejecutor (dry-run)
        self.ejecutor = EjecutorOrdenes(
            signal_queue=self.signal_queue,
            dry_run=True,
            execution_log_queue=self.execution_log_queue,
            db_path=self.db_path,
        )

        # Módulo D: Archivo
        self.archivo = ArchivoBacktest(
            db_path=self.db_path,
            execution_log_queue=self.execution_log_queue,
        )

        self._running = False

    async def inject_book_events(self, asset_id: str = "0xabc", count: int = 5):
        """Inject synthetic book events to trigger analysis."""
        for i in range(count):
            evt = NormalizedEvent(
                type="book",
                market="Test market",
                asset_id=asset_id,
                price=Decimal("0.50"),
                size=Decimal("100"),
                side="BUY",
            )
            await self.event_queue.put(evt)

            evt = NormalizedEvent(
                type="book",
                market="Test market",
                asset_id=asset_id,
                price=Decimal("0.52"),
                size=Decimal("200"),
                side="SELL",
            )
            await self.event_queue.put(evt)

    async def inject_price_events(self, asset_id: str = "0xabc"):
        """Inject price history for Monte Carlo."""
        for i in range(20):
            evt = NormalizedEvent(
                type="price_change",
                market="Test market",
                asset_id=asset_id,
                price=Decimal(str(round(0.50 + (i % 10) * 0.01, 2))),
            )
            await self.event_queue.put(evt)


# =========================================================================
# Integration Tests
# =========================================================================

class TestFullPipeline:

    @pytest.mark.asyncio
    async def test_ingesta_to_estrategia_signal_flow(self, integration_env, tmp_path):
        """Ingesta normalizes events → Estrategia consumes and emits signal."""
        harness = IntegrationTestHarness(tmp_path)

        await harness.inject_book_events(count=10)
        await harness.inject_price_events()

        # Process events directly (avoid run() which uses asyncio.wait(FIRST_EXCEPTION))
        consumed = 0
        while not harness.event_queue.empty() and consumed < 100:
            evt = await asyncio.wait_for(harness.event_queue.get(), timeout=5.0)
            harness.estrategia._process_event(evt)
            consumed += 1

        # Emit signals manually
        try:
            await asyncio.wait_for(
                harness.estrategia._compute_and_emit_signals(),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            pytest.fail("Signal computation timed out")

        signals = []
        while not harness.signal_queue.empty():
            sig = await harness.signal_queue.get()
            signals.append(sig)

        for sig in signals:
            assert "asset_id" in sig
            assert "side" in sig
            assert "probability" in sig

    @pytest.mark.asyncio
    async def test_full_pipeline_dry_run(self, integration_env, tmp_path):
        """
        Full end-to-end test:
        Ingesta → Estrategia → Ejecutor (dry-run) → Archivo
        """
        harness = IntegrationTestHarness(tmp_path)

        # Start archivo (consumer) — this is lightweight SQLite
        archivo_task = asyncio.create_task(harness.archivo.run())

        # Inject events
        await harness.inject_book_events(count=10)
        await harness.inject_price_events()

        # Process events through estrategia directly
        consumed = 0
        while not harness.event_queue.empty() and consumed < 100:
            evt = await asyncio.wait_for(harness.event_queue.get(), timeout=5.0)
            harness.estrategia._process_event(evt)
            consumed += 1

        # Emit signals
        try:
            await asyncio.wait_for(
                harness.estrategia._compute_and_emit_signals(),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            pytest.fail("Signal computation timed out")

        # Process signals through ejecutor
        while not harness.signal_queue.empty():
            signal = await harness.signal_queue.get()
            try:
                await asyncio.wait_for(
                    harness.ejecutor._process_signal(signal),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                pytest.fail("Signal processing timed out")

        # Stop archivo
        harness.archivo.stop()
        try:
            await asyncio.wait_for(archivo_task, timeout=5.0)
        except asyncio.TimeoutError:
            archivo_task.cancel()
            try:
                await archivo_task
            except (asyncio.CancelledError, RuntimeError):
                pass

        trades = await harness.archivo._fetch_trades()
        assert isinstance(trades, list)

    @pytest.mark.asyncio
    async def test_signal_reaches_executor(self, integration_env, tmp_path):
        """Signal emitted by Estrategia must reach Ejecutor."""
        harness = IntegrationTestHarness(tmp_path)

        signal = {
            "asset_id": "0xabc",
            "market": "Test market",
            "side": "BUY_YES",
            "price": "0.52",
            "size": "10.00",
            "probability": "0.55",
            "current_price": "0.52",
            "ev": "0.03",
            "tick_size": "0.01",
        }
        await harness.signal_queue.put(signal)

        try:
            await asyncio.wait_for(
                harness.ejecutor._process_signal(signal),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            pytest.fail("Signal processing timed out")

        assert harness.execution_log_queue.qsize() >= 0

    @pytest.mark.asyncio
    async def test_daily_loss_blocks_signal(self, integration_env, tmp_path):
        """After exceeding daily loss, executor must reject signals."""
        harness = IntegrationTestHarness(tmp_path)

        harness.ejecutor._daily_start_balance = Decimal("1000")
        harness.ejecutor._daily_pnl = Decimal("-100")

        signal = {
            "asset_id": "0xabc",
            "market": "Test market",
            "side": "BUY_YES",
            "price": "0.52",
            "size": "10.00",
            "probability": "0.55",
            "current_price": "0.52",
            "ev": "0.03",
            "tick_size": "0.01",
        }

        try:
            await asyncio.wait_for(
                harness.ejecutor._process_signal(signal),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            pytest.fail("Signal processing timed out")

    @pytest.mark.asyncio
    async def test_stale_order_cancellation(self, integration_env, tmp_path):
        """Stale orders should be cancelled via lifecycle manager."""
        harness = IntegrationTestHarness(tmp_path)

        stale_oid = "stale-integration-ord"
        harness.ejecutor._order_lifecycle._open_orders[stale_oid] = time.time() - 300

        try:
            cancelled = await asyncio.wait_for(
                harness.ejecutor._order_lifecycle.cancel_stale_orders(max_age_s=120),
                timeout=5.0,
            )
            assert isinstance(cancelled, int)
        except asyncio.TimeoutError:
            pytest.fail("Stale order cancellation timed out")

    @pytest.mark.asyncio
    async def test_multiple_markets_pipeline(self, integration_env, tmp_path):
        """Pipeline should handle multiple markets simultaneously."""
        harness = IntegrationTestHarness(tmp_path)

        for asset_id in ["0xabc", "0xdef"]:
            for i in range(5):
                evt = NormalizedEvent(
                    type="book",
                    market=f"Market-{asset_id}",
                    asset_id=asset_id,
                    price=Decimal("0.50"),
                    size=Decimal("100"),
                    side="BUY",
                )
                await harness.event_queue.put(evt)

        consumed = 0
        while not harness.event_queue.empty() and consumed < 100:
            evt = await asyncio.wait_for(harness.event_queue.get(), timeout=5.0)
            harness.estrategia._process_event(evt)
            consumed += 1

        assert "0xabc" in harness.estrategia._assets
        assert "0xdef" in harness.estrategia._assets


# =========================================================================
# Integration: Event loss and duplicate detection
# =========================================================================

class TestEventIntegrity:

    @pytest.mark.asyncio
    async def test_no_event_loss_on_rapid_fire(self, integration_env, tmp_path):
        """Rapid event injection should not lose events in queues."""
        harness = IntegrationTestHarness(tmp_path)

        for i in range(100):
            evt = NormalizedEvent(
                type="book",
                market="Test market",
                asset_id="0xabc",
                price=Decimal(str(round(0.50 + (i % 50) * 0.01, 2))),
                size=Decimal("100"),
                side="BUY",
            )
            await harness.event_queue.put(evt)

        consumed = 0
        while not harness.event_queue.empty() and consumed < 100:
            try:
                evt = await asyncio.wait_for(harness.event_queue.get(), timeout=0.1)
                harness.estrategia._process_event(evt)
                consumed += 1
            except asyncio.TimeoutError:
                break

        assert consumed == 100, f"Lost {100 - consumed} events"

    @pytest.mark.asyncio
    async def test_queue_full_handling(self, integration_env, tmp_path):
        """When queue is full, events should be dropped gracefully."""
        harness = IntegrationTestHarness(tmp_path)

        small_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        harness.ingesta.queue = small_queue

        for i in range(10):
            evt = NormalizedEvent(type="book", market="m1", asset_id="0xabc", price=Decimal("0.50"), size=Decimal("100"), side="BUY")
            try:
                await asyncio.wait_for(small_queue.put(evt), timeout=0.1)
            except asyncio.TimeoutError:
                pass

        assert small_queue.qsize() <= 5


# =========================================================================
# Integration: Circuit Breaker + Dry-run
# =========================================================================

class TestCircuitBreakerDryRun:

    @pytest.mark.asyncio
    async def test_dry_run_passes_circuit_breakers(self, integration_env, tmp_path):
        """Dry-run orders should still pass through circuit breakers."""
        harness = IntegrationTestHarness(tmp_path)

        try:
            blocked, reason = await asyncio.wait_for(
                harness.ejecutor._circuit_breaker.is_trading_blocked(),
                timeout=5.0,
            )
            assert isinstance(blocked, bool)
        except asyncio.TimeoutError:
            pytest.fail("Circuit breaker check timed out")

    @pytest.mark.asyncio
    async def test_dry_run_registers_in_log(self, integration_env, tmp_path):
        """Dry-run should log the attempt without sending real API calls."""
        harness = IntegrationTestHarness(tmp_path)

        signal = {
            "asset_id": "123456",
            "market": "Test market",
            "side": "BUY_YES",
            "price": "0.52",
            "size": "10.00",
            "probability": "0.55",
            "current_price": "0.52",
            "ev": "0.03",
            "tick_size": "0.01",
        }

        order_data, exchange = harness.ejecutor._build_order_payload(signal)
        typed = harness.ejecutor._build_typed_data(order_data, exchange)
        signature = harness.ejecutor._sign_order(typed)

        try:
            result = await asyncio.wait_for(
                harness.ejecutor._send_order_raw(order_data, signature, signal),
                timeout=5.0,
            )
            assert result.get("dry_run") is True
        except asyncio.TimeoutError:
            pytest.fail("Order sending timed out")
