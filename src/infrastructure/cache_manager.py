"""
Cache Manager — Multi-level cache with TTL for trading signals, prices,
order books, and sentiment analysis results.

Architecture:
  Level 1 (L1): Ultra-fast, small, 100ms TTL — for signal/price caches.
  Level 2 (L2): Medium, larger, 5s TTL — for order book snapshots.
  Sentiment Cache: Large, 600s TTL — for FinBERT sentiment results.

All caches use cachetools.TTLCache internally. Cache misses automatically
fall through to the next level.
"""

import logging
import time
from typing import Any, Callable, Optional, TypeVar

from cachetools import TTLCache

from src.config.optimization_settings import LOCAL_OPTIMIZATION_CONFIG

logger = logging.getLogger("cache_manager")

T = TypeVar("T")

_SIGNAL_CACHE: Optional[TTLCache] = None
_SENTIMENT_CACHE: Optional[TTLCache] = None
_BOOK_CACHE: Optional[TTLCache] = None
_PRICE_CACHE: Optional[TTLCache] = None


def _get_cache_maxsize(name: str, default: int) -> int:
    """Get maxsize from optimization config for a given cache."""
    try:
        return LOCAL_OPTIMIZATION_CONFIG["cache"].get(f"maxsize_{name}", default)
    except (KeyError, AttributeError):
        return default


def _get_cache_ttl(name: str, default: float) -> float:
    """Get TTL in seconds from optimization config for a given cache."""
    try:
        ttl_ms = LOCAL_OPTIMIZATION_CONFIG["cache"].get(f"{name}_ttl_ms")
        if ttl_ms is not None:
            return ttl_ms / 1000.0
        return LOCAL_OPTIMIZATION_CONFIG["cache"].get(f"{name}_ttl_seconds", default)
    except (KeyError, AttributeError):
        return default


def get_signal_cache() -> TTLCache:
    """Get or create the L1 signal cache (100ms TTL, 100 entries).

    For caching computed signals, price deltas, and trading decisions
    that become stale very quickly.
    """
    global _SIGNAL_CACHE
    if _SIGNAL_CACHE is None:
        ttl = _get_cache_ttl("l1", 0.1)
        maxsize = _get_cache_maxsize("l1", 100)
        _SIGNAL_CACHE = TTLCache(maxsize=maxsize, ttl=ttl)
        logger.debug("Signal cache created: maxsize=%d ttl=%.3fs", maxsize, ttl)
    return _SIGNAL_CACHE


def get_sentiment_cache() -> TTLCache:
    """Get or create the sentiment cache (600s TTL, 1000 entries).

    For caching FinBERT sentiment analysis results to avoid
    re-running expensive inference on the same text.
    """
    global _SENTIMENT_CACHE
    if _SENTIMENT_CACHE is None:
        ttl = _get_cache_ttl("sentiment", 600.0)
        maxsize = _get_cache_maxsize("sentiment", 1000)
        _SENTIMENT_CACHE = TTLCache(maxsize=maxsize, ttl=ttl)
        logger.debug("Sentiment cache created: maxsize=%d ttl=%.1fs", maxsize, ttl)
    return _SENTIMENT_CACHE


def get_book_cache() -> TTLCache:
    """Get or create the order book cache (50ms TTL, 1000 entries).

    For caching order book snapshots to avoid redundant processing
    of the same book state.
    """
    global _BOOK_CACHE
    if _BOOK_CACHE is None:
        ttl = _get_cache_ttl("order_book", 0.05)
        maxsize = _get_cache_maxsize("l2", 1000)
        _BOOK_CACHE = TTLCache(maxsize=maxsize, ttl=ttl)
        logger.debug("Book cache created: maxsize=%d ttl=%.3fs", maxsize, ttl)
    return _BOOK_CACHE


def get_price_cache() -> TTLCache:
    """Get or create the price cache (100ms TTL, 1000 entries).

    For caching Chainlink/Binance price feeds to avoid redundant
    lookups of the same asset price within a short window.
    """
    global _PRICE_CACHE
    if _PRICE_CACHE is None:
        ttl = _get_cache_ttl("price", 0.1)
        maxsize = _get_cache_maxsize("l2", 1000)
        _PRICE_CACHE = TTLCache(maxsize=maxsize, ttl=ttl)
        logger.debug("Price cache created: maxsize=%d ttl=%.3fs", maxsize, ttl)
    return _PRICE_CACHE


def clear_all_caches() -> None:
    """Clear ALL cache levels. Used on reconnect or manual reset."""
    for cache_name, cache_var in [
        ("signal", _SIGNAL_CACHE),
        ("sentiment", _SENTIMENT_CACHE),
        ("book", _BOOK_CACHE),
        ("price", _PRICE_CACHE),
    ]:
        if cache_var is not None:
            cache_var.clear()
            logger.debug("Cache '%s' cleared", cache_name)


class SignalCache:
    """Convenience wrapper around L1 signal cache.

    Provides dict-like get/set with type hints and logging.
    """

    def __init__(self) -> None:
        self._cache = get_signal_cache()

    def get(self, key: str, default: Optional[T] = None) -> Any:
        """Get a cached signal value."""
        value = self._cache.get(key)
        if value is None:
            return default
        return value

    def set(self, key: str, value: Any) -> None:
        """Set a cached signal value with TTL."""
        self._cache[key] = value

    def get_or_compute(
        self, key: str, compute_fn: Callable[[], T], ttl_override: Optional[float] = None
    ) -> T:
        """Get from cache or compute and cache.

        Parameters
        ----------
        key : str
            Cache key.
        compute_fn : Callable[[], T]
            Function to compute the value if not cached.
        ttl_override : float, optional
            Override the default TTL for this specific entry.

        Returns
        -------
        T
            The cached or freshly computed value.
        """
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        value = compute_fn()
        if ttl_override is not None:
            # Use a temporary cache entry with custom TTL
            # Note: TTLCache doesn't support per-entry TTL natively
            # This stores the value with the cache's global TTL
            pass

        self._cache[key] = value
        return value


class TwoLevelCache:
    """Two-level cache with L1 (fast/small) and L2 (slower/larger).

    L1: 100ms TTL — for ultra-fresh data (prices, signals).
    L2: 5s TTL — for slightly older data (order books, computed metrics).

    On a cache hit in L2, the value is promoted back to L1.
    """

    def __init__(self) -> None:
        self._l1 = get_signal_cache()
        self._l2: TTLCache = TTLCache(
            maxsize=_get_cache_maxsize("l2", 1000),
            ttl=_get_cache_ttl("l2", 5.0),
        )

    def get(self, key: str) -> Any:
        """Get from L1 first, fall back to L2 (promoting to L1 on hit)."""
        value = self._l1.get(key)
        if value is not None:
            return value

        value = self._l2.get(key)
        if value is not None:
            # Promote to L1
            self._l1[key] = value
        return value

    def set(self, key: str, value: Any) -> None:
        """Set in both L1 and L2."""
        self._l1[key] = value
        self._l2[key] = value

    def invalidate(self, key: str) -> None:
        """Remove from both levels."""
        self._l1.pop(key, None)
        self._l2.pop(key, None)

    def clear(self) -> None:
        """Clear both levels."""
        self._l1.clear()
        self._l2.clear()


# Global two-level cache instances for common use cases
_signal_cache_instance: Optional[SignalCache] = None
_price_cache_instance: Optional[TwoLevelCache] = None


def get_signal_cache_instance() -> SignalCache:
    """Get the global SignalCache singleton."""
    global _signal_cache_instance
    if _signal_cache_instance is None:
        _signal_cache_instance = SignalCache()
    return _signal_cache_instance


def get_price_cache_instance() -> TwoLevelCache:
    """Get the global price TwoLevelCache singleton."""
    global _price_cache_instance
    if _price_cache_instance is None:
        _price_cache_instance = TwoLevelCache()
    return _price_cache_instance
