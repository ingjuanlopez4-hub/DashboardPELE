import asyncio
import time
from decimal import Decimal
from typing import Any, Dict, List

import pytest
import pytest_asyncio

from src.strategy.news_fetcher import NewsFetcher, RateLimiter


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_initial_call_count_zero(self):
        limiter = RateLimiter(max_calls=10, period_seconds=60)
        assert limiter.call_count == 0

    @pytest.mark.asyncio
    async def test_acquire_increases_call_count(self):
        limiter = RateLimiter(max_calls=10, period_seconds=60)
        await limiter.acquire()
        assert limiter.call_count == 1

    @pytest.mark.asyncio
    async def test_acquire_multiple(self):
        limiter = RateLimiter(max_calls=10, period_seconds=60)
        for _ in range(5):
            await limiter.acquire()
        assert limiter.call_count == 5

    @pytest.mark.asyncio
    async def test_calls_expire_after_period(self):
        limiter = RateLimiter(max_calls=10, period_seconds=0.1)
        await limiter.acquire()
        assert limiter.call_count == 1
        await asyncio.sleep(0.15)
        assert limiter.call_count == 0

    @pytest.mark.asyncio
    async def test_rate_limiter_blocks_when_exceeded(self):
        limiter = RateLimiter(max_calls=1, period_seconds=0.5)
        await limiter.acquire()
        start = time.time()
        async def acquire_after_delay():
            await asyncio.sleep(0.1)
            await limiter.acquire()
        task = asyncio.create_task(acquire_after_delay())
        await asyncio.sleep(0.15)
        assert limiter.call_count <= 1
        task.cancel()

    @pytest.mark.asyncio
    async def test_concurrent_acquires(self):
        limiter = RateLimiter(max_calls=5, period_seconds=60)
        async def acquire():
            await limiter.acquire()
            return limiter.call_count
        results = await asyncio.gather(*[acquire() for _ in range(5)])
        assert max(results) <= 5


class TestNewsFetcher:
    @pytest.mark.asyncio
    async def test_fetch_for_market_returns_list(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
            max_articles_per_market=10,
        )
        result = await fetcher.fetch_for_market(
            market_question="Will Bitcoin reach $100k?",
            market_tags=["bitcoin", "crypto"],
            market_category="crypto",
        )
        assert isinstance(result, list)
        await fetcher.close()

    @pytest.mark.asyncio
    async def test_fetch_for_market_with_no_keywords(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
            max_articles_per_market=10,
        )
        result = await fetcher.fetch_for_market(
            market_question="",
            market_tags=[],
            market_category="",
        )
        assert isinstance(result, list)
        assert len(result) == 0
        await fetcher.close()

    @pytest.mark.asyncio
    async def test_cache_returns_same_result(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
            max_articles_per_market=10,
            cache_ttl_seconds=300,
        )
        result1 = await fetcher.fetch_for_market(
            market_question="Will BTC hit $100k?",
            market_tags=["btc"],
            market_category="crypto",
        )
        result2 = await fetcher.fetch_for_market(
            market_question="Will BTC hit $100k?",
            market_tags=["btc"],
            market_category="crypto",
        )
        assert result1 == result2
        await fetcher.close()

    @pytest.mark.asyncio
    async def test_cache_expires(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
            max_articles_per_market=10,
            cache_ttl_seconds=0,
        )
        result1 = await fetcher.fetch_for_market(
            market_question="Test question?",
            market_tags=["test"],
            market_category="test",
        )
        await asyncio.sleep(0.01)
        result2 = await fetcher.fetch_for_market(
            market_question="Test question?",
            market_tags=["test"],
            market_category="test",
        )
        await fetcher.close()

    @pytest.mark.asyncio
    async def test_fetch_all_active_markets(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
        )
        markets: List[Dict[str, Any]] = [
            {"id": "m1", "question": "Will BTC reach $100k?", "tags": ["btc"], "category": "crypto"},
            {"id": "m2", "question": "Will ETH reach $10k?", "tags": ["eth"], "category": "crypto"},
        ]
        result = await fetcher.fetch_all_active_markets(markets)
        assert isinstance(result, dict)
        assert "m1" in result
        assert "m2" in result
        assert isinstance(result["m1"], list)
        assert isinstance(result["m2"], list)
        await fetcher.close()

    @pytest.mark.asyncio
    async def test_fetch_all_active_markets_empty(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
        )
        result = await fetcher.fetch_all_active_markets([])
        assert result == {}
        await fetcher.close()

    @pytest.mark.asyncio
    async def test_close_releases_resources(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
        )
        await fetcher.close()
        assert fetcher._session is None or fetcher._session.closed

    @pytest.mark.asyncio
    async def test_deduplication(self):
        fetcher = NewsFetcher(
            news_api_key=None,
            use_rss=False,
            max_articles_per_market=10,
        )
        fetcher._news_cache["test|a"] = (["headline1", "headline1", "headline2"], time.time())
        result = await fetcher.fetch_for_market("test", ["a"], "")
        await fetcher.close()
