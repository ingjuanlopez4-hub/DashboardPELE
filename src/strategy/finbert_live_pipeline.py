"""
FinBERT Live Pipeline — Real-time NLP sentiment with category-aware fallbacks.

The live pipeline replaces simulated category baselines with real news fetching
and FinBERT inference. Falls back gracefully through multiple tiers:
  Tier 1: ONNX FinBERT on fetched news (fastest, lowest cost)
  Tier 2: PyTorch FinBERT on fetched news
  Tier 3: Category-aware keyword baseline (no model loaded)
  Tier 4: Neutral (no news available)
"""

import asyncio
import logging
import time
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from src.strategy.finbert_sentiment import FinBERTSentimentAnalyzer
from src.strategy.finbert_config import FINBERT_CONFIG
from src.strategy.news_fetcher import NewsFetcher

logger = logging.getLogger(__name__)

CATEGORY_BASELINES: dict[str, dict[str, Decimal]] = {
    "crypto": {
        "positive_shift": Decimal("0.08"),
        "negative_shift": Decimal("-0.10"),
        "base_prob": Decimal("0.50"),
    },
    "politics": {
        "positive_shift": Decimal("0.05"),
        "negative_shift": Decimal("-0.06"),
        "base_prob": Decimal("0.50"),
    },
    "sports": {
        "positive_shift": Decimal("0.03"),
        "negative_shift": Decimal("-0.04"),
        "base_prob": Decimal("0.50"),
    },
    "default": {
        "positive_shift": Decimal("0.03"),
        "negative_shift": Decimal("-0.03"),
        "base_prob": Decimal("0.50"),
    },
}


class LiveSentimentPipeline:
    def __init__(
        self,
        news_fetcher: NewsFetcher | None = None,
        sentiment_analyzer: FinBERTSentimentAnalyzer | None = None,
        category_baselines: dict[str, dict[str, Decimal]] | None = None,
        min_articles_for_real_sentiment: int = 1,
        update_interval_seconds: int = 300,
    ) -> None:
        self._news_fetcher = news_fetcher
        self._sentiment_analyzer = sentiment_analyzer
        self._category_baselines = category_baselines or CATEGORY_BASELINES
        self._min_articles = min_articles_for_real_sentiment
        self._update_interval = update_interval_seconds

        self._cache: dict[str, tuple[dict[str, Any] | None, float]] = {}
        self._keyword_sentiment_cache: dict[str, Decimal] = {}

        self._positive_keywords = [
            "bull", "surge", "gain", "rally", "upgrade", "positive", "growth",
            "breakthrough", "approval", "partnership", "adoption", "launch",
            "beat", "profit", "success", "momentum", "outperform",
        ]
        self._negative_keywords = [
            "bear", "crash", "loss", "decline", "drop", "negative", "downgrade",
            "ban", "restrict", "fraud", "hack", "breach", "lawsuit", "fine",
            "recession", "inflation", "default", "bankrupt", "sell-off",
        ]

    async def compute_sentiment(
        self,
        market_id: str,
        market_question: str,
        category: str,
        tags: list[str],
        news_texts: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        now = time.time()
        if not force_refresh:
            cached = self._cache.get(market_id)
            if cached and (now - cached[1]) < self._update_interval:
                return cached[0]

        if news_texts is None:
            news_texts = await self._fetch_news(market_question, tags)

        result = await self._analyze_with_fallbacks(
            market_id=market_id,
            market_question=market_question,
            category=category,
            news_texts=news_texts,
        )

        self._cache[market_id] = (result, now)
        return result

    async def _fetch_news(
        self,
        market_question: str,
        tags: list[str],
    ) -> list[str]:
        if self._news_fetcher is None:
            logger.debug("No NewsFetcher configured — no news available")
            return []

        try:
            texts = await self._news_fetcher.fetch_for_market(
                market_question=market_question,
                market_tags=tags,
                market_category="",
            )
            return texts or []
        except Exception as e:
            logger.warning("News fetch failed: %s", e)
            return []

    async def _analyze_with_fallbacks(
        self,
        market_id: str,
        market_question: str,
        category: str,
        news_texts: list[str],
    ) -> dict[str, Any] | None:
        result = None

        if self._sentiment_analyzer and self._sentiment_analyzer.sentiment_available:
            result = await self._try_real_finbert(market_id, market_question, news_texts)
            if result is not None:
                return result

        result = self._try_keyword_sentiment(news_texts)
        if result is not None:
            return result

        result = self._category_baseline_sentiment(category)
        if result is not None:
            return result

        return None

    async def _try_real_finbert(
        self,
        market_id: str,
        market_question: str,
        news_texts: list[str],
    ) -> dict[str, Any] | None:
        if not news_texts:
            return None

        if not self._sentiment_analyzer:
            return None

        try:
            result = await self._sentiment_analyzer.compute_sentiment_signal(
                market={"id": market_id, "question": market_question},
                current_price=Decimal("0.5"),
                news_texts=news_texts,
                market_type="long_term",
                market_id=market_id,
            )
            if result:
                result["tier"] = "live_finbert"
                return result
        except Exception as e:
            logger.warning("Real FinBERT analysis failed: %s", e)

        return None

    def _try_keyword_sentiment(
        self,
        news_texts: list[str],
    ) -> dict[str, Any] | None:
        if not news_texts:
            return None

        positive_count = 0
        negative_count = 0
        total_keyword_hits = 0

        for text in news_texts:
            text_lower = text.lower()
            for kw in self._positive_keywords:
                if kw in text_lower:
                    positive_count += 1
                    total_keyword_hits += 1
            for kw in self._negative_keywords:
                if kw in text_lower:
                    negative_count += 1
                    total_keyword_hits += 1

        if total_keyword_hits == 0:
            return None

        net_score = (positive_count - negative_count) / max(total_keyword_hits, 1)
        confidence = Decimal(str(min(total_keyword_hits / 10, 1.0)))
        implied_prob = Decimal("0.5") + Decimal(str(net_score)) * Decimal("0.15")
        implied_prob = min(max(implied_prob, Decimal("0.1")), Decimal("0.9"))

        return {
            "source": "sentiment",
            "direction": "BUY_YES" if implied_prob > Decimal("0.5") else "BUY_NO",
            "implied_probability": implied_prob,
            "edge": abs(implied_prob - Decimal("0.5")),
            "confidence": confidence.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "num_articles": len(news_texts),
            "tier": "keyword_baseline",
            "articles": [(t[:80], "keyword", float(confidence)) for t in news_texts[:3]],
        }

    def _category_baseline_sentiment(
        self,
        category: str,
    ) -> dict[str, Any] | None:
        baseline = self._category_baselines.get(
            category.lower(), self._category_baselines["default"]
        )

        implied_prob = baseline["base_prob"]
        edge = Decimal("0")

        return {
            "source": "sentiment",
            "direction": "NEUTRAL",
            "implied_probability": implied_prob,
            "edge": edge,
            "confidence": Decimal("0.2"),
            "num_articles": 0,
            "tier": "category_baseline",
            "category": category,
        }

    def get_cache_stats(self) -> dict[str, Any]:
        now = time.time()
        active = sum(1 for _, ts in self._cache.values() if (now - ts) < self._update_interval)
        return {
            "cached_markets": len(self._cache),
            "active_markets": active,
            "update_interval_seconds": self._update_interval,
            "news_fetcher_configured": self._news_fetcher is not None,
            "sentiment_analyzer_available": (
                self._sentiment_analyzer.sentiment_available
                if self._sentiment_analyzer else False
            ),
        }
