import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from src.data.database import PolymarketDatabase, MarketInfo, OrderBookSnapshot, TokenInfo
from src.data.market_discovery import MarketDiscoveryManager

logger = logging.getLogger("market_tracker")


@dataclass
class TrackedMarket:
    market: MarketInfo
    tokens: list[TokenInfo] = field(default_factory=list)
    snapshots: dict[str, OrderBookSnapshot] = field(default_factory=dict)
    volume_24h: Decimal = Decimal("0")
    liquidity_score: Decimal = Decimal("0")
    last_seen: str = ""
    trade_frequency: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        outcome_prices = [str(t.price) for t in self.tokens if t.price > 0]
        return {
            "id": self.market.id,
            "question": self.market.question,
            "liquidity_score": self.liquidity_score,
            "volume": self.market.volume_num,
            "liquidity": self.market.liquidity_num,
            "outcome_prices": outcome_prices,
            "end_date": self.market.end_date,
            "category": self.market.category,
            "enable_order_book": True,
            "_original": self,
        }


class MarketTracker:
    def __init__(self, db: PolymarketDatabase, discovery: MarketDiscoveryManager) -> None:
        self._db = db
        self._discovery = discovery
        self.tracked_markets: dict[str, TrackedMarket] = {}
        self._running = False
        self._previous_prices: dict[str, Decimal] = {}

    async def run_once(self) -> list[MarketInfo]:
        logger.info("Running single tracking cycle")
        markets = await self._discovery.discover_all_active_markets()
        if not markets:
            logger.warning("No markets found in tracking cycle")
            return []

        active_ids = {m.id for m in markets}

        for mid in list(self.tracked_markets.keys()):
            if mid not in active_ids:
                logger.info("Market %s is no longer active — emitting market_closed event", mid)
                await self._db.insert_market_event(mid, "closed", "active", "closed")
                del self.tracked_markets[mid]

        for market in markets:
            existing = self.tracked_markets.get(market.id)
            if existing is None:
                logger.info("New market detected: %s", market.question[:60])
                await self._db.insert_market_event(market.id, "opened", "", "active")
                self.tracked_markets[market.id] = TrackedMarket(market=market)
                self.tracked_markets[market.id].last_seen = datetime.now(timezone.utc).isoformat()
            else:
                old_vol = existing.market.volume_num
                old_liq = existing.market.liquidity_num
                vol_change_pct = abs(market.volume_num - old_vol) / max(old_vol, Decimal("1")) * Decimal("100")
                liq_change_pct = abs(market.liquidity_num - old_liq) / max(old_liq, Decimal("1")) * Decimal("100")

                if vol_change_pct > Decimal("10"):
                    await self._db.insert_market_event(
                        market.id, "liquidity_changed",
                        str(old_vol), str(market.volume_num),
                    )

                if liq_change_pct > Decimal("10"):
                    await self._db.insert_market_event(
                        market.id, "liquidity_changed",
                        str(old_liq), str(market.liquidity_num),
                    )

                existing.market = market
                existing.last_seen = datetime.now(timezone.utc).isoformat()

        order_books = await self._discovery.fetch_order_books(markets)

        for market in markets:
            tracked = self.tracked_markets[market.id]
            token_snaps = order_books.get(market.id, {})
            tracked.snapshots = token_snaps

            for tid, snap in token_snaps.items():
                tracked.snapshots[tid] = snap

                prev = self._previous_prices.get(tid)
                mid = snap.mid_price
                if prev is not None and prev > 0:
                    change_pct = abs(mid - prev) / prev * Decimal("100")
                    if change_pct > Decimal("20"):
                        logger.info("Price spike detected for token %s: %.2f%%", tid, float(change_pct))
                        await self._db.insert_market_event(
                            market.id, "price_spike",
                            str(prev), str(mid),
                        )
                self._previous_prices[tid] = mid

            score = self._discovery.compute_liquidity_score(market, next(iter(token_snaps.values())) if token_snaps else None)
            tracked.liquidity_score = score

            from src.data.database import LiquidityMetrics
            metrics = LiquidityMetrics(
                market_id=market.id,
                volume_num=market.volume_num,
                liquidity_num=market.liquidity_num,
                spread_pct=tracked.snapshots[list(tracked.snapshots.keys())[0]].spread_pct if tracked.snapshots else Decimal("0"),
                depth_2pct=tracked.snapshots[list(tracked.snapshots.keys())[0]].depth_2pct if tracked.snapshots else Decimal("0"),
                volume_24h=tracked.volume_24h,
                trade_frequency=tracked.trade_frequency,
                liquidity_score=score,
            )
            await self._db.insert_liquidity_metrics(metrics)

        logger.info("Tracking cycle complete: %d markets tracked", len(self.tracked_markets))
        return markets

    async def run_continuous(self, interval_seconds: int = 240) -> None:
        self._running = True
        logger.info("Starting continuous tracking every %ds", interval_seconds)

        try:
            while self._running:
                cycle_start = asyncio.get_event_loop().time()
                try:
                    await self.run_once()
                except Exception as exc:
                    logger.exception("Error in tracking cycle: %s", exc)

                elapsed = asyncio.get_event_loop().time() - cycle_start
                sleep_time = max(1, interval_seconds - elapsed)
                logger.debug("Tracking cycle took %.1fs, sleeping %.1fs", elapsed, sleep_time)
                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            logger.info("Continuous tracking cancelled")
            self._running = False

    def stop(self) -> None:
        self._running = False
        logger.info("Tracking stop requested")

    def get_market_by_id(self, market_id: str) -> Optional[TrackedMarket]:
        return self.tracked_markets.get(market_id)

    def get_markets_by_category(self, category: str) -> list[TrackedMarket]:
        return [m for m in self.tracked_markets.values() if m.market.category == category]

    def get_markets_with_min_score(self, min_score: Decimal = Decimal("0.4")) -> list[TrackedMarket]:
        return [m for m in self.tracked_markets.values() if m.liquidity_score >= min_score]
