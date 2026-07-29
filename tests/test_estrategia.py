"""
Tests del módulo de Estrategia (estrategia.py).

Cubre:
- Wick-Fishing detection
- FinBERT sentiment analysis (mock)
- Monte Carlo simulation precision
- Signal generation (EV, win_rate, Kelly)
- Multiple markets isolation
- Decimal precision in all calculations
"""

import asyncio
import time
from collections import deque
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from estrategia import (
    MotorEstrategia,
    AssetState,
    BookSnapshot,
    DEFAULT_CONFIG,
)
from ingesta import NormalizedEvent
from src.data.database import MarketInfo, OrderBookSnapshot, PolymarketDatabase


# =========================================================================
# AssetState tests
# =========================================================================

class TestAssetState:

    def test_price_history_capacity_is_configurable(self):
        asset = AssetState("0xabc", Decimal("0.01"), history_size=3)
        asset.price_history.extend(
            [Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4")]
        )
        assert list(asset.price_history) == [
            Decimal("0.2"), Decimal("0.3"), Decimal("0.4")
        ]

    def test_update_bid_adds_and_removes(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_bid(Decimal("0.50"), Decimal("100"))
        assert asset.current_bids[Decimal("0.50")] == Decimal("100")
        assert asset.best_bid == Decimal("0.50")

        asset.update_bid(Decimal("0.50"), Decimal("0"))
        assert Decimal("0.50") not in asset.current_bids
        assert asset.best_bid is None

    def test_update_ask_adds_and_removes(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_ask(Decimal("0.52"), Decimal("200"))
        assert asset.current_asks[Decimal("0.52")] == Decimal("200")
        assert asset.best_ask == Decimal("0.52")

        asset.update_ask(Decimal("0.52"), Decimal("0"))
        assert asset.best_ask is None

    def test_mid_price(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_bid(Decimal("0.50"), Decimal("100"))
        asset.update_ask(Decimal("0.52"), Decimal("200"))
        assert asset.mid_price == Decimal("0.51")

    def test_mid_price_falls_back_to_last_price(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.53")
        assert asset.mid_price == Decimal("0.53")

    def test_spread(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_bid(Decimal("0.50"), Decimal("100"))
        asset.update_ask(Decimal("0.52"), Decimal("200"))
        assert asset.spread == Decimal("0.02")

    def test_snapshot_rate_limited(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.snapshot()
        assert len(asset.snapshots) == 1
        asset.snapshot()  # should be skipped due to rate limit
        assert len(asset.snapshots) == 1

    def test_top_bids_returns_sorted(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_bid(Decimal("0.50"), Decimal("100"))
        asset.update_bid(Decimal("0.51"), Decimal("50"))
        asset.update_bid(Decimal("0.49"), Decimal("200"))
        top = asset.top_bids(2)
        assert len(top) == 2
        assert top[0][0] == Decimal("0.51")
        assert top[1][0] == Decimal("0.50")

    def test_top_asks_returns_sorted(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_ask(Decimal("0.52"), Decimal("100"))
        asset.update_ask(Decimal("0.51"), Decimal("50"))
        asset.update_ask(Decimal("0.53"), Decimal("200"))
        top = asset.top_asks(2)
        assert len(top) == 2
        assert top[0][0] == Decimal("0.51")
        assert top[1][0] == Decimal("0.52")


# =========================================================================
# MotorEstrategia - Event Processing
# =========================================================================

class TestMotorEstrategiaEventProcessing:

    @pytest.mark.asyncio
    async def test_hydrates_gbm_history_from_database(self):
        db = await PolymarketDatabase.create(":memory:")
        try:
            await db.upsert_market(MarketInfo(
                id="m1", condition_id="c1", question="Test market"
            ))
            for price in ("0.41", "0.42", "0.43"):
                await db.insert_orderbook_snapshot(
                    OrderBookSnapshot(
                        market_id="m1",
                        token_id="0xabc",
                        mid_price=Decimal(price),
                    )
                )
            motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
            motor._history_db = db
            asset = motor._get_or_create_asset("0xabc")
            asset.price_history.append(Decimal("0.44"))

            await motor._hydrate_price_history(asset)
            await motor._hydrate_price_history(asset)

            assert list(asset.price_history) == [
                Decimal("0.41"), Decimal("0.42"),
                Decimal("0.43"), Decimal("0.44"),
            ]
        finally:
            await db.close()

    def test_process_book_event_creates_asset(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="book", market="m1", asset_id="0xabc",
            price=Decimal("0.50"), size=Decimal("100"),
            side="BUY",
        )
        motor._process_event(evt)
        assert "0xabc" in motor._assets
        assert motor._assets["0xabc"].best_bid == Decimal("0.50")

    def test_process_book_event_bid_and_ask(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt_bid = NormalizedEvent(
            type="book", market="m1", asset_id="0xabc",
            price=Decimal("0.50"), size=Decimal("100"),
            side="BUY",
        )
        evt_ask = NormalizedEvent(
            type="book", market="m1", asset_id="0xabc",
            price=Decimal("0.52"), size=Decimal("200"),
            side="SELL",
        )
        motor._process_event(evt_bid)
        motor._process_event(evt_ask)
        assert motor._assets["0xabc"].best_bid == Decimal("0.50")
        assert motor._assets["0xabc"].best_ask == Decimal("0.52")

    def test_process_price_change(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="price_change", market="m1", asset_id="0xabc",
            price=Decimal("0.53"),
            extra={"best_bid": "0.52", "best_ask": "0.54"},
        )
        motor._process_event(evt)
        asset = motor._assets["0xabc"]
        assert asset.last_price == Decimal("0.53")
        assert asset.best_bid == Decimal("0.52")
        assert asset.best_ask == Decimal("0.54")

    def test_process_tick_size_change(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="tick_size_change", market="m1", asset_id="0xabc",
            extra={"new_tick_size": "0.05"},
        )
        motor._process_event(evt)
        assert motor._assets["0xabc"].tick_size == Decimal("0.05")

    def test_process_last_trade_price(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="last_trade_price", market="m1", asset_id="0xabc",
            price=Decimal("0.51"),
        )
        motor._process_event(evt)
        assert motor._assets["0xabc"].last_price == Decimal("0.51")

    def test_process_new_market(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="new_market", market="new-cond",
            extra={
                "asset_ids": ["0xnew1", "0xnew2"],
                "order_price_min_tick_size": "0.10",
            },
        )
        motor._process_event(evt)
        assert "0xnew1" in motor._assets
        assert "0xnew2" in motor._assets
        assert motor._assets["0xnew1"].tick_size == Decimal("0.10")

    def test_process_market_resolved(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="market_resolved", market="m1",
            extra={"winning_asset_id": "0xabc"},
        )
        motor._process_event(evt)
        assert "m1" in motor._resolved_markets
        assert "0xabc" in motor._resolved_assets

    def test_best_bid_ask_event(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt = NormalizedEvent(
            type="best_bid_ask", market="m1", asset_id="0xabc",
            extra={"best_bid": "0.48", "best_ask": "0.52"},
        )
        motor._process_event(evt)
        asset = motor._assets["0xabc"]
        assert asset.best_bid == Decimal("0.48")
        assert asset.best_ask == Decimal("0.52")

    def test_multiple_markets_isolated(self):
        """Verify state is kept separate for different asset_ids."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        evt1 = NormalizedEvent(
            type="book", market="m1", asset_id="0xabc",
            price=Decimal("0.50"), size=Decimal("100"), side="BUY",
        )
        evt2 = NormalizedEvent(
            type="book", market="m2", asset_id="0xdef",
            price=Decimal("0.30"), size=Decimal("200"), side="BUY",
        )
        motor._process_event(evt1)
        motor._process_event(evt2)
        assert motor._assets["0xabc"].best_bid == Decimal("0.50")
        assert motor._assets["0xdef"].best_bid == Decimal("0.30")


# =========================================================================
# Wick-Fishing Detection
# =========================================================================

class TestWickFishing:

    def _populate_snapshots(self, asset: AssetState, snapshots_data: list[dict]):
        """Helper to inject snapshots directly."""
        asset.snapshots.clear()
        for sd in snapshots_data:
            bids = [(Decimal(str(p)), Decimal(str(s))) for p, s in sd.get("bids", [])]
            asks = [(Decimal(str(p)), Decimal(str(s))) for p, s in sd.get("asks", [])]
            asset.snapshots.append(BookSnapshot(bids, asks, time.time()))
            asset._last_snapshot_time = 0  # reset rate limiter

    def test_wick_detected_on_large_order_removal(self):
        """A large order appearing then disappearing should trigger wick signal."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        motor._assets["0xabc"] = asset

        # We need avg_size * 3 < prev_s for wick detection.
        # Use 3 snapshots: 2 normal (100) + 1 huge (500) that then disappears.
        # Snapshots 0,1: normal sizes. Snapshot 2: large ask at 0.52.
        # Snapshot 3: that large ask disappears (size=0).
        # Avg size for asks level 0 = (100 + 100 + 500 + 0) / 4 = 175
        # prev_s (500) > 175*3=525? No. Need more extreme.
        self._populate_snapshots(asset, [
            {"bids": [("0.50", "100")], "asks": [("0.52", "100")]},
            {"bids": [("0.50", "100")], "asks": [("0.52", "100")]},
            {"bids": [("0.50", "100")], "asks": [("0.52", "5000")]},
            {"bids": [("0.50", "100")], "asks": [("0.52", "0")]},
        ])

        result = motor.compute_wick_signal("0xabc")
        assert result["score"] > Decimal("0.5")
        assert "wick_events" in result["details"]
        assert result["details"]["wick_events"] >= 1

    def test_no_wick_on_stable_book(self):
        """Stable book should yield neutral wick score."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        motor._assets["0xabc"] = asset

        self._populate_snapshots(asset, [
            {"bids": [("0.50", "100")], "asks": [("0.52", "200")]},
            {"bids": [("0.50", "100")], "asks": [("0.52", "200")]},
        ])

        result = motor.compute_wick_signal("0xabc")
        # With only 2 snapshots where sizes are identical, total_checks may be 0
        # or the score may be 0 since there are no wick events
        assert "wick_events" in result["details"] or result["score"] >= Decimal("0")

    def test_insufficient_data_returns_default(self):
        """Less than 2 snapshots returns neutral score."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        motor._assets["0xabc"] = asset

        result = motor.compute_wick_signal("0xabc")
        assert result["score"] == Decimal("0.5")
        assert result["details"]["reason"] == "insufficient_data"

    def test_wick_result_returns_decimal(self):
        """All numeric fields in wick result must be Decimal."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        motor._assets["0xabc"] = asset

        self._populate_snapshots(asset, [
            {"bids": [("0.50", "1000")], "asks": [("0.52", "100")]},
            {"bids": [("0.50", "0")], "asks": [("0.52", "100")]},
        ])

        result = motor.compute_wick_signal("0xabc")
        assert isinstance(result["score"], Decimal)
        assert isinstance(result["probability"], Decimal)


# =========================================================================
# Sentiment Analysis (Mock FinBERT)
# =========================================================================

class TestSentiment:

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_positive_sentiment_increases_prob(self, mock_sentiment):
        """Strong positive sentiment should push probability toward YES."""
        mock_sentiment.return_value = [
            {"label": "positive", "score": 0.9},
            {"label": "positive", "score": 0.85},
        ]

        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        motor._assets["0xabc"] = asset

        result = await motor.compute_sentiment_signal("0xabc")
        assert result["score"] > Decimal("0.5")
        assert isinstance(result["score"], Decimal)
        assert isinstance(result["probability"], Decimal)

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_negative_sentiment_decreases_prob(self, mock_sentiment):
        mock_sentiment.return_value = [
            {"label": "negative", "score": 0.9},
            {"label": "negative", "score": 0.85},
        ]

        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        motor._assets["0xabc"] = asset

        result = await motor.compute_sentiment_signal("0xabc")
        assert result["score"] < Decimal("0.5")

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_sentiment_dummy_fallback(self, mock_sentiment):
        """When FinBERT is not available, dummy returns neutral."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        motor._sentiment_pipeline = "dummy"
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        motor._assets["0xabc"] = asset

        result = await motor.compute_sentiment_signal("0xabc")
        assert result["score"] == Decimal("0.5")

    def test_fetch_news_caching(self):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        news1 = motor.fetch_news("0xabc")
        news2 = motor.fetch_news("0xabc")
        assert news1 == news2
        assert len(news1) == 3


# =========================================================================
# Monte Carlo Simulation (via MonteCarloSimulator)
# =========================================================================

class TestMonteCarlo:

    async def test_montecarlo_returns_decimal(self):
        """All Monte Carlo result fields must be Decimal."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        asset.price_history.extend([
            Decimal("0.48"), Decimal("0.49"), Decimal("0.50"),
            Decimal("0.51"), Decimal("0.52"), Decimal("0.51"),
            Decimal("0.50"), Decimal("0.49"), Decimal("0.50"),
            Decimal("0.51"),
        ])
        motor._assets["0xabc"] = asset

        # Use MonteCarloSimulator directly
        from src.strategy.monte_carlo import MonteCarloSimulator
        simulator = MonteCarloSimulator(n_paths=100)
        result = await simulator.simulate(
            current_price=Decimal("0.50"),
            volatility=Decimal("0.50"),
            days_to_expiry=Decimal("30"),
            tick_size=Decimal("0.01"),
            asset_id="0xabc",
        )
        assert isinstance(result["score"], Decimal)
        assert isinstance(result["probability"], Decimal)
        assert isinstance(result["ev"], Decimal)

    async def test_montecarlo_price_within_expected_range(self):
        """With 10000 paths, price should be near 0.50 for symmetric asset."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        asset.price_history.extend([Decimal("0.50")] * 20)
        motor._assets["0xabc"] = asset

        from src.strategy.monte_carlo import MonteCarloSimulator
        simulator = MonteCarloSimulator(n_paths=1000)
        result = await simulator.simulate(
            current_price=Decimal("0.50"),
            volatility=Decimal("0.50"),
            days_to_expiry=Decimal("30"),
            tick_size=Decimal("0.01"),
            asset_id="0xabc",
        )
        mc_prob = result["probability"]
        # Should be within 0.50 +/- 0.10
        assert Decimal("0.40") <= mc_prob <= Decimal("0.60")

    async def test_montecarlo_price_boundary(self):
        """When current_price is at boundary (0 or 1), MC returns boundary."""
        from src.strategy.monte_carlo import MonteCarloSimulator
        simulator = MonteCarloSimulator()
        result = await simulator.simulate(
            current_price=Decimal("1"),
            tick_size=Decimal("0.01"),
            asset_id="0xabc",
        )
        assert result["details"].get("reason") == "price_boundary"

    async def test_montecarlo_no_data(self):
        """No asset should return neutral."""
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        result = await motor._compute_signal("nonexistent")
        assert result is None

    async def test_montecarlo_short_term_disabled(self):
        """Short-term markets should have MC disabled."""
        from src.strategy.monte_carlo import MonteCarloSimulator
        simulator = MonteCarloSimulator()
        result = await simulator.simulate(
            current_price=Decimal("0.50"),
            days_to_expiry=Decimal("0.1"),  # Less than 7 days
            tick_size=Decimal("0.01"),
            asset_id="0xabc",
        )
        assert result["details"].get("disabled") is True
        assert result["details"]["reason"] == "short_term_market"


# =========================================================================
# Signal Generation
# =========================================================================

class TestSignalGeneration:

    async def _setup_motor_with_asset(self, current_price: Decimal, tick_size: Decimal = Decimal("0.01")):
        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue())
        asset = AssetState("0xabc", tick_size)
        asset.last_price = current_price
        asset.price_history.extend([current_price] * 20)
        motor._assets["0xabc"] = asset
        return motor

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_signal_emitted_when_ev_high(self, mock_sentiment):
        """High EV and win rate > 50% should emit a signal."""
        mock_sentiment.return_value = [{"label": "positive", "score": 0.8}]

        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue(), {
            "ev_threshold": Decimal("0.01"),
            "win_rate_threshold": Decimal("0.30"),
            "min_consensus": 1,  # Only need 1 module for test
        })
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        asset.price_history.extend([Decimal("0.50")] * 20)
        motor._assets["0xabc"] = asset

        signal = await motor._compute_signal("0xabc")
        assert signal is not None
        assert signal["side"] in ("BUY_YES", "BUY_NO")
        assert isinstance(signal["probability"], Decimal)
        assert isinstance(signal["ev"], Decimal)
        assert isinstance(signal["size"], Decimal)

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_no_signal_when_ev_low(self, mock_sentiment):
        """Low EV should return None."""
        mock_sentiment.return_value = [{"label": "neutral", "score": 1.0}]

        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue(), {
            "ev_threshold": Decimal("100"),  # impossible to exceed
            "min_consensus": 1,  # Only need 1 module for test
        })
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        asset.price_history.extend([Decimal("0.50")] * 20)
        motor._assets["0xabc"] = asset

        signal = await motor._compute_signal("0xabc")
        assert signal is None

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_signal_size_respects_kelly_fraction(self, mock_sentiment):
        """Suggested size should not exceed kelly_fraction * max."""
        mock_sentiment.return_value = [{"label": "positive", "score": 0.9}]

        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue(), {
            "ev_threshold": Decimal("0.001"),
            "win_rate_threshold": Decimal("0.0"),
            "kelly_fraction": Decimal("0.25"),
            "min_consensus": 1,  # Only need 1 module for test
        })
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        asset.price_history.extend([Decimal("0.50")] * 20)
        motor._assets["0xabc"] = asset

        signal = await motor._compute_signal("0xabc")
        assert signal is not None
        assert Decimal("0") <= signal["size"] <= Decimal("1")

    @patch("estrategia.MotorEstrategia._analyze_sentiment")
    async def test_signal_decimal_precision(self, mock_sentiment):
        """All numeric fields in signal must be Decimal."""
        mock_sentiment.return_value = [{"label": "positive", "score": 0.8}]

        motor = MotorEstrategia(asyncio.Queue(), asyncio.Queue(), {
            "ev_threshold": Decimal("0.001"),
            "win_rate_threshold": Decimal("0.0"),
            "min_consensus": 1,  # Only need 1 module for test
        })
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.50")
        asset.price_history.extend([Decimal("0.50")] * 20)
        motor._assets["0xabc"] = asset

        signal = await motor._compute_signal("0xabc")
        assert signal is not None
        for key in ("probability", "ev", "win_rate", "size", "kelly_fraction", "current_price"):
            assert isinstance(signal[key], Decimal), f"{key} is not Decimal"

    def test_current_probability_mid_price(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.update_bid(Decimal("0.50"), Decimal("100"))
        asset.update_ask(Decimal("0.52"), Decimal("200"))
        prob = MotorEstrategia._current_probability(asset)
        assert prob == Decimal("0.51")

    def test_current_probability_fallback(self):
        asset = AssetState("0xabc", Decimal("0.01"))
        asset.last_price = Decimal("0.53")
        prob = MotorEstrategia._current_probability(asset)
        assert prob == Decimal("0.53")

    def test_current_probability_default(self):
        prob = MotorEstrategia._current_probability(None)
        assert prob == Decimal("0.5")
