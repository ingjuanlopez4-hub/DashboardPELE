import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from src.strategy.finbert_config import FINBERT_CONFIG
from src.strategy.finbert_utils import extract_keywords

logger = logging.getLogger(__name__)

NEWSAPI_TIMEOUT_S = 10
RSS_TIMEOUT_S = 10
TOTAL_TIMEOUT_S = 30


class NewsFetchError(Exception):
    pass


class RateLimitExceededError(NewsFetchError):
    pass


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self._max_calls = max_calls
        self._period = period_seconds
        self._calls: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            self._calls = [t for t in self._calls if now - t < self._period]
            if len(self._calls) >= self._max_calls:
                wait_time = self._calls[0] + self._period - now
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    return await self.acquire()
            self._calls.append(time.time())

    @property
    def call_count(self) -> int:
        now = time.time()
        return sum(1 for t in self._calls if now - t < self._period)


class NewsFetcher:
    def __init__(
        self,
        news_api_key: Optional[str] = None,
        use_rss: bool = True,
        rss_feeds: Optional[List[str]] = None,
        max_articles_per_market: int = 10,
        cache_ttl_seconds: int = 300,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.news_api_key = news_api_key or FINBERT_CONFIG.get("news_api_key")
        self.use_rss = use_rss
        self.rss_feeds = rss_feeds or FINBERT_CONFIG.get("rss_feeds", [])
        self.max_articles_per_market = max_articles_per_market
        self.cache_ttl_seconds = cache_ttl_seconds

        self._news_cache: Dict[str, tuple[List[str], float]] = {}
        self._newsapi_limiter = RateLimiter(
            max_calls=FINBERT_CONFIG.get("news_api_rate_limit", 100),
            period_seconds=86400,
        )
        self._rss_limiter = RateLimiter(
            max_calls=FINBERT_CONFIG.get("rss_rate_limit", 60),
            period_seconds=60,
        )
        self._session: Optional[aiohttp.ClientSession] = session
        self._own_session = False

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._own_session = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "PELE-Bot/1.0"}
            )
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
            self._own_session = False

    async def fetch_for_market(
        self,
        market_question: str,
        market_tags: List[str],
        market_category: str,
    ) -> List[str]:
        cache_key = f"{market_question}|{','.join(sorted(market_tags))}"
        now = time.time()
        cached = self._news_cache.get(cache_key)
        if cached and (now - cached[1]) < self.cache_ttl_seconds:
            logger.debug("News cache hit for market: %s", market_question[:60])
            return cached[0]

        keywords = extract_keywords(market_question, market_tags)
        if not keywords:
            keywords = market_tags[:3] if market_tags else ["finance"]

        all_articles: List[str] = []
        errors: List[str] = []

        async def fetch_newsapi_with_timeout() -> List[str]:
            return await self._fetch_from_newsapi(keywords)

        async def fetch_rss_with_timeout() -> List[str]:
            return await self._fetch_from_rss(keywords)

        tasks = []
        if self.news_api_key:
            tasks.append(fetch_newsapi_with_timeout())
        if self.use_rss:
            tasks.append(fetch_rss_with_timeout())

        if tasks:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=TOTAL_TIMEOUT_S,
                )
                for res in results:
                    if isinstance(res, Exception):
                        errors.append(str(res))
                    elif isinstance(res, list):
                        all_articles.extend(res)
            except asyncio.TimeoutError:
                logger.warning("Total timeout (%ds) exceeded fetching news", TOTAL_TIMEOUT_S)

        if not all_articles:
            logger.debug(
                "No news found for market: %s (keywords=%s, errors=%s)",
                market_question[:60], keywords, errors,
            )

        deduplicated = list(dict.fromkeys(all_articles))[:self.max_articles_per_market]
        self._news_cache[cache_key] = (deduplicated, time.time())
        return deduplicated

    async def _fetch_from_newsapi(self, keywords: List[str]) -> List[str]:
        if not self.news_api_key:
            return []

        await self._newsapi_limiter.acquire()
        session = await self._ensure_session()
        query = " OR ".join(keywords)
        url = "https://newsapi.org/v2/everything"
        params: Dict[str, Any] = {
            "q": query,
            "language": "en",
            "pageSize": self.max_articles_per_market,
            "apiKey": self.news_api_key,
        }

        for attempt in range(3):
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=NEWSAPI_TIMEOUT_S)) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        logger.warning("NewsAPI 429, retrying after %ds (attempt %d/3)", retry_after, attempt + 1)
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status != 200:
                        logger.warning("NewsAPI returned %d (attempt %d/3)", resp.status, attempt + 1)
                        await asyncio.sleep(2 ** attempt)
                        continue
                    data = await resp.json()
                    articles = data.get("articles", [])
                    return [
                        f"{a.get('title', '')}. {a.get('description', '')}".strip()
                        for a in articles
                        if a.get("title")
                    ]
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning("NewsAPI network error (attempt %d/3): %s", attempt + 1, e)
                await asyncio.sleep(2 ** attempt)

        return []

    async def _fetch_from_rss(self, keywords: List[str]) -> List[str]:
        await self._rss_limiter.acquire()
        session = await self._ensure_session()
        articles: List[str] = []

        for feed_url in self.rss_feeds:
            try:
                async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=RSS_TIMEOUT_S)) as resp:
                    if resp.status != 200:
                        continue
                    import xml.etree.ElementTree as ET
                    text = await resp.text()
                    root = ET.fromstring(text)
                    for item in root.iter("item"):
                        title = item.findtext("title", "")
                        desc = item.findtext("description", "")
                        combined = f"{title}. {desc}".strip()
                        if any(kw.lower() in combined.lower() for kw in keywords):
                            if combined:
                                articles.append(combined)
                    logger.debug("RSS feed %s returned %d matching articles", feed_url, len(articles))
            except Exception as e:
                logger.debug("RSS fetch error for %s: %s", feed_url, e)
                continue

        return articles[:self.max_articles_per_market]

    async def fetch_all_active_markets(
        self, markets: List[Dict[str, Any]]
    ) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        tasks = []
        for market in markets:
            tasks.append(
                self.fetch_for_market(
                    market_question=market.get("question", ""),
                    market_tags=market.get("tags", []),
                    market_category=market.get("category", ""),
                )
            )

        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, market in enumerate(markets):
            mid = market.get("id", str(i))
            if isinstance(all_results[i], Exception):
                logger.error("Failed to fetch news for market %s: %s", mid, all_results[i])
                result[mid] = []
            else:
                result[mid] = all_results[i]

        return result
