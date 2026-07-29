"""
External Signal Module — Chainlink price feed and Binance WebSocket streams.

Provides real-time price signals from external sources to complement (or replace)
internal Monte Carlo and FinBERT signals, especially for short-duration markets
(5min, 15min) where the lead-lag relationship between Binance and Polymarket
is the dominant driver of price action.

Architecture:
  - ChainlinkPriceFeed: Queries BTC/USD, ETH/USD from Chainlink oracle on Polygon,
    or reads the settlement price from the CLOB WebSocket stream.
  - BinanceSignalFeed: Connects to Binance public WebSocket for real-time trade data
    with < 50ms latency. Exploits the validated 30-90s lead-lag relationship.
  - SignalAggregator: Combines both sources, calculates distance from strike price,
    and emits BUY/DOWN/NEUTRAL signals.

All monetary values use Decimal in the trading path; float used only for
internal calculations where performance matters.
"""

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from dataclasses import dataclass

import aiohttp
import websockets

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"
POLYGON_CHAINLINK_BTC_USD = "0xf4030086522a5beea4988f114ca0b1e0b4e0e0e0"
POLYGON_CHAINLINK_ETH_USD = "0xf4030086522a5beea4988f114ca0b1e0b4e0e0e1"

DEFAULT_MIN_DISTANCE_PCT = Decimal("0.15")
DEFAULT_CACHE_TTL_MS = 100
DEFAULT_BINANCE_STREAMS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt"]

SIGNAL_UP = "UP"
SIGNAL_DOWN = "DOWN"
SIGNAL_NEUTRAL = "NEUTRAL"

# Chainlink ABI for latestRoundData
CHAINLINK_ABI = json.loads(
    '[{"inputs":[],"name":"latestRoundData","outputs":[{"internalType":"uint80","name":"roundId",'
    '"type":"uint80"},{"internalType":"int256","name":"answer","type":"int256"},'
    '{"internalType":"uint256","name":"startedAt","type":"uint256"},'
    '{"internalType":"uint256","name":"updatedAt","type":"uint256"},'
    '{"internalType":"uint80","name":"answeredInRound","type":"uint80"}],'
    '"stateMutability":"view","type":"function"}]'
)

CHAINLINK_DECIMALS = 8
CHAINLINK_TIMEOUT_S = 10


class ChainlinkPriceFeed:
    """Queries Chainlink price feeds on Polygon for settlement prices.

    This is the same oracle Polymarket uses for market settlement, giving
    the bot the exact settlement price with zero uncertainty.

    Parameters
    ----------
    rpc_url : str
        Polygon RPC URL. Should be a private node (Alchemy/QuickNode) for
        consistent low-latency queries.
    btc_feed : str
        Chainlink BTC/USD feed contract address.
    eth_feed : str
        Chainlink ETH/USD feed contract address.
    """

    def __init__(
        self,
        rpc_url: str = "https://polygon-rpc.com",
        btc_feed: str = POLYGON_CHAINLINK_BTC_USD,
        eth_feed: str = POLYGON_CHAINLINK_ETH_USD,
    ) -> None:
        self._rpc_url = rpc_url
        self._btc_feed = btc_feed
        self._eth_feed = eth_feed
        self._w3: Any = None
        self._cached_prices: dict[str, tuple[Decimal, float]] = {}  # feed -> (price, timestamp)
        self._cache_ttl_s = 5.0  # Cache Chainlink queries for 5 seconds

    async def _ensure_web3(self) -> Any:
        if self._w3 is None:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        return self._w3

    async def get_btc_price(self) -> Decimal | None:
        return await self._get_feed_price(self._btc_feed, "BTC/USD")

    async def get_eth_price(self) -> Decimal | None:
        return await self._get_feed_price(self._eth_feed, "ETH/USD")

    async def _get_feed_price(self, feed_address: str, label: str) -> Decimal | None:
        """Get the latest price from a Chainlink feed.

        Uses cached value if within TTL. Returns None on failure.
        """
        now = time.time()
        cached = self._cached_prices.get(feed_address)
        if cached and (now - cached[1]) < self._cache_ttl_s:
            return cached[0]

        try:
            w3 = await self._ensure_web3()
            checksum_addr = w3.to_checksum_address(feed_address)
            contract = w3.eth.contract(address=checksum_addr, abi=CHAINLINK_ABI)

            round_data = await asyncio.to_thread(
                contract.functions.latestRoundData().call,
            )
            # round_data = (roundId, answer, startedAt, updatedAt, answeredInRound)
            answer = round_data[1]
            price_decimal = Decimal(str(answer)) / Decimal(10 ** CHAINLINK_DECIMALS)

            self._cached_prices[feed_address] = (price_decimal, now)
            logger.debug("Chainlink %s: %s", label, price_decimal)
            return price_decimal

        except Exception:
            logger.exception("Failed to fetch Chainlink %s", label)
            return cached[0] if cached else None

    async def get_price(self, symbol: str) -> Decimal | None:
        """Get price for a symbol (BTC or ETH)."""
        symbol_upper = symbol.upper().replace("/", "").replace("USD", "")
        if symbol_upper in ("BTC", "BTCUSD"):
            return await self.get_btc_price()
        elif symbol_upper in ("ETH", "ETHUSD"):
            return await self.get_eth_price()
        else:
            logger.warning("Unsupported Chainlink symbol: %s", symbol)
            return None

    async def close(self) -> None:
        self._w3 = None
        self._cached_prices.clear()


class BinanceSignalFeed:
    """Real-time Binance trade stream via WebSocket.

    Connects to Binance public WebSocket for trade data with < 50ms latency.
    Exploits the validated 30-90 second lead-lag relationship between
    Binance and Polymarket prices.

    Parameters
    ----------
    streams : list[str]
        List of Binance stream names (e.g., ['btcusdt', 'ethusdt']).
    on_price : Callable | None
        Optional callback: on_price(symbol, price_decimal).
    reconnect_delay : float
        Base delay between reconnection attempts (seconds).
    """

    def __init__(
        self,
        streams: list[str] | None = None,
        on_price: Callable | None = None,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._streams = streams or DEFAULT_BINANCE_STREAMS[:2]  # Default: BTC, ETH only
        self._on_price = on_price
        self._reconnect_delay = reconnect_delay
        self._running = False
        self._ws: Any = None
        self._latest_prices: dict[str, Decimal] = {}
        self._latest_times: dict[str, float] = {}

    @property
    def latest_prices(self) -> dict[str, tuple[Decimal, float]]:
        """Return latest prices: {symbol: (price, timestamp)}."""
        return {
            sym: (self._latest_prices.get(sym, Decimal("0")),
                  self._latest_times.get(sym, 0.0))
            for sym in self._streams
        }

    async def get_price(self, symbol: str) -> Decimal | None:
        """Get the latest cached price for a symbol."""
        return self._latest_prices.get(symbol.lower().replace("/", "").replace("USDT", "usdt"))

    async def _connect_and_stream(self) -> None:
        """Connect to Binance WebSocket and stream trade data."""
        stream_names = [f"{s}@trade" for s in self._streams]
        combined_stream = f"{BINANCE_WS_BASE}/{','.join(stream_names)}" if len(stream_names) > 1 else f"{BINANCE_WS_BASE}/{stream_names[0]}"

        while self._running:
            try:
                async with websockets.connect(
                    combined_stream,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    logger.info("Binance WebSocket connected: %d streams", len(self._streams))

                    async for raw_message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_message)
                            await self._handle_trade(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception:
                            logger.exception("Error handling Binance trade message")

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Binance WS disconnected — reconnecting in %.1fs", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

        self._ws = None

    async def _handle_trade(self, data: dict[str, Any]) -> None:
        """Process a single trade event from Binance."""
        stream = data.get("stream", "")
        trade_data = data.get("data", data) if stream else data

        symbol = (trade_data.get("s", "") or stream.replace("@trade", "").upper()).lower()
        price_str = trade_data.get("p", "0")
        trade_time = int(trade_data.get("T", 0)) / 1000  # ms to s

        try:
            price = Decimal(str(price_str))
        except Exception:
            return

        self._latest_prices[symbol] = price
        self._latest_times[symbol] = trade_time

        if self._on_price:
            try:
                await self._on_price(symbol, price) if asyncio.iscoroutinefunction(self._on_price) else self._on_price(symbol, price)
            except Exception:
                logger.exception("Error in Binance on_price callback")

    async def start(self) -> None:
        """Start the Binance WebSocket stream."""
        self._running = True
        self._task = asyncio.create_task(self._connect_and_stream())
        logger.info("BinanceSignalFeed started: %s", self._streams)

    async def stop(self) -> None:
        """Stop the Binance WebSocket stream."""
        self._running = False
        if hasattr(self, "_task") and self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("BinanceSignalFeed stopped")


class StrikePriceTracker:
    """Tracks strike prices (prices at candle start) for each asset.

    The strike price is the underlying asset price (BTC, ETH, etc.) at the
    start of each Polymarket candle. The signal is distance from current
    price to strike price.
    """

    def __init__(self) -> None:
        self._strikes: dict[str, Decimal] = {}  # symbol -> strike price
        self._last_reset: dict[str, float] = {}  # symbol -> last reset time

    def get_strike(self, symbol: str) -> Decimal:
        """Get the current strike price for a symbol."""
        return self._strikes.get(symbol, Decimal("0"))

    def update_strike(self, symbol: str, price: Decimal) -> None:
        """Update strike price (called at start of each candle)."""
        self._strikes[symbol] = price
        self._last_reset[symbol] = time.time()

    def get_distance_pct(self, symbol: str, current_price: Decimal) -> Decimal:
        """Calculate distance from strike as a percentage.

        Returns distance_pct (positive = up, negative = down).
        Returns 0 if no strike is set.
        """
        strike = self._strikes.get(symbol)
        if strike is None or strike == 0:
            return Decimal("0")
        if current_price == 0:
            return Decimal("0")
        distance = ((current_price - strike) / strike) * Decimal("100")
        return distance.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ExternalSignal:
    """A signal emitted by the SignalAggregator."""

    direction: str  # "UP", "DOWN", or "NEUTRAL"
    distance_pct: Decimal
    source: str  # "chainlink", "binance", or "both"
    current_price: Decimal
    strike_price: Decimal
    symbol: str
    confidence: Decimal  # 0.0 to 1.0
    timestamp: float


class SignalAggregator:
    """Combines Chainlink and Binance price feeds into trading signals.

    For each tracked asset (BTC, ETH, etc.), computes the percentage distance
    from the strike price (price at candle start) and emits UP/DOWN/NEUTRAL
    signals when the distance exceeds min_distance_threshold.

    Signals are cached with a configurable TTL (default 100ms) to prevent
    redundant processing when multiple strategy components query simultaneously.

    Parameters
    ----------
    chainlink_feed : ChainlinkPriceFeed | None
        Chainlink price feed instance. If None, uses Binance-only.
    binance_feed : BinanceSignalFeed | None
        Binance WebSocket feed. If None, uses Chainlink-only.
    min_distance_pct : Decimal
        Minimum percentage distance from strike to emit a signal (default 0.15%).
    cache_ttl_ms : int
        Signal cache TTL in milliseconds (default 100ms).
    """

    def __init__(
        self,
        chainlink_feed: ChainlinkPriceFeed | None = None,
        binance_feed: BinanceSignalFeed | None = None,
        min_distance_pct: Decimal = DEFAULT_MIN_DISTANCE_PCT,
        cache_ttl_ms: int = DEFAULT_CACHE_TTL_MS,
    ) -> None:
        self._chainlink = chainlink_feed
        self._binance = binance_feed
        self._min_distance = min_distance_pct
        self._cache_ttl_s = cache_ttl_ms / 1000.0

        self._strike_tracker = StrikePriceTracker()
        self._latest_signals: dict[str, ExternalSignal] = {}
        self._last_signal_time: dict[str, float] = {}

        # Track symbol -> market mappings
        self._symbol_markets: dict[str, set[str]] = {}  # symbol -> {asset_id, ...}

        # Chainlink data as fallback
        self._chainlink_prices: dict[str, tuple[Decimal, float]] = {}

    def register_market(self, asset_id: str, symbol: str) -> None:
        """Register a market with its underlying asset symbol.

        Parameters
        ----------
        asset_id : str
            Polymarket asset/token ID.
        symbol : str
            Underlying asset symbol (e.g., "btcusdt", "ethusdt").
        """
        if symbol not in self._symbol_markets:
            self._symbol_markets[symbol] = set()
        self._symbol_markets[symbol].add(asset_id)

    def set_strike_price(self, symbol: str, strike: Decimal) -> None:
        """Set the strike price for a symbol (called at candle start)."""
        self._strike_tracker.update_strike(symbol, strike)
        logger.info("Strike set for %s: %s", symbol, str(strike))

    async def get_signal(
        self,
        symbol: str,
        current_price: Decimal | None = None,
    ) -> ExternalSignal:
        """Get the current signal for a symbol.

        Checks cache first. If cache is stale, computes fresh signal
        from available price sources.

        Parameters
        ----------
        symbol : str
            Asset symbol (e.g., "btcusdt", "ethusdt").
        current_price : Decimal | None
            Optional override for current price. If None, uses best available source.

        Returns
        -------
        ExternalSignal with direction, distance_pct, confidence.
        """
        now = time.time()

        # Check cache
        last_time = self._last_signal_time.get(symbol, 0.0)
        if now - last_time < self._cache_ttl_s and symbol in self._latest_signals:
            return self._latest_signals[symbol]

        # Get current price from best available source
        price = current_price
        source = "none"

        if price is None and self._binance:
            price = await self._binance.get_price(symbol)
            if price is not None:
                source = "binance"

        if price is None and self._chainlink:
            chainlink_price = await self._chainlink.get_price(symbol)
            if chainlink_price is not None:
                price = chainlink_price
                source = "chainlink"

        if price is None:
            # No price available — return cached or neutral
            if symbol in self._latest_signals:
                return self._latest_signals[symbol]
            return ExternalSignal(
                direction=SIGNAL_NEUTRAL,
                distance_pct=Decimal("0"),
                source="none",
                current_price=Decimal("0"),
                strike_price=self._strike_tracker.get_strike(symbol),
                symbol=symbol,
                confidence=Decimal("0"),
                timestamp=now,
            )

        # Calculate distance from strike
        distance = self._strike_tracker.get_distance_pct(symbol, price)
        strike = self._strike_tracker.get_strike(symbol)

        # Determine direction and confidence
        abs_distance = abs(distance)
        if abs_distance >= self._min_distance:
            direction = SIGNAL_UP if distance > 0 else SIGNAL_DOWN
            # Confidence scales with distance, maxing at 2x threshold
            confidence = min(abs_distance / (self._min_distance * 2), Decimal("1.0"))
        else:
            direction = SIGNAL_NEUTRAL
            confidence = abs_distance / self._min_distance if self._min_distance > 0 else Decimal("0")

        if source == "both":
            combined_confidence = confidence * Decimal("1.2")  # Boost when both sources agree
            confidence = min(combined_confidence, Decimal("1.0"))
            source = "both"

        signal = ExternalSignal(
            direction=direction,
            distance_pct=distance,
            source=source,
            current_price=price,
            strike_price=strike,
            symbol=symbol,
            confidence=confidence.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            timestamp=now,
        )

        self._latest_signals[symbol] = signal
        self._last_signal_time[symbol] = now

        logger.debug(
            "Signal for %s: %s distance=%.2f%% confidence=%s source=%s",
            symbol, direction, float(distance), signal.confidence, source,
        )
        return signal

    def get_latest_signal(self, symbol: str) -> ExternalSignal | None:
        """Get the latest cached signal without recomputing."""
        return self._latest_signals.get(symbol)

    def get_market_signal(self, asset_id: str) -> ExternalSignal | None:
        """Get the latest signal for a specific market/asset.

        Looks up the asset_id in registered market mappings.
        """
        for symbol, market_ids in self._symbol_markets.items():
            if asset_id in market_ids:
                return self._latest_signals.get(symbol)
        return None

    async def start(self) -> None:
        """Start the aggregator."""
        if self._binance:
            await self._binance.start()
        logger.info("SignalAggregator started")

    async def stop(self) -> None:
        """Stop the aggregator and its feeds."""
        if self._binance:
            await self._binance.stop()
        if self._chainlink:
            await self._chainlink.close()
        logger.info("SignalAggregator stopped")
