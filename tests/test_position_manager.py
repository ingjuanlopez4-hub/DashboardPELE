"""
Tests del módulo Position Manager (src/live/position_manager.py).

Cubre:
- TrackedPosition: TP/SL levels, age tracking, price check
- PositionManager: open/close, update_price (TP/SL triggers),
  force-close por edad máxima, force_close_cb
- Escenarios de casos límite: TP/SL desactivados, precios extremos,
  múltiples posiciones, ciclos de edad
"""

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest

from src.live.position_manager import PositionManager, TrackedPosition


class TestTrackedPosition:
    """Pruebas de la posición trackeada individual."""

    def test_default_tp_sl_calculation(self):
        """TP y SL deben calcularse correctamente desde los porcentajes."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            take_profit_pct=Decimal("50.0"),
            stop_loss_pct=Decimal("30.0"),
        )
        # TP: 0.50 * (1 + 0.50) = 0.75
        assert pos.take_profit_price == Decimal("0.75")
        # SL: 0.50 * (1 - 0.30) = 0.35
        assert pos.stop_loss_price == Decimal("0.35")

    def test_tp_sl_for_short_position(self):
        """Para posiciones SELL, TP y SL deben invertirse."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="SELL_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            take_profit_pct=Decimal("50.0"),
            stop_loss_pct=Decimal("30.0"),
        )
        # Short TP: 0.50 * (1 - 0.50) = 0.25
        assert pos.take_profit_price == Decimal("0.25")
        # Short SL: 0.50 * (1 + 0.30) = 0.65
        assert pos.stop_loss_price == Decimal("0.65")

    def test_no_tp_sl_when_disabled(self):
        """Con TP/SL en 0, los niveles deben ser None."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            take_profit_pct=None,
            stop_loss_pct=None,
        )
        assert pos.take_profit_price is None
        assert pos.stop_loss_price is None

    def test_age_cycles_increases_over_time(self):
        """age_cycles debe incrementar con el tiempo."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time() - 120,  # 2 min
            cycle_duration_minutes=1,
        )
        assert pos.age_cycles >= 2

    def test_age_cycles_zero_when_recent(self):
        """age_cycles debe ser 0 para posiciones recién abiertas."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            cycle_duration_minutes=15,
        )
        assert pos.age_cycles == 0

    def test_check_price_take_profit(self):
        """Precio en o por encima del TP debe retornar 'take_profit'."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            take_profit_pct=Decimal("50.0"),
        )
        result = pos.check_price(Decimal("0.80"))
        assert result == "take_profit"

    def test_check_price_stop_loss(self):
        """Precio en o por debajo del SL debe retornar 'stop_loss'."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            stop_loss_pct=Decimal("30.0"),
        )
        result = pos.check_price(Decimal("0.30"))
        assert result == "stop_loss"

    def test_check_price_no_action(self):
        """Precio entre TP y SL debe retornar None."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            take_profit_pct=Decimal("50.0"),
            stop_loss_pct=Decimal("30.0"),
        )
        result = pos.check_price(Decimal("0.55"))
        assert result is None

    def test_check_price_short_take_profit(self):
        """Para shorts, precio en TP (por debajo) debe retornar 'take_profit'."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="SELL_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            take_profit_pct=Decimal("50.0"),
        )
        result = pos.check_price(Decimal("0.20"))
        assert result == "take_profit"

    def test_check_price_short_stop_loss(self):
        """Para shorts, precio en SL (por encima) debe retornar 'stop_loss'."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="SELL_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=time.time(),
            stop_loss_pct=Decimal("30.0"),
        )
        result = pos.check_price(Decimal("0.70"))
        assert result == "stop_loss"

    def test_to_dict_structure(self):
        """to_dict debe retornar la estructura esperada."""
        pos = TrackedPosition(
            asset_id="asset-1",
            side="BUY_YES",
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
            entry_time=1234567890,
            take_profit_pct=Decimal("50.0"),
            stop_loss_pct=Decimal("30.0"),
        )
        d = pos.to_dict()
        assert d["asset_id"] == "asset-1"
        assert d["side"] == "BUY_YES"
        assert d["entry_price"] == "0.50"
        assert d["size"] == "100"
        assert d["take_profit_price"] is not None
        assert d["stop_loss_price"] is not None
        assert "cycles_active" in d


class TestPositionManager:
    """Pruebas del gestor de posiciones."""

    @pytest.mark.asyncio
    async def test_open_and_close_position(self):
        """Abrir y cerrar una posición debe funcionar correctamente."""
        pm = PositionManager()
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))
        assert pm.get_position("asset-1") is not None
        assert len(pm.get_all_positions()) == 1

        pm.close_position("asset-1")
        assert pm.get_position("asset-1") is None
        assert len(pm.get_all_positions()) == 0

        await pm.stop()

    @pytest.mark.asyncio
    async def test_stop_loss_triggers_force_close(self):
        """SL debe emitir force_close y remover la posición."""
        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            stop_loss_pct=Decimal("20.0"),
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))

        result = await pm.update_price("asset-1", Decimal("0.35"))
        assert result == "stop_loss"
        assert len(force_closed) == 1
        assert force_closed[0]["force_close_reason"] == "stop_loss"
        assert pm.get_position("asset-1") is None

        await pm.stop()

    @pytest.mark.asyncio
    async def test_take_profit_triggers_force_close(self):
        """TP debe emitir force_close y remover la posición."""
        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            take_profit_pct=Decimal("50.0"),
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))

        result = await pm.update_price("asset-1", Decimal("0.80"))
        assert result == "take_profit"
        assert len(force_closed) == 1
        assert force_closed[0]["force_close_reason"] == "take_profit"

        await pm.stop()

    @pytest.mark.asyncio
    async def test_force_close_signal_structure(self):
        """La señal de force_close debe contener los campos esperados."""
        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            stop_loss_pct=Decimal("20.0"),
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))
        await pm.update_price("asset-1", Decimal("0.35"))

        signal = force_closed[0]
        assert signal["is_force_close"] is True
        assert signal["force_close_reason"] == "stop_loss"
        assert signal["asset_id"] == "asset-1"
        assert signal["side"] == "SELL_YES"  # long -> sell
        assert signal["priority"] == "high"

        await pm.stop()

    @pytest.mark.asyncio
    async def test_position_age_limit_force_closes(self):
        """Posiciones que exceden la edad máxima deben cerrarse forzosamente."""
        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            max_position_age_cycles=1,
            cycle_duration_minutes=1,
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))

        pos = pm.get_position("asset-1")
        object.__setattr__(pos, "entry_time", time.time() - 120)  # 2 min atrás

        closed = await pm.check_age_limit()
        assert "asset-1" in closed
        assert len(force_closed) == 1
        assert force_closed[0]["force_close_reason"] == "max_age_exceeded"

        await pm.stop()

    @pytest.mark.asyncio
    async def test_no_tp_sl_when_disabled(self):
        """Con TP/SL desactivados, los cambios de precio no deben triggerear."""
        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            take_profit_pct=Decimal("0"),
            stop_loss_pct=Decimal("0"),
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))

        result = await pm.update_price("asset-1", Decimal("0.99"))
        assert result is None
        result = await pm.update_price("asset-1", Decimal("0.01"))
        assert result is None
        assert len(force_closed) == 0

        await pm.stop()

    @pytest.mark.asyncio
    async def test_update_price_for_unowned_asset(self):
        """Actualizar precio de un activo sin posición no debe hacer nada."""
        pm = PositionManager()
        await pm.start()

        result = await pm.update_price("nonexistent", Decimal("0.50"))
        assert result is None

        await pm.stop()

    @pytest.mark.asyncio
    async def test_multiple_positions_independent(self):
        """Múltiples posiciones deben gestionarse independientemente."""
        force_closed = []

        async def force_close_cb(signal):
            force_closed.append(signal)

        pm = PositionManager(
            force_close_cb=force_close_cb,
            stop_loss_pct=Decimal("30.0"),
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))
        pm.open_position("asset-2", "BUY_NO", Decimal("0.30"), Decimal("200"))

        assert len(pm.get_all_positions()) == 2

        # Solo asset-1 debe triggerear SL
        await pm.update_price("asset-1", Decimal("0.30"))
        assert len(force_closed) == 1
        assert force_closed[0]["asset_id"] == "asset-1"

        # asset-2 debe seguir activa
        assert pm.get_position("asset-2") is not None

        await pm.stop()

    @pytest.mark.asyncio
    async def test_close_nonexistent_position(self):
        """Cerrar una posición inexistente debe retornar None."""
        pm = PositionManager()
        await pm.start()

        result = pm.close_position("nonexistent")
        assert result is None

        await pm.stop()

    @pytest.mark.asyncio
    async def test_get_stats_structure(self):
        """get_stats debe retornar la estructura esperada."""
        pm = PositionManager()
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))
        stats = pm.get_stats()

        assert stats["open_positions"] == 1
        assert len(stats["positions"]) == 1
        assert stats["positions"][0]["asset_id"] == "asset-1"
        assert stats["positions"][0]["side"] == "BUY_YES"

        await pm.stop()

    @pytest.mark.asyncio
    async def test_force_close_callback_error_handled(self):
        """Error en force_close_cb no debe romper el gestor."""
        async def failing_cb(signal):
            raise RuntimeError("callback failed")

        pm = PositionManager(
            force_close_cb=failing_cb,
            stop_loss_pct=Decimal("20.0"),
        )
        await pm.start()

        pm.open_position("asset-1", "BUY_YES", Decimal("0.50"), Decimal("100"))

        # No debe lanzar excepción
        result = await pm.update_price("asset-1", Decimal("0.35"))
        assert result == "stop_loss"
        # La posición debe eliminarse incluso si el callback falla
        assert pm.get_position("asset-1") is None

        await pm.stop()
