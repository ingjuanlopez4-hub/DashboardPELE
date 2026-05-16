import asyncio
import logging
import math
import random
import time
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

import aiohttp

from src.data.database import (
    MarketInfo,
    OrderBookSnapshot,
    PolymarketDatabase,
    TokenInfo,
)

logger = logging.getLogger("market_discovery")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

DEFAULT_LIQUIDITY_WEIGHTS: dict[str, Decimal] = {
    "volume": Decimal("0.25"),
    "liquidity": Decimal("0.30"),
    "depth": Decimal("0.20"),
    "spread": Decimal("0.15"),
    "activity": Decimal("0.10"),
}

MIN_VOLUME = Decimal("50000")
MIN_LIQUIDITY = Decimal("25000")
MAX_SPREAD_PCT = Decimal("5.0")
MIN_DEPTH_2PCT = Decimal("5000")
MIN_DAYS_TO_RESOLUTION = 14
MIN_VOLUME_24H = Decimal("5000")
MIN_YES_PROB = Decimal("0.30")
MAX_YES_PROB = Decimal("0.70")

GAMMA_SEMAPHORE = asyncio.Semaphore(10)
CLOB_SEMAPHORE = asyncio.Semaphore(5)
MAX_RETRIES = 3
BASE_BACKOFF_S = 1.0


class MarketDiscoveryManager:
    def __init__(
        self,
        db: PolymarketDatabase,
        gamma_api_base: str = GAMMA_API_BASE,
        clob_api_base: str = CLOB_API_BASE,
        weights: Optional[dict[str, Decimal]] = None,
    ) -> None:
        self._db = db
        self._gamma_base = gamma_api_base
        self._clob_base = clob_api_base
        self._weights = weights or DEFAULT_LIQUIDITY_WEIGHTS
        self._session: Optional[aiohttp.ClientSession] = None
        self._discovered_markets: dict[str, MarketInfo] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "PolymarketBot/1.0"}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _quantize_price(price: Decimal, tick_size: Decimal) -> Decimal:
        return price.quantize(tick_size, rounding=ROUND_HALF_EVEN)

    async def _fetch_json(
        self,
        url: str,
        semaphore: asyncio.Semaphore,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        session = await self._get_session()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with semaphore:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", str(BASE_BACKOFF_S * 2 ** attempt)))
                            logger.warning("Rate limited (429) on %s — waiting %ds", url, retry_after)
                            await asyncio.sleep(retry_after)
                            continue
                        resp.raise_for_status()
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Request failed (attempt %d/%d) for %s: %s", attempt, MAX_RETRIES, url, exc)
                if attempt < MAX_RETRIES:
                    backoff = BASE_BACKOFF_S * 2 ** attempt + random.uniform(0, 1)
                    await asyncio.sleep(backoff)
                else:
                    logger.error("All retries exhausted for %s", url)
                    raise
        return None

    async def discover_all_active_markets(self) -> list[MarketInfo]:
        logger.info("Starting full market discovery via Gamma API")
        all_markets: list[MarketInfo] = []
        offset = 0
        limit = 100

        while True:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
                "limit": str(limit),
                "offset": str(offset),
            }
            try:
                data = await self._fetch_json(
                    f"{self._gamma_base}/markets",
                    GAMMA_SEMAPHORE,
                    params=params,
                )
            except Exception as exc:
                logger.error("Discovery failed at offset %d: %s", offset, exc)
                break

            if not data or not isinstance(data, list) or len(data) == 0:
                logger.info("No more markets at offset %d — stopping pagination", offset)
                break

            for item in data:
                try:
                    market = MarketInfo.from_api(item)
                    all_markets.append(market)
                    self._discovered_markets[market.id] = market
                except Exception as exc:
                    logger.warning("Failed to parse market at offset %d: %s", offset, exc)
                    continue

            logger.debug("Discovered %d markets at offset %d", len(data), offset)
            offset += limit

            await asyncio.sleep(0.1)

        logger.info("Discovery complete: %d active markets found", len(all_markets))
        await self._db.upsert_markets_bulk(all_markets)
        return all_markets

    async def fetch_order_books(
        self, markets: list[MarketInfo]
    ) -> dict[str, dict[str, OrderBookSnapshot]]:
        logger.info("Fetching order books for %d markets", len(markets))
        result: dict[str, dict[str, OrderBookSnapshot]] = {}

        tasks = []
        skipped = 0
        for market in markets:
            for token in market.tokens:
                tid = token.token_id.strip() if token.token_id else ""
                if not tid:
                    skipped += 1
                    continue
                tasks.append(self._fetch_single_order_book(market.id, tid))

        snapshots = await asyncio.gather(*tasks, return_exceptions=True)

        for snap in snapshots:
            if isinstance(snap, OrderBookSnapshot):
                if snap.market_id not in result:
                    result[snap.market_id] = {}
                result[snap.market_id][snap.token_id] = snap
                await self._db.insert_orderbook_snapshot(snap)

        logger.info("Fetched order books for %d markets", len(result))
        return result

    async def _fetch_single_order_book(
        self, market_id: str, token_id: str
    ) -> OrderBookSnapshot:
        url = f"{self._clob_base}/book"
        params = {"token_id": token_id}
        try:
            data = await self._fetch_json(url, CLOB_SEMAPHORE, params=params)
        except Exception as exc:
            logger.warning("Failed to fetch order book for token %s: %s", token_id, exc)
            return OrderBookSnapshot(market_id=market_id, token_id=token_id)

        return self._parse_order_book(data, market_id, token_id)

    @staticmethod
    def _parse_order_book(
        data: Any, market_id: str, token_id: str
    ) -> OrderBookSnapshot:
        snap = OrderBookSnapshot(market_id=market_id, token_id=token_id)

        if not data or not isinstance(data, dict):
            return snap

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        best_bid = Decimal("0")
        best_ask = Decimal("0")
        bid_depth_5 = Decimal("0")
        ask_depth_5 = Decimal("0")
        total_bid_volume_2pct = Decimal("0")
        total_ask_volume_2pct = Decimal("0")

        for level in bids[:5]:
            try:
                p = Decimal(str(level.get("price", "0")))
                s = Decimal(str(level.get("size", "0")))
                if p > best_bid:
                    best_bid = p
                bid_depth_5 += p * s
            except Exception:
                continue

        for level in asks[:5]:
            try:
                p = Decimal(str(level.get("price", "0")))
                s = Decimal(str(level.get("size", "0")))
                if best_ask == Decimal("0") or p < best_ask:
                    best_ask = p
                ask_depth_5 += p * s
            except Exception:
                continue

        mid_price = (best_bid + best_ask) / Decimal("2") if best_bid > 0 and best_ask > 0 else Decimal("0")

        spread = best_ask - best_bid
        spread_pct = (spread / mid_price * Decimal("100")) if mid_price > 0 else Decimal("999")

        depth_lower = mid_price * Decimal("0.98") if mid_price > 0 else Decimal("0")
        depth_upper = mid_price * Decimal("1.02") if mid_price > 0 else Decimal("0")

        for level in bids:
            try:
                p = Decimal(str(level.get("price", "0")))
                s = Decimal(str(level.get("size", "0")))
                if depth_lower <= p <= mid_price:
                    total_bid_volume_2pct += p * s
            except Exception:
                continue

        for level in asks:
            try:
                p = Decimal(str(level.get("price", "0")))
                s = Decimal(str(level.get("size", "0")))
                if mid_price <= p <= depth_upper:
                    total_ask_volume_2pct += p * s
            except Exception:
                continue

        snap.best_bid = best_bid
        snap.best_ask = best_ask
        snap.mid_price = mid_price
        snap.spread_pct = spread_pct.quantize(Decimal("0.01"))
        snap.depth_2pct = (total_bid_volume_2pct + total_ask_volume_2pct).quantize(Decimal("0.01"))
        snap.bid_depth_5 = bid_depth_5.quantize(Decimal("0.01"))
        snap.ask_depth_5 = ask_depth_5.quantize(Decimal("0.01"))

        return snap

    def compute_liquidity_score(
        self,
        market: MarketInfo,
        snap: Optional[OrderBookSnapshot] = None,
        volume_24h: Optional[Decimal] = None,
    ) -> Decimal:
        w = self._weights
        vol = market.volume_num
        liq = market.liquidity_num
        spread = snap.spread_pct if snap else Decimal("999")
        depth = snap.depth_2pct if snap else Decimal("0")
        v24h = volume_24h or Decimal("0")

        vol_score = min(Decimal("1.0"), vol / Decimal("500000"))
        liq_score = min(Decimal("1.0"), liq / Decimal("250000"))
        depth_score = min(Decimal("1.0"), depth / Decimal("25000"))
        spread_score = max(Decimal("0"), Decimal("1.0") - spread / Decimal("10.0"))
        act_score = min(Decimal("1.0"), v24h / Decimal("25000"))

        total_weight = w["volume"] + w["liquidity"] + w["depth"] + w["spread"] + w["activity"]
        if total_weight == Decimal("0"):
            return Decimal("0")

        score = (
            w["volume"] * vol_score
            + w["liquidity"] * liq_score
            + w["depth"] * depth_score
            + w["spread"] * spread_score
            + w["activity"] * act_score
        ) / total_weight

        result = score.quantize(Decimal("0.0001"))
        logger.log(
            logging.DEBUG if result >= Decimal("0.05") else logging.DEBUG,
            "Score=%s for %s (vol=%s liq=%s spread=%s depth=%s v24h=%s)",
            result, market.question[:40] if market.question else "?", vol, liq, spread, depth, v24h,
        )
        return result

    def meets_thresholds(
        self,
        market: MarketInfo,
        snap: Optional[OrderBookSnapshot] = None,
        volume_24h: Optional[Decimal] = None,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []

        if market.volume_num < MIN_VOLUME:
            reasons.append(f"volume {market.volume_num} < {MIN_VOLUME}")

        if market.liquidity_num < MIN_LIQUIDITY:
            reasons.append(f"liquidity {market.liquidity_num} < {MIN_LIQUIDITY}")

        if snap is not None:
            if snap.spread_pct > MAX_SPREAD_PCT:
                reasons.append(f"spread {snap.spread_pct}% > {MAX_SPREAD_PCT}%")
            if snap.depth_2pct < MIN_DEPTH_2PCT:
                reasons.append(f"depth_2pct {snap.depth_2pct} < {MIN_DEPTH_2PCT}")

        if volume_24h is not None and volume_24h < MIN_VOLUME_24H:
            reasons.append(f"volume_24h {volume_24h} < {MIN_VOLUME_24H}")

        yes_price = Decimal("0.5")
        for token in (market.tokens or []):
            if token.outcome.upper() == "YES" and token.price > 0:
                yes_price = token.price
                break

        if yes_price < MIN_YES_PROB or yes_price > MAX_YES_PROB:
            reasons.append(f"YES price {yes_price} outside [{MIN_YES_PROB}, {MAX_YES_PROB}]")

        return len(reasons) == 0, reasons

    async def run_full_discovery(self) -> list[MarketInfo]:
        markets = await self.discover_all_active_markets()
        if not markets:
            logger.warning("No markets discovered")
            return []

        order_books = await self.fetch_order_books(markets)

        for market in markets:
            token_snaps = order_books.get(market.id, {})
            snap = next(iter(token_snaps.values())) if token_snaps else None
            score = self.compute_liquidity_score(market, snap)
            meets, reasons = self.meets_thresholds(market, snap)

            from src.data.database import LiquidityMetrics
            metrics = LiquidityMetrics(
                market_id=market.id,
                volume_num=market.volume_num,
                liquidity_num=market.liquidity_num,
                spread_pct=snap.spread_pct if snap else Decimal("0"),
                depth_2pct=snap.depth_2pct if snap else Decimal("0"),
                volume_24h=Decimal("0"),
                liquidity_score=score,
            )
            await self._db.insert_liquidity_metrics(metrics)

            if not meets:
                logger.debug("Market %s rejected: %s", market.question[:50] if market.question else "?", "; ".join(reasons))

        return markets
