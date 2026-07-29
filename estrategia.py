"""
Módulo B — Estrategia de trading algorítmico para Polymarket.

Consume eventos normalizados del Módulo A (ingesta), mantiene estado
interno de mercados, calcula señales de trading combinando análisis
Wick-Fishing, sentimiento con FinBERT y simulación Monte Carlo.
Produce señales en una cola asyncio para el Módulo C (ejecución).
"""

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import numpy as np

from ingesta import NormalizedEvent
from src.data.database import PolymarketDatabase
from src.strategy.external_signal import (
    SignalAggregator,
    ChainlinkPriceFeed,
    BinanceSignalFeed,
    ExternalSignal,
    SIGNAL_UP,
    SIGNAL_DOWN,
    SIGNAL_NEUTRAL,
)
from src.strategy.signal_weights import (
    SignalWeightsManager,
    SignalPerformanceTracker,
    DEFAULT_WEIGHTS,
    SIGNAL_SOURCES,
)
from src.strategy.monte_carlo import MonteCarloSimulator

logger = logging.getLogger("estrategia")

DEFAULT_TICK_SIZE = Decimal("0.01")
SIZE_PRECISION = Decimal("0.01")

DEFAULT_CONFIG: dict[str, Any] = {
    "ev_threshold": Decimal("0.02"),           # Reduced from 0.03
    "win_rate_threshold": Decimal("0.50"),
    "kelly_fraction": Decimal("0.25"),
    "min_consensus": 2,                        # At least 2 modules must agree
    "analysis_interval": 5.0,
    "min_analysis_cooldown": 1.0,
    "book_snapshot_window": 100,
    "news_cache_ttl": 300,
    "default_volatility": Decimal("0.50"),
    "default_days_to_expiry": 7,
    "gbm_history_size": 10_000,
    "min_edge": Decimal("0.02"),              # Reduced from 0.05
    "max_position_size_pct": Decimal("3.0"),  # Max 3% per trade
}

SIGNAL_SIDES = {
    "BUY_YES": "BUY_YES",
    "BUY_NO": "BUY_NO",
    "SELL_YES": "SELL_YES",
    "SELL_NO": "SELL_NO",
    "NONE": "NONE",
}


class BookSnapshot:
    """Instantánea del libro de órdenes en un momento dado."""

    __slots__ = ("bids", "asks", "timestamp")

    def __init__(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        timestamp: float,
    ) -> None:
        self.bids = bids
        self.asks = asks
        self.timestamp = timestamp


class AssetState:
    """Estado interno completo de un activo financiero en Polymarket."""

    __slots__ = (
        "asset_id",
        "tick_size",
        "current_bids",
        "current_asks",
        "snapshots",
        "price_history",
        "last_price",
        "best_bid",
        "best_ask",
        "last_update",
        "_last_snapshot_time",
    )

    def __init__(
        self, asset_id: str, tick_size: Decimal, history_size: int = 10_000
    ) -> None:
        self.asset_id = asset_id
        self.tick_size = tick_size
        self.current_bids: dict[Decimal, Decimal] = {}
        self.current_asks: dict[Decimal, Decimal] = {}
        self.snapshots: deque[BookSnapshot] = deque(maxlen=100)
        self.price_history: deque[Decimal] = deque(maxlen=history_size)
        self.last_price: Decimal | None = None
        self.best_bid: Decimal | None = None
        self.best_ask: Decimal | None = None
        self.last_update: float = 0.0
        self._last_snapshot_time: float = 0.0

    def update_bid(self, price: Decimal, size: Decimal) -> None:
        if size == 0:
            self.current_bids.pop(price, None)
        else:
            self.current_bids[price] = size
        self.best_bid = max(self.current_bids.keys()) if self.current_bids else None

    def update_ask(self, price: Decimal, size: Decimal) -> None:
        if size == 0:
            self.current_asks.pop(price, None)
        else:
            self.current_asks[price] = size
        self.best_ask = min(self.current_asks.keys()) if self.current_asks else None

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.last_price

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def snapshot(self) -> None:
        now = time.time()
        if now - self._last_snapshot_time < 0.5:
            return
        bids = sorted(
            [(p, s) for p, s in self.current_bids.items() if s > 0],
            key=lambda x: x[0],
            reverse=True,
        )[:10]
        asks = sorted(
            [(p, s) for p, s in self.current_asks.items() if s > 0],
            key=lambda x: x[0],
        )[:10]
        self.snapshots.append(BookSnapshot(bids, asks, now))
        self._last_snapshot_time = now

    def top_bids(self, n: int = 5) -> list[tuple[Decimal, Decimal]]:
        return sorted(
            [(p, s) for p, s in self.current_bids.items() if s > 0],
            key=lambda x: x[0],
            reverse=True,
        )[:n]

    def top_asks(self, n: int = 5) -> list[tuple[Decimal, Decimal]]:
        return sorted(
            [(p, s) for p, s in self.current_asks.items() if s > 0],
            key=lambda x: x[0],
        )[:n]


class MotorEstrategia:
    """Motor de estrategia que consume eventos y produce señales de trading.

    Parameters
    ----------
    event_queue : asyncio.Queue[NormalizedEvent]
        Cola de entrada con eventos normalizados del Módulo A.
    signal_queue : asyncio.Queue[dict]
        Cola de salida con señales de trading para el Módulo C.
    config : dict | None
        Diccionario de configuración (opcional).
    """

    def __init__(
        self,
        event_queue: asyncio.Queue,
        signal_queue: asyncio.Queue,
        config: dict[str, Any] | None = None,
        signal_aggregator: SignalAggregator | None = None,
        weights_manager: SignalWeightsManager | None = None,
        performance_tracker: SignalPerformanceTracker | None = None,
        monte_carlo_simulator: MonteCarloSimulator | None = None,
        history_db_path: str | None = None,
    ) -> None:
        self.event_queue = event_queue
        self.signal_queue = signal_queue
        self.config = {**DEFAULT_CONFIG, **(config or {})}

        self._assets: dict[str, AssetState] = {}
        self._market_meta: dict[str, dict[str, Any]] = {}
        self._resolved_markets: set[str] = set()
        self._resolved_assets: set[str] = set()
        self._history_db_path = history_db_path
        self._history_db: PolymarketDatabase | None = None
        self._hydrated_assets: set[str] = set()

        self._sentiment_pipeline = None
        self._sentiment_lock = asyncio.Lock()
        self._news_cache: dict[str, tuple[list[str], float]] = {}

        self._last_analysis_time: dict[str, float] = {}
        self._pending_book_hashes: set[str] = set()

        self._running = False
        self._tasks: list[asyncio.Task] = []

        # ── External signal (Chainlink/Binance) ─────────────────────────
        self._signal_aggregator = signal_aggregator

        # ── Dynamic weights manager ─────────────────────────────────────
        self._weights_manager = weights_manager
        self._perf_tracker = performance_tracker

        # ── Monte Carlo (long-term only, reduced scope) ─────────────────
        self._mc_simulator = monte_carlo_simulator or MonteCarloSimulator(
            n_paths=self.config.get("montecarlo_paths", 1000),
        )

        # ── Track which market type each asset belongs to ───────────────
        self._market_types: dict[str, str] = {}  # asset_id -> market_type

    # ------------------------------------------------------------------
    # Gestión de estado
    # ------------------------------------------------------------------

    def _get_or_create_asset(
        self, asset_id: str, tick_size: Decimal | None = None
    ) -> AssetState:
        if asset_id not in self._assets:
            self._assets[asset_id] = AssetState(
                asset_id,
                tick_size or DEFAULT_TICK_SIZE,
                history_size=int(self.config["gbm_history_size"]),
            )
        elif tick_size is not None:
            self._assets[asset_id].tick_size = tick_size
        return self._assets[asset_id]

    async def _hydrate_price_history(self, asset: AssetState) -> None:
        """Load persisted prices once so GBM survives process restarts."""
        if asset.asset_id in self._hydrated_assets or self._history_db is None:
            return
        rows = await self._history_db.get_token_price_history(
            asset.asset_id,
            limit=int(self.config["gbm_history_size"]),
        )
        live_prices = list(asset.price_history)
        asset.price_history.clear()
        asset.price_history.extend(
            Decimal(str(row["mid_price"])) for row in rows
        )
        asset.price_history.extend(live_prices)
        self._hydrated_assets.add(asset.asset_id)
        if rows:
            logger.info(
                "GBM histórico cargado %s: %d puntos",
                asset.asset_id,
                len(rows),
            )

    def _process_event(self, evt: NormalizedEvent) -> None:
        if evt.type == "book":
            asset = self._get_or_create_asset(
                evt.asset_id,
                Decimal(str(evt.extra.get("new_tick_size", "0.01")))
                if "new_tick_size" in evt.extra
                else None,
            )
            if evt.side == "BUY" and evt.price is not None and evt.size is not None:
                asset.update_bid(evt.price, evt.size)
            elif evt.side == "SELL" and evt.price is not None and evt.size is not None:
                asset.update_ask(evt.price, evt.size)
            asset.last_update = time.time()

        elif evt.type == "price_change":
            asset = self._get_or_create_asset(evt.asset_id)
            if evt.price is not None:
                asset.last_price = evt.price
                asset.price_history.append(evt.price)
            bb = evt.extra.get("best_bid")
            ba = evt.extra.get("best_ask")
            if bb is not None:
                asset.best_bid = Decimal(str(bb))
            if ba is not None:
                asset.best_ask = Decimal(str(ba))
            asset.last_update = time.time()

        elif evt.type == "best_bid_ask":
            asset = self._get_or_create_asset(evt.asset_id)
            bb = evt.extra.get("best_bid")
            ba = evt.extra.get("best_ask")
            if bb is not None:
                asset.best_bid = Decimal(str(bb))
            if ba is not None:
                asset.best_ask = Decimal(str(ba))

        elif evt.type == "tick_size_change":
            new_tick = Decimal(str(evt.extra.get("new_tick_size", "0.01")))
            asset = self._get_or_create_asset(evt.asset_id, new_tick)
            logger.info(
                "tick_size actualizado %s: %s", evt.asset_id, new_tick
            )

        elif evt.type == "last_trade_price":
            asset = self._get_or_create_asset(evt.asset_id)
            if evt.price is not None:
                asset.last_price = evt.price
                asset.price_history.append(evt.price)

        elif evt.type == "new_market":
            self._market_meta[evt.market] = dict(evt.extra)
            for key in ("asset_ids", "assets_ids", "clob_token_ids"):
                aids = evt.extra.get(key, [])
                if aids:
                    tick = Decimal(
                        str(
                            evt.extra.get(
                                "order_price_min_tick_size", "0.01"
                            )
                        )
                    )
                    for aid in aids:
                        self._get_or_create_asset(aid, tick)
                    break
            logger.info("nuevo mercado registrado: %s", evt.market)

        elif evt.type == "market_resolved":
            self._resolved_markets.add(evt.market)
            winner = evt.extra.get("winning_asset_id")
            if winner:
                self._resolved_assets.add(winner)
            logger.info(
                "mercado resuelto: %s ganador=%s", evt.market, winner
            )

    # ------------------------------------------------------------------
    # Wick-Fishing
    # ------------------------------------------------------------------

    def compute_wick_signal(self, asset_id: str) -> dict[str, Any]:
        """Analiza el libro de órdenes en busca de patrones Wick-Fishing.

        Retorna un dict con:
            score (Decimal) — señal normalizada [0, 1]
            probability (Decimal) — probabilidad implícita ajustada
            details (dict) — métricas internas
        """
        asset = self._assets.get(asset_id)
        if asset is None or len(asset.snapshots) < 2:
            return {
                "score": Decimal("0.5"),
                "probability": self._current_probability(asset),
                "details": {"reason": "insufficient_data"},
            }

        current_price = self._current_probability(asset)
        wick_events = 0
        total_checks = 0
        max_intensity = Decimal("0")

        for i in range(1, len(asset.snapshots)):
            prev = asset.snapshots[i - 1]
            curr = asset.snapshots[i]

            compare_levels = min(len(prev.asks), len(curr.asks), 5)
            for level in range(compare_levels):
                if level >= len(prev.asks) or level >= len(curr.asks):
                    continue
                prev_p, prev_s = prev.asks[level]
                curr_p, curr_s = curr.asks[level]

                if prev_p != curr_p:
                    continue

                if prev_s > 0 and curr_s == 0:
                    avg_size = self._avg_level_size(asset.snapshots, "asks", level)
                    if avg_size > 0 and prev_s > avg_size * 3:
                        wick_events += 1
                        intensity = prev_s / avg_size
                        if intensity > max_intensity:
                            max_intensity = intensity
                total_checks += 1

            compare_levels = min(len(prev.bids), len(curr.bids), 5)
            for level in range(compare_levels):
                if level >= len(prev.bids) or level >= len(curr.bids):
                    continue
                prev_p, prev_s = prev.bids[level]
                curr_p, curr_s = curr.bids[level]

                if prev_p != curr_p:
                    continue

                if prev_s > 0 and curr_s == 0:
                    avg_size = self._avg_level_size(
                        asset.snapshots, "bids", level
                    )
                    if avg_size > 0 and prev_s > avg_size * 3:
                        wick_events += 1
                        intensity = prev_s / avg_size
                        if intensity > max_intensity:
                            max_intensity = intensity
                total_checks += 1

        if total_checks == 0:
            return {
                "score": Decimal("0.5"),
                "probability": current_price,
                "details": {"reason": "no_comparisons"},
            }

        raw_ratio = Decimal(str(wick_events)) / Decimal(str(total_checks))
        intensity_factor = min(max_intensity / Decimal("10"), Decimal("1"))
        score = min(raw_ratio * Decimal("10"), Decimal("1"))
        score = score * Decimal("0.7") + intensity_factor * Decimal("0.3")

        spread = asset.spread
        spread_adj = Decimal("0")
        if spread is not None and current_price > 0:
            spread_adj = (spread / current_price) * Decimal("0.1")

        wick_prob = current_price * (Decimal("1") + (score - Decimal("0.5")) * spread_adj)
        wick_prob = max(Decimal("0.001"), min(wick_prob, Decimal("0.999")))

        return {
            "score": score.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "probability": wick_prob.quantize(
                asset.tick_size, rounding=ROUND_HALF_UP
            ),
            "details": {
                "wick_events": wick_events,
                "total_checks": total_checks,
                "raw_ratio": float(raw_ratio),
                "max_intensity": float(max_intensity),
            },
        }

    @staticmethod
    def _avg_level_size(
        snapshots: deque, side: str, level: int
    ) -> Decimal:
        sizes = []
        for snap in snapshots:
            levels = snap.bids if side == "bids" else snap.asks
            if level < len(levels):
                sizes.append(levels[level][1])
        if not sizes:
            return Decimal("0")
        return sum(sizes, Decimal("0")) / Decimal(str(len(sizes)))

    # ------------------------------------------------------------------
    # Sentimiento con FinBERT
    # ------------------------------------------------------------------

    async def _ensure_sentiment_pipeline(self) -> Any:
        """Carga el pipeline de FinBERT bajo demanda (lazy loading)."""
        async with self._sentiment_lock:
            if self._sentiment_pipeline is not None:
                return self._sentiment_pipeline
            try:
                from transformers import pipeline

                logger.info("cargando FinBERT (ProsusAI/finbert)…")
                self._sentiment_pipeline = await asyncio.to_thread(
                    pipeline,
                    "sentiment-analysis",
                    model="ProsusAI/finbert",
                )
                logger.info("FinBERT cargado correctamente")
            except ImportError:
                logger.warning(
                    "transformers no instalado; usando sentimiento dummy"
                )
                self._sentiment_pipeline = "dummy"
            except Exception:
                logger.exception("error al cargar FinBERT")
                self._sentiment_pipeline = "dummy"
            return self._sentiment_pipeline

    def fetch_news(self, asset_id: str) -> list[str]:
        """Obtiene titulares recientes para un activo.

        Versión simulada — en producción reemplazar por RSS/API real.
        """
        now = time.time()
        cached = self._news_cache.get(asset_id)
        if cached and (now - cached[1]) < self.config["news_cache_ttl"]:
            return cached[0]

        headlines = self._dummy_news_headlines(asset_id)
        self._news_cache[asset_id] = (headlines, now)
        return headlines

    @staticmethod
    def _dummy_news_headlines(asset_id: str) -> list[str]:
        dummy_pool = [
            f"Encuestas muestran ventaja para el candidato vinculado a {asset_id}",
            f"Análisis: la probabilidad del evento {asset_id} aumenta esta semana",
            f"Volatilidad en mercados de predicción para {asset_id}",
            f"Expertos debaten el resultado de {asset_id}",
            f"Nuevos datos cambian perspectivas para {asset_id}",
            f"El mercado de {asset_id} muestra señales mixtas",
            f"Inversores incrementan posiciones en {asset_id}",
            f"Reporte: sin cambios significativos para {asset_id}",
        ]
        return dummy_pool[:3]

    async def _analyze_sentiment(
        self, texts: list[str]
    ) -> list[dict[str, Any]]:
        """Ejecuta FinBERT sobre una lista de textos en un hilo separado."""
        pipe = await self._ensure_sentiment_pipeline()
        if pipe == "dummy":
            return [
                {"label": "neutral", "score": 1.0} for _ in texts
            ]
        try:
            results = await asyncio.to_thread(pipe, texts)
            return results
        except Exception:
            logger.exception("error en inferencia FinBERT")
            return [{"label": "neutral", "score": 1.0} for _ in texts]

    async def compute_sentiment_signal(
        self, asset_id: str
    ) -> dict[str, Any]:
        """Analiza sentimiento de noticias y lo mapea a probabilidad.

        Retorna:
            score (Decimal) — sentimiento agregado [-1, 1] → [0, 1]
            probability (Decimal) — probabilidad implícita por sentimiento
        """
        asset = self._assets.get(asset_id)
        current_price = self._current_probability(asset)

        headlines = self.fetch_news(asset_id)
        if not headlines:
            return {
                "score": Decimal("0.5"),
                "probability": current_price,
            }

        results = await self._analyze_sentiment(headlines)

        pos_sum = Decimal("0")
        neg_sum = Decimal("0")
        total_weight = Decimal("0")

        for r in results:
            label = r.get("label", "neutral")
            score = Decimal(str(r.get("score", 0.5)))
            if label == "positive":
                pos_sum += score
            elif label == "negative":
                neg_sum += score
            total_weight += Decimal("1")

        if total_weight == 0:
            return {
                "score": Decimal("0.5"),
                "probability": current_price,
            }

        net_sentiment = (pos_sum - neg_sum) / total_weight
        sent_score = (net_sentiment + Decimal("1")) / Decimal("2")
        sent_score = max(Decimal("0"), min(sent_score, Decimal("1")))

        delta = (sent_score - Decimal("0.5")) * Decimal("0.2")
        sent_prob = current_price * (Decimal("1") + delta)
        sent_prob = max(Decimal("0.001"), min(sent_prob, Decimal("0.999")))

        return {
            "score": sent_score.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "probability": sent_prob.quantize(
                self._tick_for(asset_id), rounding=ROUND_HALF_UP
            ),
        }

    # ------------------------------------------------------------------
    # Clasificador de mercado
    # ------------------------------------------------------------------

    def _classify_market(self, asset_id: str) -> str:
        """Classify a market by its duration/type for weight selection.

        Returns one of: "crypto_5min", "crypto_15min", "long_term".

        Uses market metadata duration if available, otherwise falls back
        to default classification.
        """
        # Check cached classification
        cached = self._market_types.get(asset_id)
        if cached:
            return cached

        # Try to find duration from market metadata
        for meta in self._market_meta.values():
            aids = meta.get("asset_ids") or meta.get("assets_ids") or meta.get("clob_token_ids") or []
            if asset_id in set(aids):
                # Check for explicit market duration
                duration_min = meta.get("duration_minutes")
                if duration_min:
                    if int(duration_min) <= 5:
                        mt = "crypto_5min"
                    elif int(duration_min) <= 15:
                        mt = "crypto_15min"
                    else:
                        mt = "long_term"
                    self._market_types[asset_id] = mt
                    return mt

                # Check end_date to compute days remaining
                end_str = meta.get("end_date_iso") or meta.get("close_time") or meta.get("market_close")
                if end_str:
                    try:
                        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        remaining_days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
                        if remaining_days <= 1:
                            mt = "crypto_5min" if remaining_days <= 0.5 else "crypto_15min"
                        else:
                            mt = "long_term"
                        self._market_types[asset_id] = mt
                        return mt
                    except (ValueError, TypeError):
                        pass
                break

        # Default: long_term
        self._market_types[asset_id] = "long_term"
        return "long_term"

    # ------------------------------------------------------------------
    # Señal externa (Chainlink/Binance)
    # ------------------------------------------------------------------

    async def compute_external_signal(
        self,
        asset_id: str,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Compute external signal from Chainlink/Binance price feeds.

        Converts the SignalAggregator's ExternalSignal into the standard
        signal dict format used by MotorEstrategia.

        Parameters
        ----------
        asset_id : str
            Polymarket asset/token ID.
        symbol : str | None
            Underlying asset symbol (e.g., "btcusdt"). If None, derived
            from market metadata or asset_id.

        Returns
        -------
        dict with keys: score, probability, direction, confidence, ev, details
        """
        if self._signal_aggregator is None:
            return {
                "score": Decimal("0.5"),
                "probability": self._current_probability(self._assets.get(asset_id)),
                "direction": "NEUTRAL",
                "confidence": Decimal("0"),
                "ev": Decimal("0"),
                "details": {"reason": "no_aggregator"},
            }

        # Try to get signal for this specific market/asset
        signal = self._signal_aggregator.get_market_signal(asset_id)
        if signal is None and symbol:
            signal = self._signal_aggregator.get_latest_signal(symbol)

        if signal is None:
            return {
                "score": Decimal("0.5"),
                "probability": self._current_probability(self._assets.get(asset_id)),
                "direction": "NEUTRAL",
                "confidence": Decimal("0"),
                "ev": Decimal("0"),
                "details": {"reason": "no_signal_from_feeds"},
            }

        # Convert ExternalSignal to standard signal dict
        direction = signal.direction
        if direction == SIGNAL_UP:
            # UP means probability should increase
            score = Decimal("0.5") + signal.confidence * Decimal("0.5")
            direction_normalized = "UP"
        elif direction == SIGNAL_DOWN:
            score = Decimal("0.5") - signal.confidence * Decimal("0.5")
            direction_normalized = "DOWN"
        else:
            score = Decimal("0.5")
            direction_normalized = "NEUTRAL"

        current_price = self._current_probability(self._assets.get(asset_id))
        ev = abs(score - current_price)

        return {
            "score": score.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "probability": score.quantize(
                self._tick_for(asset_id), rounding=ROUND_HALF_UP
            ),
            "direction": direction_normalized,
            "confidence": signal.confidence,
            "signal": direction_normalized,
            "ev": ev.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "details": {
                "source": signal.source,
                "distance_pct": str(signal.distance_pct),
                "current_price": str(signal.current_price),
                "strike_price": str(signal.strike_price),
                "symbol": signal.symbol,
            },
        }

    # ------------------------------------------------------------------
    # Generación de señales
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _current_probability(asset: AssetState | None) -> Decimal:
        if asset is None:
            return Decimal("0.5")
        return asset.mid_price or asset.last_price or Decimal("0.5")

    def _tick_for(self, asset_id: str) -> Decimal:
        asset = self._assets.get(asset_id)
        return asset.tick_size if asset else DEFAULT_TICK_SIZE

    def _days_to_expiry_mc(self, asset_id: str) -> Decimal | None:
        """Estimate days to expiry from market metadata for Monte Carlo."""
        for meta in self._market_meta.values():
            aids = (
                meta.get("asset_ids")
                or meta.get("assets_ids")
                or meta.get("clob_token_ids")
                or []
            )
            if asset_id in set(aids):
                end_str = (
                    meta.get("end_date_iso")
                    or meta.get("close_time")
                    or meta.get("market_close")
                )
                if end_str:
                    try:
                        end = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        )
                        remaining = (
                            end - datetime.now(timezone.utc)
                        ).total_seconds()
                        return max(
                            Decimal(str(remaining / 86400)),
                            Decimal("0.001"),
                        )
                    except (ValueError, TypeError):
                        pass
                break
        return None

    async def _compute_signal(
        self, asset_id: str
    ) -> dict[str, Any] | None:
        """Calcula la señal combinada para un activo usando pesos dinámicos y consenso.

        Pipeline:
          1. Clasificar mercado (crypto_5min / crypto_15min / long_term)
          2. Obtener señales de todos los módulos activos
          3. Aplicar matriz de pesos dinámicos
          4. Verificar consenso mínimo (≥2 módulos deben coincidir)
          5. Calcular Kelly fraccional
          6. Retornar señal o None
        """
        asset = self._assets.get(asset_id)
        if asset is None:
            return None

        await self._hydrate_price_history(asset)

        current_price = self._current_probability(asset)
        if current_price <= 0 or current_price >= 1:
            return None

        asset.snapshot()

        # 1. Classify market
        market_type = self._classify_market(asset_id)

        # 2. Estimate volatility for Monte Carlo (only for long-term)
        mc_volatility = None
        mc_days_to_expiry = self._days_to_expiry_mc(asset_id)
        if len(asset.price_history) >= 5:
            mc_volatility = MonteCarloSimulator._estimate_volatility(
                list(asset.price_history), asset.spread, asset.mid_price
            )

        # Compute signals from all active sources
        wick_result = self.compute_wick_signal(asset_id)

        sent_result = None
        if market_type == "long_term":
            sent_result = await self.compute_sentiment_signal(asset_id)
        else:
            # FinBERT disabled for short-term markets (latency)
            sent_result = {
                "score": Decimal("0.5"),
                "probability": current_price,
                "direction": "NEUTRAL",
            }

        mc_result = await self._mc_simulator.simulate(
            current_price=current_price,
            volatility=mc_volatility,
            days_to_expiry=mc_days_to_expiry,
            tick_size=asset.tick_size,
            price_history=list(asset.price_history) if len(asset.price_history) >= 2 else None,
            spread=asset.spread,
            mid_price=asset.mid_price,
            asset_id=asset_id,
        )

        external_result = await self.compute_external_signal(asset_id)

        # 3. Gather signals into standardized format
        signals: dict[str, dict[str, Any]] = {}

        signals["wick"] = {
            "score": wick_result["score"],
            "probability": wick_result["probability"],
            "direction": "UP" if wick_result["score"] > Decimal("0.5") else ("DOWN" if wick_result["score"] < Decimal("0.5") else "NEUTRAL"),
            "signal": "UP" if wick_result["score"] > Decimal("0.5") else ("DOWN" if wick_result["score"] < Decimal("0.5") else "NONE"),
            "confidence": (abs(wick_result["score"] - Decimal("0.5")) * Decimal("2")).quantize(SIZE_PRECISION),
        }

        signals["external"] = {
            "score": external_result["score"],
            "probability": external_result.get("probability", current_price),
            "direction": external_result.get("direction", "NEUTRAL"),
            "signal": external_result.get("direction", "NEUTRAL"),
            "confidence": external_result.get("confidence", Decimal("0")),
        }

        # FinBERT only active for long_term markets
        if sent_result is not None and market_type == "long_term":
            signals["finbert"] = {
                "score": sent_result["score"],
                "probability": sent_result["probability"],
                "direction": "UP" if sent_result["score"] > Decimal("0.5") else ("DOWN" if sent_result["score"] < Decimal("0.5") else "NEUTRAL"),
                "signal": "UP" if sent_result["score"] > Decimal("0.5") else ("DOWN" if sent_result["score"] < Decimal("0.5") else "NONE"),
                "confidence": (abs(sent_result["score"] - Decimal("0.5")) * Decimal("2")).quantize(SIZE_PRECISION),
            }
        else:
            signals["finbert"] = {
                "score": Decimal("0.5"),
                "probability": current_price,
                "direction": "NEUTRAL",
                "signal": "NONE",
                "confidence": Decimal("0"),
            }

        # Monte Carlo only active for long_term markets (or throttled)
        if mc_result.get("details", {}).get("disabled", False):
            signals["montecarlo"] = {
                "score": current_price,
                "probability": current_price,
                "direction": "NEUTRAL",
                "signal": "NONE",
                "confidence": Decimal("0"),
            }
        else:
            signals["montecarlo"] = {
                "score": mc_result.get("score", Decimal("0.5")),
                "probability": mc_result.get("probability", current_price),
                "direction": "UP" if mc_result.get("score", Decimal("0.5")) > current_price else ("DOWN" if mc_result.get("score", Decimal("0.5")) < current_price else "NEUTRAL"),
                "signal": "UP" if mc_result.get("score", Decimal("0.5")) > current_price else ("DOWN" if mc_result.get("score", Decimal("0.5")) < current_price else "NONE"),
                "confidence": (abs(mc_result.get("ev", Decimal("0"))) * Decimal("10")).quantize(SIZE_PRECISION),
            }

        # 4. Apply dynamic weights and check consensus
        if self._weights_manager is not None:
            # Use dynamic weights
            consensus_passed, consensus_reason = self._weights_manager.check_consensus(
                signals=signals,
                market_type=market_type,
                min_consensus=int(self.config.get("min_consensus", 2)),
            )

            if not consensus_passed:
                logger.debug(
                    "Consenso no alcanzado para %s (tipo=%s): %s",
                    asset_id, market_type, consensus_reason,
                )
                return None

            weighted = self._weights_manager.compute_weighted_signal(
                signals=signals,
                market_type=market_type,
            )

            combined_prob = weighted["composite_score"]
            direction = weighted["direction"]
            composite_confidence = weighted["confidence"]
            ev = weighted["ev"]
            sources_agreeing = weighted["sources_agreeing"]
            total_active = weighted["total_active_sources"]
        else:
            # Fallback: simple average of all active sources
            active_sigs = [(k, v) for k, v in signals.items()
                           if v.get("signal", "NONE") != "NONE" and v.get("confidence", 0) > 0]

            if len(active_sigs) < int(self.config.get("min_consensus", 2)):
                return None

            avg_prob = sum(s["probability"] for _, s in active_sigs) / Decimal(str(len(active_sigs)))
            combined_prob = max(Decimal("0.001"), min(avg_prob, Decimal("0.999")))
            ev = combined_prob - current_price
            direction = "UP" if ev > 0 else "DOWN"
            composite_confidence = sum(s["confidence"] for _, s in active_sigs) / Decimal(str(len(active_sigs)))
            sources_agreeing = len([s for _, s in active_sigs if (
                (s.get("signal") == "UP" and ev > 0) or
                (s.get("signal") == "DOWN" and ev < 0)
            )])
            total_active = len(active_sigs)

        abs_ev = abs(ev)
        min_edge = self.config.get("min_edge", Decimal("0.02"))

        if abs_ev < min_edge:
            logger.debug(
                "Señal %s: ev=%s < min_edge=%s — descartada",
                asset_id, abs_ev, min_edge,
            )
            return None

        if ev > 0:
            side = "BUY_YES"
            win_rate = combined_prob
            kelly_raw = (combined_prob - current_price) / (
                Decimal("1") - current_price
            ) if current_price < 1 else Decimal("0")
        else:
            side = "BUY_NO"
            win_rate = Decimal("1") - combined_prob
            kelly_raw = (current_price - combined_prob) / current_price if current_price > 0 else Decimal("0")

        if kelly_raw <= 0:
            return None

        if win_rate < self.config["win_rate_threshold"]:
            return None

        # Apply position size cap (max 3% of bankroll per trade)
        max_pos_size_pct = self.config.get("max_position_size_pct", Decimal("3.0"))
        kelly_fractional = min(
            kelly_raw * self.config["kelly_fraction"],
            max_pos_size_pct / Decimal("100"),
        )

        tick = asset.tick_size
        signal: dict[str, Any] = {
            "asset_id": asset_id,
            "market": self._find_market_for_asset(asset_id),
            "market_type": market_type,
            "side": side,
            "probability": combined_prob.quantize(tick, rounding=ROUND_HALF_UP),
            "ev": ev.quantize(tick, rounding=ROUND_HALF_UP),
            "win_rate": win_rate.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "size": kelly_fractional.quantize(
                SIZE_PRECISION, rounding=ROUND_HALF_UP
            ),
            "kelly_fraction": kelly_fractional.quantize(
                SIZE_PRECISION, rounding=ROUND_HALF_UP
            ),
            "current_price": current_price.quantize(
                tick, rounding=ROUND_HALF_UP
            ),
            "price": current_price.quantize(tick, rounding=ROUND_HALF_UP),
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "confidence": composite_confidence.quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP),
            "sources_agreeing": sources_agreeing,
            "total_active_sources": total_active,
            "components": {
                "wick": {
                    "score": str(wick_result["score"]),
                    "prob": str(wick_result["probability"]),
                },
                "external": {
                    "direction": external_result.get("direction", "NEUTRAL"),
                    "confidence": str(external_result.get("confidence", "0")),
                    "details": external_result.get("details", {}),
                },
                "sentiment": {
                    "score": str(sent_result["score"]) if sent_result else "0.5",
                    "prob": str(sent_result["probability"]) if sent_result else str(current_price),
                },
                "montecarlo": {
                    "prob": str(mc_result.get("probability", "0.5")),
                    "ev": str(mc_result.get("ev", "0")),
                    "details": mc_result.get("details", {}),
                },
            },
        }
        return signal

    def _find_market_for_asset(self, asset_id: str) -> str:
        for market, meta in self._market_meta.items():
            for key in ("asset_ids", "assets_ids", "clob_token_ids"):
                aids = meta.get(key, [])
                if asset_id in set(aids):
                    return market
        return ""

    async def _compute_and_emit_signals(self) -> None:
        """Itera sobre todos los activos activos y emite señales."""
        now = time.time()
        for asset_id in list(self._assets.keys()):
            if asset_id in self._resolved_assets:
                continue

            last = self._last_analysis_time.get(asset_id, 0.0)
            if now - last < self.config["min_analysis_cooldown"]:
                continue

            try:
                signal = await self._compute_signal(asset_id)
                if signal is not None:
                    await self.signal_queue.put(signal)
                    self._last_analysis_time[asset_id] = now
                    logger.info(
                        "señal %s %s prob=%s ev=%s size=%s",
                        asset_id,
                        signal["side"],
                        signal["probability"],
                        signal["ev"],
                        signal["size"],
                    )
            except Exception:
                logger.exception("error al computar señal para %s", asset_id)

    # ------------------------------------------------------------------
    # Bucle principal
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Bucle principal: consume eventos y ejecuta análisis periódico."""
        self._running = True
        if self._history_db_path:
            self._history_db = await PolymarketDatabase.create(self._history_db_path)
        logger.info("MotorEstrategia iniciado")

        async def _periodic_analysis() -> None:
            while self._running:
                try:
                    await asyncio.sleep(self.config["analysis_interval"])
                    await self._compute_and_emit_signals()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("error en análisis periódico")

        async def _event_consumer() -> None:
            while self._running:
                try:
                    evt = await asyncio.wait_for(
                        self.event_queue.get(), timeout=1.0
                    )
                    self._process_event(evt)

                    if evt.type in (
                        "price_change",
                        "book",
                        "best_bid_ask",
                        "new_market",
                    ):
                        await self._compute_and_emit_signals()
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("error en consumidor de eventos")

        periodic_task = asyncio.create_task(_periodic_analysis())
        consumer_task = asyncio.create_task(_event_consumer())
        self._tasks = [periodic_task, consumer_task]

        try:
            done, _ = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in done:
                exc = t.exception()
                if exc:
                    logger.error("tarea finalizó con error: %s", exc)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            for t in self._tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self._history_db is not None:
                await self._history_db.close()
                self._history_db = None
            logger.info("MotorEstrategia detenido")

    def stop(self) -> None:
        """Detiene el motor de estrategia."""
        self._running = False
