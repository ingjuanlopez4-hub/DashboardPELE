"""
Tests del módulo Market Filter (src/live/market_filter.py).

Cubre:
- MarketQualifier: filtro de probabilidad, volumen, tiempo hasta resolución,
  ventana de oportunidad, edge dinámico
- check_signal: método unificado con todas las comprobaciones
- Escenarios de integración con PerformanceTracker
- Calibración dinámica: min_edge se ajusta según MAE
"""

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest

from src.live.market_filter import MarketQualifier
from src.live.performance_tracker import PerformanceTracker


class TestMarketQualifierProbability:
    """Pruebas del filtro de rango de probabilidad."""

    def setup_method(self):
        self.q = MarketQualifier(
            min_prob=Decimal("0.30"),
            max_prob=Decimal("0.70"),
        )

    def test_probability_below_min(self):
        """Probabilidad por debajo del mínimo debe ser rechazada."""
        result = self.q.check_probability_range(Decimal("0.10"))
        assert result is not None
        assert "probability_below_min" in result

    def test_probability_above_max(self):
        """Probabilidad por encima del máximo debe ser rechazada."""
        result = self.q.check_probability_range(Decimal("0.90"))
        assert result is not None
        assert "probability_above_max" in result

    def test_probability_at_min_boundary(self):
        """Probabilidad exactamente en el límite inferior debe ser aceptada."""
        result = self.q.check_probability_range(Decimal("0.30"))
        assert result is None

    def test_probability_at_max_boundary(self):
        """Probabilidad exactamente en el límite superior debe ser aceptada."""
        result = self.q.check_probability_range(Decimal("0.70"))
        assert result is None

    def test_probability_mid_range(self):
        """Probabilidad en el rango medio debe ser aceptada."""
        result = self.q.check_probability_range(Decimal("0.50"))
        assert result is None


class TestMarketQualifierOpportunityWindow:
    """Pruebas del filtro de ventana de oportunidad."""

    def setup_method(self):
        self.q = MarketQualifier(opportunity_windows={
            "15m": {"window_before_end_s": 90},
            "5m": {"window_before_end_s": 30},
            "default": {"window_before_end_s": 60},
        })

    def test_outside_window_too_early(self):
        """Mercado con mucho tiempo restante debe estar fuera de la ventana."""
        now = time.time()
        end_time = now + 600  # 10 minutos > 90s
        result = self.q.check_opportunity_window(end_time, "15m", now)
        assert result is not None
        assert "outside_opportunity_window" in result

    def test_inside_window(self):
        """Mercado con poco tiempo restante debe estar dentro de la ventana."""
        now = time.time()
        end_time = now + 30  # 30s < 90s
        result = self.q.check_opportunity_window(end_time, "15m", now)
        assert result is None

    def test_market_already_ended(self):
        """Mercado ya finalizado debe ser rechazado."""
        now = time.time()
        end_time = now - 100  # ya terminó
        result = self.q.check_opportunity_window(end_time, "15m", now)
        assert result is not None
        assert "market_already_ended" in result

    def test_no_end_time_skips_check(self):
        """Ausencia de end_time debe saltar el filtro."""
        result = self.q.check_opportunity_window(None, "15m")
        assert result is None

    def test_negative_end_time_skips_check(self):
        """End_time <= 0 debe saltar el filtro."""
        result = self.q.check_opportunity_window(0, "15m")
        assert result is None

    def test_different_window_configs(self):
        """Distintos tipos de mercado deben usar su ventana configurada."""
        now = time.time()
        # 5m: ventana 30s -> 40s fuera
        end_time = now + 40
        result = self.q.check_opportunity_window(end_time, "5m", now)
        assert result is not None

        # 5m: ventana 30s -> 20s dentro
        end_time = now + 20
        result = self.q.check_opportunity_window(end_time, "5m", now)
        assert result is None


class TestMarketQualifierVolume:
    """Pruebas del filtro de volumen."""

    def setup_method(self):
        self.q = MarketQualifier(min_volume_24h=Decimal("5000"))

    def test_volume_below_min(self):
        """Volumen por debajo del mínimo debe ser rechazado."""
        result = self.q.check_volume(Decimal("100"))
        assert result is not None
        assert "volume_below_min" in result

    def test_volume_above_min(self):
        """Volumen por encima del mínimo debe ser aceptado."""
        result = self.q.check_volume(Decimal("10000"))
        assert result is None

    def test_volume_at_min_boundary(self):
        """Volumen exactamente en el mínimo debe ser aceptado."""
        result = self.q.check_volume(Decimal("5000"))
        assert result is None

    def test_no_volume_data_skips_check(self):
        """Ausencia de datos de volumen debe saltar el filtro."""
        result = self.q.check_volume(None)
        assert result is None


class TestMarketQualifierTimeToResolution:
    """Pruebas del filtro de tiempo hasta resolución."""

    def setup_method(self):
        self.q = MarketQualifier(min_hours_to_resolution=336)

    def test_too_close_to_resolution(self):
        """Mercado con menos de 336h restantes debe ser rechazado."""
        now = time.time()
        end_time = now + 86400  # 24h
        result = self.q.check_time_to_resolution(end_time, now)
        assert result is not None
        assert "too_close_to_resolution" in result

    def test_far_enough_from_resolution(self):
        """Mercado con más de 336h restantes debe ser aceptado."""
        now = time.time()
        end_time = now + 1800000  # 500h
        result = self.q.check_time_to_resolution(end_time, now)
        assert result is None

    def test_no_end_time_skips_check(self):
        """Ausencia de end_time debe saltar el filtro."""
        result = self.q.check_time_to_resolution(None)
        assert result is None


class TestMarketQualifierDynamicEdge:
    """Pruebas de la calibración dinámica del min_edge."""

    def test_static_edge_default(self):
        """Sin provider dinámico, debe usarse el edge estático."""
        q = MarketQualifier()
        effective = q.get_min_edge(Decimal("0.05"))
        assert effective == Decimal("0.05")

    def test_dynamic_edge_overrides_static(self):
        """El provider dinámico debe sobreescribir el edge estático si es mayor."""
        calls = []

        def provider():
            calls.append(1)
            return Decimal("0.08")

        q = MarketQualifier(dynamic_min_edge_provider=provider)
        effective = q.get_min_edge(Decimal("0.05"))
        assert effective == Decimal("0.08")
        assert len(calls) == 1

    def test_dynamic_edge_lower_than_static(self):
        """Si el edge dinámico es menor, debe usarse el estático."""
        def provider():
            return Decimal("0.03")

        q = MarketQualifier(dynamic_min_edge_provider=provider)
        effective = q.get_min_edge(Decimal("0.05"))
        assert effective == Decimal("0.05")

    def test_dynamic_edge_provider_error(self):
        """Error en el provider no debe romper el flujo."""
        def provider():
            raise RuntimeError("provider error")

        q = MarketQualifier(dynamic_min_edge_provider=provider)
        effective = q.get_min_edge(Decimal("0.05"))
        assert effective == Decimal("0.05")


class TestMarketQualifierCheckSignal:
    """Pruebas del método unificado check_signal."""

    def setup_method(self):
        self.q = MarketQualifier(
            min_prob=Decimal("0.30"),
            max_prob=Decimal("0.70"),
            min_volume_24h=Decimal("5000"),
            min_hours_to_resolution=336,
            opportunity_windows={
                "15m": {"window_before_end_s": 90},
                "default": {"window_before_end_s": 60},
            },
        )

    def test_good_signal_passes_all_filters(self):
        """Señal válida debe pasar todos los filtros."""
        now = time.time()
        signal = {
            "probability": "0.50",
            "ev": "0.10",
            "asset_id": "good-asset",
            "market_type": "15m",
        }
        meta = {
            "volume_24h": Decimal("10000"),
            "end_time_s": now + 1800000,       # resolución > 336h
            "candle_end_time_s": now + 30,      # dentro de ventana 90s
        }
        result = self.q.check_signal(signal, meta, Decimal("0.05"))
        assert result is None

    def test_signal_low_probability_rejected(self):
        """Señal con probabilidad baja debe ser rechazada."""
        signal = {"probability": "0.10", "ev": "0.10"}
        result = self.q.check_signal(signal, static_min_edge=Decimal("0.05"))
        assert result is not None
        assert "probability_below_min" in result

    def test_signal_high_probability_rejected(self):
        """Señal con probabilidad alta debe ser rechazada."""
        signal = {"probability": "0.90", "ev": "0.10"}
        result = self.q.check_signal(signal, static_min_edge=Decimal("0.05"))
        assert result is not None
        assert "probability_above_max" in result

    def test_signal_low_edge_rejected(self):
        """Señal con edge bajo debe ser rechazada."""
        signal = {"probability": "0.50", "ev": "0.01"}
        result = self.q.check_signal(signal, static_min_edge=Decimal("0.05"))
        assert result is not None
        assert "edge_below_min" in result

    def test_no_market_meta_skips_volume_and_window(self):
        """Sin market_meta, los filtros de volumen/ventana deben saltarse."""
        signal = {"probability": "0.50", "ev": "0.10"}
        result = self.q.check_signal(signal, market_meta=None, static_min_edge=Decimal("0.05"))
        assert result is None

    def test_signal_rejected_low_volume(self):
        """Volumen bajo debe rechazar la señal."""
        now = time.time()
        signal = {"probability": "0.50", "ev": "0.10"}
        meta = {"volume_24h": Decimal("100")}
        result = self.q.check_signal(signal, meta, Decimal("0.05"))
        assert result is not None
        assert "volume_below_min" in result

    def test_signal_rejected_too_close_to_resolution(self):
        """Resolución demasiado cercana debe rechazar la señal."""
        now = time.time()
        signal = {"probability": "0.50", "ev": "0.10"}
        meta = {"volume_24h": Decimal("10000"), "end_time_s": now + 3600}
        result = self.q.check_signal(signal, meta, Decimal("0.05"))
        assert result is not None
        assert "too_close_to_resolution" in result

    def test_signal_rejected_outside_window(self):
        """Fuera de la ventana de oportunidad debe rechazar la señal."""
        now = time.time()
        signal = {"probability": "0.50", "ev": "0.10", "market_type": "15m"}
        meta = {
            "volume_24h": Decimal("10000"),
            "end_time_s": now + 1800000,
            "candle_end_time_s": now + 200,  # 200s > 90s window
        }
        result = self.q.check_signal(signal, meta, Decimal("0.05"))
        assert result is not None
        assert "outside_opportunity_window" in result


class TestPerformanceTrackerCalibration:
    """Pruebas de integración: PerformanceTracker calibra el min_edge."""

    @pytest.mark.asyncio
    async def test_min_edge_increases_with_mae(self, tmp_path):
        """MAE alto debe incrementar el min_edge automáticamente."""
        tracker = PerformanceTracker(
            db_path=str(tmp_path / "test_calib.db"),
            base_min_edge=Decimal("0.05"),
            mae_adjustment_factor=Decimal("1.5"),
            max_min_edge=Decimal("0.15"),
        )
        await tracker.start()

        # Sin predicciones: MAE = 0, adjusted_min_edge = base
        assert tracker.adjusted_min_edge == Decimal("0.05")

        # Añadir predicciones con error alto
        await tracker.record_prediction("a", Decimal("0.90"), False)  # error = 0.90
        await tracker.record_prediction("b", Decimal("0.10"), True)   # error = 0.90

        # MAE = 0.90, adjustment = 0.90 * 1.5 = 1.35, capped at 0.15
        assert tracker.adjusted_min_edge == Decimal("0.15")

        await tracker.stop()

    @pytest.mark.asyncio
    async def test_mae_calculation_accuracy(self, tmp_path):
        """El MAE debe calcularse correctamente con valores conocidos."""
        tracker = PerformanceTracker(
            db_path=str(tmp_path / "test_mae.db"),
            window_size=10,
        )
        await tracker.start()

        await tracker.record_prediction("a", Decimal("0.60"), True)   # error = 0.40
        await tracker.record_prediction("b", Decimal("0.40"), False)  # error = 0.40
        await tracker.record_prediction("c", Decimal("0.80"), True)   # error = 0.20

        expected_mae = (Decimal("0.40") + Decimal("0.40") + Decimal("0.20")) / Decimal("3")
        assert tracker.prediction_count == 3
        assert tracker.mae == expected_mae

        await tracker.stop()

    @pytest.mark.asyncio
    async def test_min_edge_capped_at_max(self, tmp_path):
        """El min_edge ajustado no debe exceder max_min_edge."""
        tracker = PerformanceTracker(
            db_path=str(tmp_path / "test_cap2.db"),
            base_min_edge=Decimal("0.05"),
            mae_adjustment_factor=Decimal("5.0"),
            max_min_edge=Decimal("0.15"),
        )
        await tracker.start()

        await tracker.record_prediction("a", Decimal("0.90"), False)  # error = 0.90
        await tracker.record_prediction("b", Decimal("0.10"), True)   # error = 0.90

        assert tracker.adjusted_min_edge == Decimal("0.15")

        await tracker.stop()

    @pytest.mark.asyncio
    async def test_mae_zero_with_no_predictions(self, tmp_path):
        """Sin predicciones, MAE debe ser 0 y min_edge = base."""
        tracker = PerformanceTracker(
            db_path=str(tmp_path / "test_empty.db"),
            base_min_edge=Decimal("0.05"),
        )
        await tracker.start()

        assert tracker.mae == Decimal("0")
        assert tracker.adjusted_min_edge == Decimal("0.05")
        assert tracker.prediction_count == 0

        await tracker.stop()

    @pytest.mark.asyncio
    async def test_get_stats_structure(self, tmp_path):
        """get_stats debe retornar la estructura esperada."""
        tracker = PerformanceTracker(db_path=str(tmp_path / "test_stats.db"))
        await tracker.start()

        stats = tracker.get_stats()
        assert "mae" in stats
        assert "adjusted_min_edge" in stats
        assert "base_min_edge" in stats
        assert "max_min_edge" in stats
        assert "prediction_count" in stats
        assert "window_size" in stats

        await tracker.stop()


class TestMarketQualifierEdgeCases:
    """Pruebas de casos límite adicionales."""

    def test_probability_at_extremes(self):
        """Probabilidades extremas deben ser filtradas correctamente."""
        q = MarketQualifier(min_prob=Decimal("0.30"), max_prob=Decimal("0.70"))
        assert q.check_probability_range(Decimal("0.00")) is not None
        assert q.check_probability_range(Decimal("1.00")) is not None
        assert q.check_probability_range(Decimal("0.29999")) is not None
        assert q.check_probability_range(Decimal("0.70001")) is not None

    def test_volume_zero(self):
        """Volumen cero debe ser filtrado."""
        q = MarketQualifier(min_volume_24h=Decimal("5000"))
        assert q.check_volume(Decimal("0")) is not None

    def test_opportunity_window_missing_type(self):
        """Tipo de mercado no definido debe usar 'default'."""
        now = time.time()
        q = MarketQualifier(opportunity_windows={
            "default": {"window_before_end_s": 60},
        })
        end_time = now + 30
        result = q.check_opportunity_window(end_time, "unknown_type", now)
        assert result is None  # 30s < 60s default window

    def test_signal_without_ev_uses_zero(self):
        """Señal sin campo 'ev' debe tratarse como EV=0."""
        q = MarketQualifier(min_prob=Decimal("0.30"), max_prob=Decimal("0.70"))
        signal = {"probability": "0.50"}
        result = q.check_signal(signal, static_min_edge=Decimal("0.05"))
        assert result is not None
        assert "edge_below_min" in result
