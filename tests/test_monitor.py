"""
Tests del módulo Monitor (src/live/monitor.py).

Cubre:
- CronMonitor: reconciliación de posiciones, verificación de balance,
  detección de discrepancias, callback de alerta crítica
- Health endpoint: build_health_response con estados OK/DEGRADED/BLOCKED
- Monitor loop: inicio/parada, logging de checks
- Casos límite: balance no disponible, fetch falla, discrepancia umbral
"""

import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock

from typing import Any

import pytest

from src.live.monitor import CronMonitor


class TestCronMonitorReconciliation:
    """Pruebas de la reconciliación de posiciones."""

    @pytest.mark.asyncio
    async def test_reconciliation_ok(self, tmp_path):
        """Reconciliación exitosa debe retornar status 'ok'."""
        async def fetch_orders():
            return [("ord-1", time.time()), ("ord-2", time.time())]

        monitor = CronMonitor(
            db_path=str(tmp_path / "test_rec.db"),
            fetch_open_orders_cb=fetch_orders,
        )
        await monitor.start()

        result = await monitor.reconcile_positions()
        assert result["status"] == "ok"
        assert result["remote_count"] == 2

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_reconciliation_skipped_no_callback(self, tmp_path):
        """Sin fetch callback, reconciliación debe retornar 'skipped'."""
        monitor = CronMonitor(db_path=str(tmp_path / "test_rec2.db"))
        await monitor.start()

        result = await monitor.reconcile_positions()
        assert result["status"] == "skipped"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_reconciliation_empty_orders(self, tmp_path):
        """Sin órdenes abiertas, reconciliación debe retornar ok."""
        async def fetch_orders():
            return []

        monitor = CronMonitor(
            db_path=str(tmp_path / "test_rec3.db"),
            fetch_open_orders_cb=fetch_orders,
        )
        await monitor.start()

        result = await monitor.reconcile_positions()
        assert result["status"] == "ok"
        assert result["remote_count"] == 0

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_reconciliation_fetch_error(self, tmp_path):
        """Error en fetch debe retornar status 'error'."""
        async def failing_fetch():
            raise RuntimeError("API failure")

        monitor = CronMonitor(
            db_path=str(tmp_path / "test_rec4.db"),
            fetch_open_orders_cb=failing_fetch,
        )
        await monitor.start()

        result = await monitor.reconcile_positions()
        assert result["status"] == "error"

        await monitor.stop()


class TestCronMonitorBalance:
    """Pruebas de la verificación de balance."""

    @pytest.mark.asyncio
    async def test_balance_ok_when_match(self, tmp_path):
        """Balance on-chain igual al esperado debe retornar status 'ok'."""
        monitor = CronMonitor(
            db_path=str(tmp_path / "test_bal.db"),
            balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
            expected_balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
            balance_discrepancy_threshold=Decimal("1.0"),
        )
        await monitor.start()

        result = await monitor.check_balance()
        assert result["status"] == "ok"
        assert result["on_chain"] == "1000"
        assert result["expected"] == "1000"
        assert result["discrepancy"] == "0"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_balance_discrepancy_triggers_alert(self, tmp_path):
        """Discrepancia grande debe disparar alerta crítica."""
        alerted = []

        async def critical_cb(msg):
            alerted.append(msg)

        monitor = CronMonitor(
            db_path=str(tmp_path / "test_bal2.db"),
            balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
            expected_balance_provider=lambda: asyncio.sleep(0, Decimal("800")),
            balance_discrepancy_threshold=Decimal("1.0"),
            on_critical_cb=critical_cb,
        )
        await monitor.start()

        result = await monitor.check_balance()
        assert result["status"] == "discrepancy"
        assert len(alerted) == 1
        assert "BALANCE DISCREPANCY" in alerted[0]

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_balance_discrepancy_within_threshold(self, tmp_path):
        """Discrepancia menor al umbral no debe disparar alerta."""
        alerted = []

        async def critical_cb(msg):
            alerted.append(msg)

        monitor = CronMonitor(
            db_path=str(tmp_path / "test_bal3.db"),
            balance_provider=lambda: asyncio.sleep(0, Decimal("1000.50")),
            expected_balance_provider=lambda: asyncio.sleep(0, Decimal("1000.00")),
            balance_discrepancy_threshold=Decimal("1.0"),
            on_critical_cb=critical_cb,
        )
        await monitor.start()

        result = await monitor.check_balance()
        # 1000.50 - 1000.00 = 0.50 < 1.0, no debe alertar
        assert result["status"] == "ok"
        assert len(alerted) == 0

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_balance_provider_error(self, tmp_path):
        """Error en balance_provider no debe romper el check."""
        monitor = CronMonitor(
            db_path=str(tmp_path / "test_bal4.db"),
            balance_provider=AsyncMock(side_effect=RuntimeError("fail")),
            expected_balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
        )
        await monitor.start()

        result = await monitor.check_balance()
        assert "status" in result

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_expected_balance_provider_error(self, tmp_path):
        """Error en expected_provider no debe romper el check."""
        monitor = CronMonitor(
            db_path=str(tmp_path / "test_bal5.db"),
            balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
            expected_balance_provider=AsyncMock(side_effect=RuntimeError("fail")),
        )
        await monitor.start()

        result = await monitor.check_balance()
        assert "status" in result

        await monitor.stop()


class TestCronMonitorHealthEndpoint:
    """Pruebas del endpoint de health."""

    def test_health_response_ok(self):
        """Estado saludable debe retornar status OK."""
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
            ws_health={"connected": True, "book_synced": True, "syncing": False},
            order_guard_paused=False,
            position_stats={"open_positions": 2},
            performance_stats={"mae": "0.05", "adjusted_min_edge": "0.05"},
            extra={"dry_run": False, "uptime_seconds": 3600, "last_error": ""},
        )

        assert health["status"] == "OK"
        assert health["circuit_breakers"]["blocked"] is False
        assert health["websocket"]["connected"] is True
        assert health["order_guard"]["trading_paused"] is False
        assert health["positions"]["open_positions"] == 2

    def test_health_response_blocked(self):
        """Circuit breaker bloqueado debe retornar status BLOCKED."""
        monitor = CronMonitor()

        health = monitor.build_health_response(
            circuit_breaker_snapshot={
                "status": "BLOCKED",
                "blocked": True,
                "block_reason": "drawdown_exceeded",
                "daily_loss_pct": "15.00",
                "total_drawdown_blocked": False,
                "total_drawdown_peak_balance": "1000",
                "cooldown_active": False,
                "cooldown_remaining_s": 0,
                "failure_count": 0,
                "total_orders_placed": 5,
                "total_orders_filled": 2,
                "daily_start_balance": "1000",
                "total_pnl": "-150",
            },
            ws_health={"connected": True, "book_synced": True, "syncing": False},
            order_guard_paused=False,
            position_stats={"open_positions": 0},
            performance_stats={},
            extra={"dry_run": False, "uptime_seconds": 1000, "last_error": "drawdown"},
        )

        assert health["status"] == "BLOCKED"

    def test_health_response_degraded_ws(self):
        """WebSocket desconectado debe retornar DEGRADED."""
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
                "total_orders_placed": 0,
                "total_orders_filled": 0,
                "daily_start_balance": "1000",
                "total_pnl": "0",
            },
            ws_health={"connected": False, "book_synced": False, "syncing": True},
            order_guard_paused=False,
            position_stats={"open_positions": 0},
            performance_stats={},
            extra={},
        )

        assert health["status"] == "DEGRADED"

    def test_health_response_degraded_order_guard(self):
        """OrderGuard pausado debe retornar DEGRADED."""
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
                "total_orders_placed": 0,
                "total_orders_filled": 0,
                "daily_start_balance": "1000",
                "total_pnl": "0",
            },
            ws_health={"connected": True, "book_synced": True, "syncing": False},
            order_guard_paused=True,
            position_stats={"open_positions": 0},
            performance_stats={},
            extra={},
        )

        assert health["status"] == "DEGRADED"

    def test_health_response_total_drawdown_blocked(self):
        """Total drawdown permanente debe retornar BLOCKED."""
        monitor = CronMonitor()

        health = monitor.build_health_response(
            circuit_breaker_snapshot={
                "status": "HEALTHY",
                "blocked": False,
                "block_reason": "none",
                "daily_loss_pct": "0.00",
                "total_drawdown_blocked": True,
                "total_drawdown_peak_balance": "1000",
                "cooldown_active": False,
                "cooldown_remaining_s": 0,
                "failure_count": 0,
                "total_orders_placed": 0,
                "total_orders_filled": 0,
                "daily_start_balance": "1000",
                "total_pnl": "-300",
            },
            ws_health={"connected": True, "book_synced": True, "syncing": False},
            order_guard_paused=False,
            position_stats={"open_positions": 0},
            performance_stats={},
            extra={},
        )

        assert health["status"] == "BLOCKED"

    def test_health_response_contains_all_keys(self):
        """La respuesta de health debe contener todas las secciones esperadas."""
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
                "total_orders_placed": 0,
                "total_orders_filled": 0,
                "daily_start_balance": "1000",
                "total_pnl": "0",
            },
            ws_health={"connected": True, "book_synced": True, "syncing": False},
            order_guard_paused=False,
            position_stats={"open_positions": 0},
            performance_stats={},
            extra={"dry_run": True, "uptime_seconds": 100, "last_error": ""},
        )

        expected_sections = {"status", "timestamp", "circuit_breakers", "websocket",
                             "order_guard", "positions", "performance", "monitor"}
        assert expected_sections.issubset(health.keys())


class TestCronMonitorLifecycle:
    """Pruebas del ciclo de vida del monitor."""

    @pytest.mark.asyncio
    async def test_start_stop_cycle(self, tmp_path):
        """Iniciar y detener el monitor debe funcionar sin errores."""
        monitor = CronMonitor(
            db_path=str(tmp_path / "test_lifecycle.db"),
            monitor_interval_s=3600,  # no se ejecute durante el test
        )
        await monitor.start()
        assert monitor._running is True
        await monitor.stop()
        assert monitor._running is False

    @pytest.mark.asyncio
    async def test_get_health_dict_never_run(self, tmp_path):
        """get_health_dict debe funcionar incluso sin ejecutar checks."""
        monitor = CronMonitor(db_path=str(tmp_path / "test_health.db"))
        await monitor.start()

        health = monitor.get_health_dict()
        assert "monitor" in health
        assert "last_reconciliation" in health["monitor"]
        assert "last_balance_check" in health["monitor"]
        assert "last_balance_usdc" in health["monitor"]

        await monitor.stop()


class TestCronMonitorEdgeCases:
    """Pruebas de casos límite del monitor."""

    @pytest.mark.asyncio
    async def test_log_check_sqlite(self, tmp_path):
        """log_check debe persistir en SQLite sin errores."""
        monitor = CronMonitor(db_path=str(tmp_path / "test_log.db"))
        await monitor.start()

        await monitor._log_check("test_check", "ok", "test detail")
        # Verificar que se insertó
        db = monitor._db
        cursor = await db.execute("SELECT COUNT(*) FROM monitor_log")
        row = await cursor.fetchone()
        assert row[0] >= 1

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_reconciliation_issues_tracked(self, tmp_path):
        """Los issues de reconciliación deben acumularse."""
        async def failing_fetch():
            raise RuntimeError("network error")

        monitor = CronMonitor(
            db_path=str(tmp_path / "test_issues.db"),
            fetch_open_orders_cb=failing_fetch,
        )
        await monitor.start()

        await monitor.reconcile_positions()
        assert len(monitor._reconciliation_issues) >= 1

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_balance_without_providers(self, tmp_path):
        """Sin providers, balance check debe retornar ceros."""
        monitor = CronMonitor(db_path=str(tmp_path / "test_noprov.db"))
        await monitor.start()

        result = await monitor.check_balance()
        assert result["on_chain"] == "0"
        assert result["expected"] == "0"
        assert result["discrepancy"] == "0"

        await monitor.stop()
