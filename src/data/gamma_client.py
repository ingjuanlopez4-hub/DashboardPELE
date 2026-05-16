import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("gamma_client")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = "/markets"
MAX_CONCURRENT = 5
PAGE_LIMIT = 100
TIMEOUT_SECONDS = 30


class GammaClientError(Exception):
    pass


class RateLimitError(GammaClientError):
    pass


class GammaClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = GAMMA_API_BASE,
        max_concurrent: int = MAX_CONCURRENT,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _fetch_page(
        self, offset: int, limit: int = PAGE_LIMIT
    ) -> list[dict[str, Any]]:
        url = f"{self._base_url}{MARKETS_ENDPOINT}"
        params: dict[str, str | int] = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }

        async with self._semaphore:
            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
            ) as response:
                if response.status == 429:
                    raise RateLimitError(f"Rate limited at offset={offset}")
                if response.status != 200:
                    text = await response.text()
                    raise GammaClientError(
                        f"HTTP {response.status} at offset={offset}: {text[:200]}"
                    )
                data = await response.json()
                if not isinstance(data, list):
                    raise GammaClientError(
                        f"Unexpected response type: {type(data).__name__}"
                    )
                return data

    async def discover_all_active_markets(self) -> list[dict[str, Any]]:
        all_markets: list[dict[str, Any]] = []
        offset = 0
        page = 0

        while True:
            try:
                markets = await self._fetch_page(offset)
            except GammaClientError:
                logger.exception("Failed to fetch page at offset %d", offset)
                break

            if not markets:
                logger.info("Empty page at offset %d — pagination complete", offset)
                break

            all_markets.extend(markets)
            offset += PAGE_LIMIT
            page += 1
            logger.info(
                "Fetched page %d (%d markets, total: %d)",
                page, len(markets), len(all_markets),
            )

            if len(markets) < PAGE_LIMIT:
                logger.info("Partial page — pagination complete")
                break

        logger.info("Discovery complete: %d active markets found", len(all_markets))
        return all_markets

    @staticmethod
    def parse_market_basic(data: dict[str, Any]) -> dict[str, Any]:
        outcomes = data.get("outcomes", ["Yes", "No"])
        outcome_prices = data.get("outcomePrices", [0.5, 0.5])
        clob_token_ids = data.get("clobTokenIds", [])

        end_date_str = data.get("endDate", "")
        days_left = 0
        if end_date_str:
            try:
                end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_left = (end - datetime.now(timezone.utc)).days
            except (ValueError, TypeError):
                pass

        return {
            "id": str(data.get("conditionId", data.get("id", ""))),
            "condition_id": str(data.get("conditionId", data.get("id", ""))),
            "question": str(data.get("question", "")),
            "slug": str(data.get("slug", "")),
            "url": str(data.get("url", "")),
            "category": str(data.get("category", "")),
            "tags": data.get("tags", []),
            "volume": Decimal(str(data.get("volume", "0"))),
            "liquidity": Decimal(str(data.get("liquidity", "0"))),
            "active": bool(data.get("active", True)),
            "closed": bool(data.get("closed", False)),
            "archived": bool(data.get("archived", False)),
            "enable_order_book": bool(data.get("enableOrderBook", True)),
            "tick_size": Decimal(str(data.get("tickSize", "0.01"))),
            "neg_risk": bool(data.get("negRisk", False)),
            "end_date": end_date_str,
            "days_left": days_left,
            "outcomes": outcomes,
            "outcome_prices": [Decimal(str(p)) for p in outcome_prices],
            "clob_token_ids": [str(t) for t in clob_token_ids],
            "created_at": str(data.get("createdAt", "")),
            "updated_at": str(data.get("updatedAt", "")),
            "events": data.get("events", []),
        }
