"""
On-chain Data Signals — Blockchain data feeds for enhanced signal generation.

Fetches and analyzes on-chain data from Polygon (and optionally Ethereum)
to generate trading signals based on:
  - Large token transfers (whale movements)
  - Smart contract interactions
  - Gas price spikes (network congestion)
  - DEX liquidity changes
  - Mempool analysis for pending transactions

These signals complement the existing FinBERT/MC/Wick signals for markets
where on-chain activity is predictive of price direction.
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class OnChainSignal:
    direction: str
    confidence: Decimal
    source: str
    details: dict[str, Any]
    timestamp: float


WHALE_THRESHOLD_USDC = Decimal("100000")
LARGE_TX_THRESHOLD_USDC = Decimal("50000")
GAS_SPIKE_THRESHOLD_GWEI = Decimal("150")
DEFAULT_POLYGON_RPC = "https://polygon-rpc.com"

ERC20_TRANSFER_ABI = json.loads(
    '[{"anonymous":false,"inputs":[{"indexed":true,"name":"from","type":"address"},'
    '{"indexed":true,"name":"to","type":"address"},'
    '{"indexed":false,"name":"value","type":"uint256"}],'
    '"name":"Transfer","type":"event"}]'
)


class MempoolWatcher:
    def __init__(
        self,
        rpc_url: str = DEFAULT_POLYGON_RPC,
        poll_interval_s: float = 5.0,
        max_pending_txns: int = 1000,
    ) -> None:
        self._rpc_url = rpc_url
        self._poll_interval = poll_interval_s
        self._w3: Any = None
        self._pending_txns: deque[dict[str, Any]] = deque(maxlen=max_pending_txns)
        self._running = False
        self._latest_gas_prices: deque[int] = deque(maxlen=20)
        self._whale_alerts: list[dict[str, Any]] = []

    async def _ensure_web3(self) -> Any:
        if self._w3 is None:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            logger.info("MempoolWatcher connected to %s", self._rpc_url[:40])
        return self._w3

    async def get_gas_price_gwei(self) -> int:
        try:
            w3 = await self._ensure_web3()
            price_wei = await asyncio.to_thread(w3.eth.gas_price)
            from web3 import Web3
            gwei = int(Web3.from_wei(price_wei, "gwei"))
            self._latest_gas_prices.append(gwei)
            return gwei
        except Exception:
            logger.exception("Failed to fetch gas price")
            return 0

    async def get_block_number(self) -> int:
        try:
            w3 = await self._ensure_web3()
            return await asyncio.to_thread(w3.eth.block_number)
        except Exception:
            return 0

    async def get_recent_transactions(
        self,
        block_count: int = 5,
    ) -> list[dict[str, Any]]:
        """Fetch recent blocks and extract transactions of interest."""
        try:
            w3 = await self._ensure_web3()
            latest = await asyncio.to_thread(w3.eth.block_number)
            txns: list[dict[str, Any]] = []

            for i in range(max(0, latest - block_count), latest + 1):
                block = await asyncio.to_thread(
                    w3.eth.get_block, i, full_transactions=True,
                )
                for tx in block.get("transactions", []):
                    tx_dict = dict(tx)
                    if tx_dict.get("value", 0) > 0:
                        from web3 import Web3
                        value_eth = Web3.from_wei(tx_dict["value"], "ether")
                        if float(value_eth) > 10:
                            txns.append({
                                "hash": tx_dict.get("hash", "").hex() if hasattr(tx_dict.get("hash"), "hex") else str(tx_dict.get("hash", "")),
                                "from": str(tx_dict.get("from", "")),
                                "to": str(tx_dict.get("to", "")),
                                "value_eth": float(value_eth),
                                "block": i,
                                "gas_price_gwei": float(Web3.from_wei(tx_dict.get("gasPrice", 0), "gwei")),
                            })

            return txns
        except Exception:
            logger.exception("Failed to fetch recent transactions")
            return []

    async def poll_gas_and_blocks(self) -> None:
        """Background task to poll for gas prices and blocks."""
        self._running = True
        while self._running:
            try:
                gas = await self.get_gas_price_gwei()
                if gas > 0:
                    logger.debug("Polygon gas price: %d Gwei", gas)
            except Exception:
                pass
            await asyncio.sleep(self._poll_interval)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self.poll_gas_and_blocks())
        logger.info("MempoolWatcher started")

    async def stop(self) -> None:
        self._running = False
        if hasattr(self, "_task") and self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MempoolWatcher stopped")


class OnChainSignalAggregator:
    def __init__(
        self,
        mempool_watcher: MempoolWatcher | None = None,
        whale_threshold: Decimal = WHALE_THRESHOLD_USDC,
        gas_spike_threshold: Decimal = GAS_SPIKE_THRESHOLD_GWEI,
        min_confidence: Decimal = Decimal("0.3"),
    ) -> None:
        self._mempool = mempool_watcher or MempoolWatcher()
        self._whale_threshold = whale_threshold
        self._gas_spike_threshold = gas_spike_threshold
        self._min_confidence = min_confidence

        self._latest_signals: dict[str, OnChainSignal] = {}
        self._cache_ttl_s = 10.0
        self._last_signal_time: dict[str, float] = {}

    async def get_gas_signal(self) -> OnChainSignal | None:
        gas_gwei = await self._mempool.get_gas_price_gwei()
        if gas_gwei <= 0:
            return None

        if gas_gwei >= float(self._gas_spike_threshold * Decimal("2")):
            confidence = Decimal("0.8")
            direction = "DOWN"
            detail = "extreme_gas_spike"
        elif gas_gwei >= float(self._gas_spike_threshold):
            confidence = Decimal("0.5")
            direction = "DOWN"
            detail = "gas_spike"
        elif gas_gwei <= 20:
            confidence = Decimal("0.3")
            direction = "UP"
            detail = "low_gas"
        else:
            return None

        return OnChainSignal(
            direction=direction,
            confidence=confidence,
            source="gas",
            details={"gas_price_gwei": gas_gwei, "signal": detail},
            timestamp=time.time(),
        )

    async def get_whale_signal(self) -> OnChainSignal | None:
        txns = await self._mempool.get_recent_transactions(block_count=3)
        large_txns = [
            t for t in txns
            if t.get("value_eth", 0) * 2000 >= float(self._whale_threshold)
        ]

        if not large_txns:
            return None

        total_volume = sum(t["value_eth"] for t in large_txns) * 2000
        avg_gas = sum(t["gas_price_gwei"] for t in large_txns) / len(large_txns)

        if total_volume >= float(self._whale_threshold * Decimal("5")):
            confidence = Decimal("0.7")
            direction = "UP" if avg_gas < 100 else "DOWN"
        elif total_volume >= float(self._whale_threshold):
            confidence = Decimal("0.4")
            direction = "NEUTRAL"
        else:
            return None

        return OnChainSignal(
            direction=direction,
            confidence=confidence,
            source="whale",
            details={
                "large_txns": len(large_txns),
                "total_volume_usdc": round(total_volume, 2),
                "avg_gas_gwei": round(avg_gas, 1),
            },
            timestamp=time.time(),
        )

    async def get_aggregated_signal(self) -> OnChainSignal | None:
        signals: list[OnChainSignal] = []

        gas_sig = await self.get_gas_signal()
        if gas_sig and gas_sig.confidence >= self._min_confidence:
            signals.append(gas_sig)

        whale_sig = await self.get_whale_signal()
        if whale_sig and whale_sig.confidence >= self._min_confidence:
            signals.append(whale_sig)

        if not signals:
            return None

        if len(signals) == 1:
            return signals[0]

        up_votes = sum(1 for s in signals if s.direction == "UP")
        down_votes = sum(1 for s in signals if s.direction == "DOWN")
        total_confidence = sum(s.confidence for s in signals) / len(signals)

        if up_votes > down_votes:
            direction = "UP"
        elif down_votes > up_votes:
            direction = "DOWN"
        else:
            direction = "NEUTRAL"

        return OnChainSignal(
            direction=direction,
            confidence=total_confidence.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            source="onchain_aggregated",
            details={
                "num_signals": len(signals),
                "signals": [
                    {"source": s.source, "direction": s.direction, "confidence": float(s.confidence)}
                    for s in signals
                ],
            },
            timestamp=time.time(),
        )

    def map_to_trading_signal(
        self, onchain_signal: OnChainSignal | None
    ) -> dict[str, Any] | None:
        if onchain_signal is None:
            return None

        if onchain_signal.confidence < self._min_confidence:
            return None

        direction_map = {
            "UP": "BUY_YES",
            "DOWN": "BUY_NO",
            "NEUTRAL": "NONE",
        }

        trade_dir = direction_map.get(onchain_signal.direction, "NONE")
        if trade_dir == "NONE":
            return None

        return {
            "source": "onchain",
            "direction": trade_dir,
            "confidence": onchain_signal.confidence,
            "edge": onchain_signal.confidence * Decimal("0.05"),
            "details": onchain_signal.details,
        }

    async def start(self) -> None:
        await self._mempool.start()
        logger.info("OnChainSignalAggregator started")

    async def stop(self) -> None:
        await self._mempool.stop()
        logger.info("OnChainSignalAggregator stopped")
