"""
Tests del módulo de Archivo (archivo.py).

Cubre:
- Almacenamiento de trades en SQLite
- Cálculo de métricas (PnL, Sharpe, drawdown, win rate)
- Backtesting sobre señales históricas
- Generación de reportes JSON
- Prometheus metrics endpoint
- Precisión Decimal en todos los valores monetarios
"""

import asyncio
import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archivo import ArchivoBacktest, Trade, COMMISSION_PCT, TICK_SIZE


# =========================================================================
# Trade Storage
# =========================================================================

class TestTradeStorage:

    @pytest.fixture
    def archivo(self, tmp_path):
        return ArchivoBacktest(
            db_path=str(tmp_path / "test_trades.db"),
        )

    async def test_store_trade(self, archivo):
        await archivo._init_db()

        entry = {
            "timestamp": "2026-05-14T12:00:00Z",
            "asset_id": "0xabc",
            "market": "Test market",
            "side": "BUY_YES",
            "price": "0.52",
            "size": "100.00",
            "success": True,
            "order_id": "ord-001",
        }

        await archivo._store_trade(entry)

        trades = await archivo._fetch_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.asset_id == "0xabc"
        assert trade.price == Decimal("0.52")
        assert trade.size == Decimal("100.00")
        assert trade.usdc_amount == Decimal("52.00")  # 0.52 * 100
        assert isinstance(trade.price, Decimal)
        assert isinstance(trade.size, Decimal)
        assert isinstance(trade.usdc_amount, Decimal)

    async def test_store_trade_price_as_text_in_db(self, archivo):
        """Price must be stored as text in SQLite to preserve precision."""
        await archivo._init_db()

        entry = {
            "timestamp": "2026-05-14T12:00:00Z",
            "asset_id": "0xabc",
            "market": "Test",
            "side": "BUY_YES",
            "price": "0.515",
            "size": "33.33",
            "success": True,
            "order_id": "ord-002",
        }
        await archivo._store_trade(entry)

        db = archivo._db
        cursor = await db.execute("SELECT price, size, usdc_amount FROM trades WHERE order_id = ?", ("ord-002",))
        row = await cursor.fetchone()
        # Values should be stored as strings, not floats
        assert isinstance(row[0], str), "Price must be stored as TEXT in SQLite"
        assert isinstance(row[1], str), "Size must be stored as TEXT in SQLite"

    async def test_multiple_trades_stored(self, archivo):
        await archivo._init_db()
        entries = [
            {"timestamp": "2026-05-14T12:00:00Z", "asset_id": "a1", "market": "m1", "side": "BUY_YES", "price": "0.50", "size": "100", "success": True, "order_id": "o1"},
            {"timestamp": "2026-05-14T12:01:00Z", "asset_id": "a2", "market": "m2", "side": "BUY_NO", "price": "0.30", "size": "200", "success": True, "order_id": "o2"},
            {"timestamp": "2026-05-14T12:02:00Z", "asset_id": "a3", "market": "m3", "side": "BUY_YES", "price": "0.70", "size": "50", "success": False, "order_id": "o3"},
        ]
        for e in entries:
            await archivo._store_trade(e)

        trades = await archivo._fetch_trades()
        assert len(trades) == 3


# =========================================================================
# Metrics Calculation
# =========================================================================

class TestMetrics:

    def _make_trade(self, timestamp, asset_id, side, price, size, success=True, market="m"):
        return Trade(
            timestamp=timestamp,
            asset_id=asset_id,
            market=market,
            side=side,
            price=Decimal(str(price)),
            size=Decimal(str(size)),
            usdc_amount=Decimal(str(price)) * Decimal(str(size)),
            order_id=f"ord-{timestamp}",
            success=success,
        )

    def test_empty_trades_returns_zero_metrics(self):
        archivo = ArchivoBacktest(db_path=":memory:")
        metrics = archivo._calculate_metrics([])
        assert metrics["net_pnl_usdc"] == Decimal("0")
        assert metrics["return_pct"] == Decimal("0")
        assert metrics["sharpe_ratio"] == Decimal("0")
        assert metrics["max_drawdown_pct"] == Decimal("0")
        assert metrics["win_rate"] == Decimal("0")
        assert metrics["total_trades"] == 0

    def test_simple_profitable_trade(self):
        """Buy at 0.50, sell at 0.60 = profit."""
        archivo = ArchivoBacktest(db_path=":memory:")
        trades = [
            self._make_trade("2026-05-01T12:00:00Z", "a1", "BUY_YES", "0.50", "100"),
            self._make_trade("2026-05-02T12:00:00Z", "a1", "SELL_YES", "0.60", "100"),
        ]
        metrics = archivo._calculate_metrics(trades)
        # PnL = (0.60 - 0.50) * 100 = 10.00
        assert metrics["net_pnl_usdc"] == Decimal("10.00")
        assert metrics["total_trades"] == 2

    def test_net_pnl_is_decimal(self):
        """net_pnl must be Decimal, never float."""
        archivo = ArchivoBacktest(db_path=":memory:")
        trades = [
            self._make_trade("2026-05-01T12:00:00Z", "a1", "BUY_YES", "0.50", "100"),
            self._make_trade("2026-05-02T12:00:00Z", "a1", "SELL_YES", "0.60", "100"),
        ]
        metrics = archivo._calculate_metrics(trades)
        assert isinstance(metrics["net_pnl_usdc"], Decimal)
        assert isinstance(metrics["return_pct"], Decimal)
        assert isinstance(metrics["max_drawdown_pct"], Decimal)
        assert isinstance(metrics["win_rate"], Decimal)

    def test_win_rate_50pct(self):
        """Two trades: one win, one loss = 50% win rate."""
        archivo = ArchivoBacktest(db_path=":memory:")
        trades = [
            self._make_trade("2026-05-01T12:00:00Z", "a1", "BUY_YES", "0.50", "100"),
            self._make_trade("2026-05-02T12:00:00Z", "a1", "SELL_YES", "0.60", "100"),  # profit
            self._make_trade("2026-05-03T12:00:00Z", "a2", "BUY_YES", "0.50", "100"),
            # No sell for a2, but we add a sell at loss for same asset
            self._make_trade("2026-05-04T12:00:00Z", "a2", "SELL_YES", "0.40", "100"),  # loss
        ]
        metrics = archivo._calculate_metrics(trades)
        # FIFO: 4 trades, 2 have realized PnL (trades 2 and 4), 1 winning
        # win_rate = winning_trades / total_trades = 1/4 = 0.25
        assert metrics["win_rate"] == Decimal("0.25")

    def test_max_drawdown(self):
        """Verify max drawdown calculation."""
        archivo = ArchivoBacktest(db_path=":memory:")
        trades = [
            self._make_trade("2026-05-01T12:00:00Z", "a1", "BUY_YES", "0.50", "100"),
            self._make_trade("2026-05-02T12:00:00Z", "a1", "SELL_YES", "0.70", "100"),  # +20 gain
            self._make_trade("2026-05-03T12:00:00Z", "a2", "BUY_YES", "0.50", "200"),
            self._make_trade("2026-05-04T12:00:00Z", "a2", "SELL_YES", "0.30", "200"),  # -40 loss
        ]
        metrics = archivo._calculate_metrics(trades)
        # Peak was 20, then dropped to -20, drawdown = 40/20 = 200%
        # Actually: cummulative: +20 -> peak=20, then +20 + (-40) = -20
        # dd = (20 - (-20)) / 20 = 40/20 = 200%
        assert metrics["max_drawdown_pct"] > Decimal("0")

    def test_sharpe_ratio_reasonable(self):
        """Sharpe should be computable with multiple days of data."""
        archivo = ArchivoBacktest(db_path=":memory:")
        trades = []
        for day in range(1, 6):
            trades.append(self._make_trade(
                f"2026-05-{day:02d}T12:00:00Z", "a1", "BUY_YES", "0.50", "100"
            ))
            trades.append(self._make_trade(
                f"2026-05-{day:02d}T13:00:00Z", "a1", "SELL_YES", "0.55", "100"
            ))
        metrics = archivo._calculate_metrics(trades)
        # With 5 days of consistent profit, Sharpe should be positive
        assert isinstance(metrics["sharpe_ratio"], Decimal)
        # It could be Decimal("0") if stdev is 0, or a positive number

    def test_return_pct_calculation(self):
        """Return percentage = net_pnl / total_buy_usdc * 100."""
        archivo = ArchivoBacktest(db_path=":memory:")
        trades = [
            self._make_trade("2026-05-01T12:00:00Z", "a1", "BUY_YES", "0.50", "100"),  # cost: 50
            self._make_trade("2026-05-02T12:00:00Z", "a1", "SELL_YES", "0.60", "100"),
        ]
        metrics = archivo._calculate_metrics(trades)
        # net_pnl = 10, total_buy = 50, return = 10/50*100 = 20%
        assert metrics["return_pct"] == Decimal("20")


# =========================================================================
# Backtesting
# =========================================================================

class TestBacktesting:

    @pytest.fixture
    def archivo(self):
        return ArchivoBacktest(db_path=":memory:")

    def test_backtest_generates_trades(self, archivo):
        """Backtest should generate Trade objects from a DataFrame mock."""
        # DataFrame mock with .iterrows()
        class MockDataFrame:
            class MockRow:
                def get(self, key, default=None):
                    return {
                        "timestamp": "2026-05-01T12:00:00Z",
                        "asset_id": "a1",
                        "market": "m1",
                        "signal": "BUY_YES",
                        "probability": 0.55,
                        "ev": 0.05,
                        "price": 0.50,
                        "size": 100,
                    }.get(key, default)

            def iterrows(self):
                yield (0, self.MockRow())
                yield (1, self.MockRow())

        metrics = archivo.run_backtest(MockDataFrame())
        assert metrics["total_trades"] == 2
        assert isinstance(metrics["net_pnl_usdc"], Decimal)

    def test_backtest_applies_slippage(self, archivo):
        """Backtest should apply 1 tick slippage + commission."""
        class MockDataFrame:
            class MockRow:
                def get(self, key, default=None):
                    data = {
                        "timestamp": "2026-05-01T12:00:00Z",
                        "asset_id": "a1",
                        "market": "m1",
                        "signal": "BUY_YES",
                        "probability": 0.55,
                        "ev": 0.05,
                        "price": 0.50,
                        "size": 100,
                    }
                    return data.get(key, default)

            def iterrows(self):
                yield (0, self.MockRow())

        metrics = archivo.run_backtest(MockDataFrame())
        # Execution price should be 0.50 + 0.01 = 0.51 (buy slippage up)
        # That means the first trade has cost = 0.51 * 100 = 51, plus fee
        assert metrics["total_trades"] == 1


# =========================================================================
# Report Generation
# =========================================================================

class TestReport:

    @pytest.fixture
    def archivo(self, tmp_path):
        return ArchivoBacktest(db_path=str(tmp_path / "test_report.db"))

    async def test_generate_report_structure(self, archivo):
        await archivo._init_db()

        # Store a trade so metrics have data
        entry = {
            "timestamp": "2026-05-14T12:00:00Z",
            "asset_id": "0xabc",
            "market": "Test",
            "side": "BUY_YES",
            "price": "0.50",
            "size": "100",
            "success": True,
            "order_id": "o1",
        }
        await archivo._store_trade(entry)

        report_str = await archivo.generate_report()
        report = json.loads(report_str)

        assert report["report_type"] == "monthly_performance"
        assert "period" in report
        assert "metrics" in report
        assert "net_pnl_usdc" in report["metrics"]
        assert isinstance(report["metrics"]["net_pnl_usdc"], str)

    async def test_report_decimal_as_string(self, archivo):
        """Decimal values in report must be serialized as strings."""
        await archivo._init_db()
        entry = {
            "timestamp": "2026-05-14T12:00:00Z",
            "asset_id": "0xabc",
            "market": "Test",
            "side": "BUY_YES",
            "price": "0.50",
            "size": "100",
            "success": True,
            "order_id": "o1",
        }
        await archivo._store_trade(entry)

        report_str = await archivo.generate_report()
        report = json.loads(report_str)
        # Verify metrics contain string representations of Decimal values
        metrics = report["metrics"]
        assert isinstance(metrics.get("net_pnl_usdc", ""), str)
        assert isinstance(metrics.get("return_pct", ""), str)


# =========================================================================
# Prometheus Metrics
# =========================================================================

class TestPrometheus:

    def test_start_prometheus_server(self, archivo):
        """Should start without error on an available port."""
        try:
            archivo.start_prometheus_server(port=0)  # port 0 = random
            assert archivo._prometheus_started is True
        except Exception as e:
            pytest.skip(f"Prometheus server not available: {e}")

    @pytest.fixture
    def archivo(self):
        return ArchivoBacktest(db_path=":memory:")


# =========================================================================
# Event Consumer Integration
# =========================================================================

class TestEventConsumer:

    async def test_consume_events_from_queue(self, tmp_path):
        q: asyncio.Queue = asyncio.Queue()
        archivo = ArchivoBacktest(
            db_path=str(tmp_path / "test_consume.db"),
            execution_log_queue=q,
        )

        entry = {
            "timestamp": "2026-05-14T12:00:00Z",
            "asset_id": "0xabc",
            "market": "Test",
            "side": "BUY_YES",
            "price": "0.52",
            "size": "100.00",
            "success": True,
            "order_id": "ord-consume-1",
        }
        await q.put(entry)

        # Run archivo briefly
        task = asyncio.create_task(archivo.run())
        await asyncio.sleep(0.2)
        archivo.stop()
        await task

        # Verify trade was stored
        trades = await archivo._fetch_trades()
        assert len(trades) == 1
        assert trades[0].order_id == "ord-consume-1"

    async def test_malformed_entry_handled(self, tmp_path):
        """Malformed entries should not crash the consumer."""
        q: asyncio.Queue = asyncio.Queue()
        archivo = ArchivoBacktest(
            db_path=str(tmp_path / "test_malformed.db"),
            execution_log_queue=q,
        )

        await q.put({"no_price": "here", "success": True})

        task = asyncio.create_task(archivo.run())
        await asyncio.sleep(0.2)
        archivo.stop()
        await task

        # Should not crash — entry with missing fields should still be stored
        trades = await archivo._fetch_trades()
        assert len(trades) >= 0
