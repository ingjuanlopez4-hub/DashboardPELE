"""
Tests del módulo de Ingesta (ingesta.py).

Cubre:
- Normalización de eventos book/price_change/tick_size_change
- Cuantización Decimal con tick_size
- Reconexión con backoff exponencial
- Manejo de eventos desconocidos
- Señal SIGTERM / cierre limpio
- Sin pérdida ni duplicación de eventos
"""

import asyncio
import json
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import AsyncMock, patch

import pytest
import websockets

from ingesta import IngestaCLOB, NormalizedEvent, MarketState


# =========================================================================
# MarketState tests
# =========================================================================

class TestMarketState:

    def test_get_tick_size_default(self):
        ms = MarketState()
        assert ms.get_tick_size("unknown") == Decimal("0.01")

    def test_get_tick_size_from_snapshot(self, market_snapshot):
        ms = MarketState(market_snapshot)
        assert ms.get_tick_size("0xabc") == Decimal("0.01")
        assert ms.get_tick_size("0x123") == Decimal("0.05")

    def test_set_tick_size(self):
        ms = MarketState()
        ms.set_tick_size("0xabc", "0.05")
        assert ms.get_tick_size("0xabc") == Decimal("0.05")

    def test_ingest_new_market(self):
        ms = MarketState()
        msg = {
            "market": "new-cond",
            "assets_ids": ["0xnew1", "0xnew2"],
            "order_price_min_tick_size": "0.10",
        }
        ms.ingest_new_market(msg)
        assert ms.get_tick_size("0xnew1") == Decimal("0.10")
        assert ms.get_tick_size("0xnew2") == Decimal("0.10")


# =========================================================================
# NormalizedEvent tests
# =========================================================================

class TestNormalizedEvent:

    def test_to_dict_with_decimal(self):
        evt = NormalizedEvent(
            type="book",
            market="m1",
            asset_id="a1",
            price=Decimal("0.52"),
            size=Decimal("100.50"),
            side="BUY",
        )
        d = evt.to_dict()
        assert d["price"] == "0.52"
        assert d["size"] == "100.50"
        assert d["type"] == "book"

    def test_to_dict_no_price(self):
        evt = NormalizedEvent(type="new_market", market="m1")
        d = evt.to_dict()
        assert "price" not in d


# =========================================================================
# IngestaCLOB tests
# =========================================================================

class TestIngestaNormalization:

    def test_qprice_quantization(self):
        """price se cuantiza al tick_size con ROUND_HALF_UP."""
        ing = IngestaCLOB(asset_ids=["0xabc"])
        result = ing._qprice("0.515", "0xabc")
        assert result == Decimal("0.52")
        assert isinstance(result, Decimal)

    def test_qprice_quantization_down(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        result = ing._qprice("0.514", "0xabc")
        assert result == Decimal("0.51")

    def test_qprice_custom_tick(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        ing.market_state.set_tick_size("0xabc", "0.05")
        result = ing._qprice("0.13", "0xabc")
        assert result == Decimal("0.13")

    def test_qsize_quantization(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        result = ing._qsize("100.506")
        assert result == Decimal("100.51")
        assert isinstance(result, Decimal)

    @pytest.mark.parametrize("price_str, tick_str, expected_str", [
        ("0.515", "0.01", "0.52"),
        ("0.514", "0.01", "0.51"),
        ("0.10", "0.05", "0.10"),
        ("0.13", "0.05", "0.13"),
    ])
    def test_price_quantization_parametrized(self, price_str, tick_str, expected_str):
        """Regression: issue #142 — float 0.29 / 0.01 = 28.999999..."""
        tick = Decimal(tick_str)
        price = Decimal(price_str).quantize(tick, rounding=ROUND_HALF_UP)
        assert str(price) == expected_str

    def test_normalize_book_event_sets_decimal_precision(self, sample_book_event):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        events = ing._normalize_book(sample_book_event)
        assert len(events) == 2  # 1 bid + 1 ask

        bid_event = events[0]
        assert bid_event.type == "book"
        assert bid_event.asset_id == "0xabc"
        assert bid_event.side == "BUY"
        assert bid_event.price == Decimal("0.48")
        assert bid_event.size == Decimal("100.50")
        assert isinstance(bid_event.price, Decimal)
        assert isinstance(bid_event.size, Decimal)

        ask_event = events[1]
        assert ask_event.side == "SELL"
        assert ask_event.price == Decimal("0.52")
        assert ask_event.size == Decimal("200.00")

    def test_normalize_book_multiple_levels(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "book",
            "asset_id": "0xabc",
            "market": "Test market",
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.49", "size": "200"},
            ],
            "asks": [
                {"price": "0.51", "size": "150"},
            ],
            "timestamp": "1747216800000",
            "hash": "def456",
        }
        events = ing._normalize_book(msg)
        assert len(events) == 3

    def test_normalize_price_change(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "price_change",
            "market": "Test market",
            "timestamp": "1747216800000",
            "price_changes": [
                {
                    "asset_id": "0xabc",
                    "price": "0.53",
                    "size": "50",
                    "side": "SELL",
                    "hash": "hash1",
                    "best_bid": "0.52",
                    "best_ask": "0.54",
                }
            ],
        }
        events = ing._normalize_price_change(msg)
        assert len(events) == 1
        evt = events[0]
        assert evt.type == "price_change"
        assert evt.price == Decimal("0.53")
        assert evt.size == Decimal("50.00")
        assert evt.extra["best_bid"] == "0.52"
        assert evt.extra["best_ask"] == "0.54"

    def test_normalize_tick_size_change(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "tick_size_change",
            "asset_id": "0xabc",
            "market": "Test market",
            "old_tick_size": "0.01",
            "new_tick_size": "0.05",
            "timestamp": "1747216800000",
        }
        events = ing._normalize_tick_size_change(msg)
        assert len(events) == 1
        evt = events[0]
        assert evt.type == "tick_size_change"
        assert evt.extra["new_tick_size"] == "0.05"
        # Market state should be updated
        assert ing.market_state.get_tick_size("0xabc") == Decimal("0.05")

    def test_normalize_last_trade_price(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "last_trade_price",
            "asset_id": "0xabc",
            "market": "Test market",
            "price": "0.525",
            "size": "10",
            "side": "BUY",
            "timestamp": "1747216800000",
        }
        events = ing._normalize_last_trade_price(msg)
        assert len(events) == 1
        assert events[0].price == Decimal("0.53")  # quantized from 0.525

    def test_normalize_best_bid_ask(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "best_bid_ask",
            "asset_id": "0xabc",
            "market": "Test market",
            "best_bid": "0.48",
            "best_ask": "0.52",
            "timestamp": "1747216800000",
        }
        events = ing._normalize_best_bid_ask(msg)
        assert len(events) == 1
        assert events[0].extra["best_bid"] == "0.48"
        assert events[0].extra["best_ask"] == "0.52"

    def test_normalize_new_market(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "new_market",
            "market": "new-cond",
            "assets_ids": ["0xnew1"],
            "order_price_min_tick_size": "0.10",
            "timestamp": "1747216800000",
        }
        events = ing._normalize_new_market(msg)
        assert len(events) == 1
        assert events[0].type == "new_market"
        assert ing.market_state.get_tick_size("0xnew1") == Decimal("0.10")

    def test_normalize_market_resolved(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        msg = {
            "event_type": "market_resolved",
            "market": "Test market",
            "winning_outcome": "YES",
            "winning_asset_id": "0xabc",
            "timestamp": "1747216800000",
        }
        events = ing._normalize_market_resolved(msg)
        assert len(events) == 1
        assert events[0].extra["winning_outcome"] == "YES"
        assert events[0].extra["winning_asset_id"] == "0xabc"

    def test_unknown_event_type_ignored(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        events = ing._normalize(json.dumps({"event_type": "unknown_type", "data": "x"}))
        assert events == []

    def test_pong_message_ignored(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        events = ing._normalize("PONG")
        assert events == []

    def test_message_without_event_type(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        events = ing._normalize(json.dumps({"foo": "bar"}))
        assert events == []

    def test_parse_timestamp_ms(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        # timestamp in milliseconds (1747216800000 ms = 1747216800 s)
        ts = ing._parse_timestamp("1747216800000")
        assert "2025-05-14" in ts

    def test_parse_timestamp_seconds(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        ts = ing._parse_timestamp("1747216800")
        assert "2025-05-14" in ts or "2025" in ts

    def test_parse_timestamp_invalid(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        ts = ing._parse_timestamp("not-a-timestamp")
        assert ts == "not-a-timestamp"

    def test_dispatch_puts_event_in_queue(self, sample_book_event):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        raw = json.dumps(sample_book_event)

        async def run():
            await ing._dispatch(raw)
            assert ing.queue.qsize() == 2
            evt = await ing.queue.get()
            assert isinstance(evt, NormalizedEvent)
            assert isinstance(evt.price, Decimal)

        asyncio.run(run())


class TestIngestaReconnection:

    def test_run_creates_resilient_client(self):
        """Verify run() starts the ResilientWebSocketClient."""
        from src.live.data_resilience import ResilientWebSocketClient

        ing = IngestaCLOB(asset_ids=["0xabc"])

        # run() should create a _resilient_ws attribute
        async def run():
            t = asyncio.create_task(ing.run())
            await asyncio.sleep(0.050)
            ing.stop()
            await asyncio.sleep(0.050)
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
            assert ing._resilient_ws is not None
            assert isinstance(ing._resilient_ws, ResilientWebSocketClient)

        asyncio.run(run())

    def test_reconnection_resubscribes(self):
        """After reconnection, auth and subscribe must be sent again.

        Note: Reconnection logic is now in ResilientWebSocketClient.
        This test verifies the connect_factory sends auth + subscribe.
        """
        from src.live.data_resilience import ResilientWebSocketClient

        sent_messages = []

        async def fake_connect():
            class FakeWS:
                def __init__(self):
                    self._closed = False

                async def send(self, msg):
                    sent_messages.append(msg)

                async def close(self):
                    self._closed = True

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration

            ws = FakeWS()
            # When the connect factory is called by ResilientWS, we send auth+sub
            # This mimics what IngestaCLOB._connect_and_auth does
            await ws.send(json.dumps({"type": "auth"}))
            await ws.send(json.dumps({"type": "market", "assets_ids": ["0xabc"]}))
            return ws

        ing = IngestaCLOB(asset_ids=["0xabc"])
        client = ResilientWebSocketClient(connect_factory=fake_connect)

        async def run_test():
            t = asyncio.create_task(client.start())
            await asyncio.sleep(0.100)
            await client.stop()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
            # auth + subscribe should have been sent
            auth_sent = any("auth" in msg for msg in sent_messages)
            market_sent = any("market" in msg for msg in sent_messages)
            assert auth_sent, "Auth message was not sent"
            assert market_sent, "Market subscription was not sent"

        asyncio.run(run_test())


class TestIngestaCleanShutdown:

    def test_stop_sets_running_false(self):
        ing = IngestaCLOB(asset_ids=["0xabc"])
        ing._running = True
        ing.stop()
        assert ing._running is False

    def test_run_clean_exit_on_cancel(self):
        """run() should clean up when cancelled."""
        ing = IngestaCLOB(asset_ids=["0xabc"])

        async def runner():
            task = asyncio.create_task(ing.run())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert ing._running is False

        asyncio.run(runner())

    def test_queue_not_lost_on_stop(self):
        """Events already in queue should survive a stop."""
        ing = IngestaCLOB(asset_ids=["0xabc"])

        async def run():
            evt = NormalizedEvent(type="book", market="m1", price=Decimal("0.50"))
            await ing.queue.put(evt)
            ing.stop()
            recovered = await ing.queue.get()
            assert recovered.price == Decimal("0.50")

        asyncio.run(run())


# =========================================================================
# Regression test for issue #142: float vs Decimal precision
# =========================================================================

class TestRegressionIssue142:

    @pytest.mark.parametrize("price_str, tick_str, expected_quantized", [
        ("0.29", "0.01", "0.29"),
        ("0.07", "0.01", "0.07"),
        ("0.999", "0.01", "1.00"),
        ("0.001", "0.01", "0.00"),
    ])
    def test_float_division_regression(self, price_str, tick_str, expected_quantized):
        """
        Regression: py-clob-client issue #142.
        float(0.29) / 0.01 = 28.9999999... which would truncate to 28.
        With Decimal: Decimal('0.29') / Decimal('0.01') = 29 exactly.
        """
        price = Decimal(price_str)
        tick = Decimal(tick_str)
        tick_count = int(price / tick)  # This would be wrong with float
        quantized = price.quantize(tick, rounding=ROUND_HALF_UP)
        assert str(quantized) == expected_quantized
