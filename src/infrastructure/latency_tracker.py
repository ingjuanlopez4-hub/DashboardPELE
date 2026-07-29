"""
Latency Tracker — Per-operation latency measurement with P50/P95/P99 statistics.

Provides a context manager decorator @measure_latency(operation_name) and a
tracker class that records latency for each operation type and computes
aggregate statistics over a sliding window.

Usage:
    tracker = LatencyTracker(window_size=1000)

    # As a context manager:
    with tracker.measure("finbert_inference"):
        result = await model.analyze(text)

    # As a decorator:
    @tracker.measure("monte_carlo_sim")
    async def simulate(...):
        ...

    # Get stats:
    stats = tracker.stats()
    print(stats["monte_carlo_sim"]["p95"])
"""

import asyncio
import inspect
import logging
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger("latency_tracker")

T = TypeVar("T")


class _LatencyContext:
    """Context manager that records latency on exit."""

    __slots__ = ("_tracker", "_operation", "_start")

    def __init__(self, tracker: "LatencyTracker", operation: str) -> None:
        self._tracker = tracker
        self._operation = operation
        self._start = 0.0

    def __enter__(self) -> "_LatencyContext":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._tracker.record(self._operation, elapsed_ms)


class AsyncLatencyContext:
    """Async context manager for measuring coroutine latency."""

    __slots__ = ("_tracker", "_operation", "_start")

    def __init__(self, tracker: "LatencyTracker", operation: str) -> None:
        self._tracker = tracker
        self._operation = operation
        self._start = 0.0

    async def __aenter__(self) -> "AsyncLatencyContext":
        self._start = time.perf_counter()
        return self

    async def __aexit__(self, *args: Any) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._tracker.record(self._operation, elapsed_ms)


class LatencyTracker:
    """Per-operation latency tracker with sliding window statistics.

    Tracks latency for each unique operation name over a configurable
    sliding window. Computes P50, P95, P99, max, and mean latency.

    Parameters
    ----------
    window_size : int
        Maximum number of latency samples to keep per operation.
        Older samples are evicted (FIFO). Default 1000.
    """

    def __init__(self, window_size: int = 1000) -> None:
        self._window_size = window_size
        self._data: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        self._lock = asyncio.Lock()

    def measure(self, operation: str) -> _LatencyContext:
        """Return a sync context manager for measuring latency.

        Usage:
            with tracker.measure("my_op"):
                do_something()
        """
        return _LatencyContext(self, operation)

    def measure_async(self, operation: str) -> AsyncLatencyContext:
        """Return an async context manager for measuring coroutine latency.

        Usage:
            async with tracker.measure_async("my_op"):
                await do_something()
        """
        return AsyncLatencyContext(self, operation)

    def record(self, operation: str, latency_ms: float) -> None:
        """Record a single latency measurement.

        Parameters
        ----------
        operation : str
            Name of the operation (e.g., "finbert_inference", "ws_message_parse").
        latency_ms : float
            Measured latency in milliseconds.
        """
        self._data[operation].append((latency_ms, time.monotonic()))

    def stats(self, operation: Optional[str] = None) -> dict[str, Any]:
        """Compute aggregate latency statistics.

        Parameters
        ----------
        operation : str, optional
            If provided, returns stats only for this operation.
            If None, returns stats for ALL operations.

        Returns
        -------
        dict mapping operation name -> {
            "count": int,
            "mean_ms": float,
            "p50_ms": float,
            "p95_ms": float,
            "p99_ms": float,
            "max_ms": float,
            "min_ms": float,
        }
        """
        if operation:
            return {operation: self._compute_stats(operation)}

        result: dict[str, Any] = {}
        for op in self._data:
            result[op] = self._compute_stats(op)
        return result

    def _compute_stats(self, operation: str) -> dict[str, float]:
        samples = [s[0] for s in self._data.get(operation, [])]
        if not samples:
            return {
                "count": 0,
                "mean_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "max_ms": 0.0,
                "min_ms": 0.0,
                "std_ms": 0.0,
            }

        sorted_samples = sorted(samples)
        n = len(sorted_samples)
        mean = sum(sorted_samples) / n

        # Standard deviation
        variance = sum((x - mean) ** 2 for x in sorted_samples) / n

        return {
            "count": n,
            "mean_ms": round(mean, 3),
            "p50_ms": round(sorted_samples[int(n * 0.50)], 3),
            "p95_ms": round(sorted_samples[int(n * 0.95)], 3),
            "p99_ms": round(sorted_samples[int(n * 0.99)], 3),
            "max_ms": round(sorted_samples[-1], 3),
            "min_ms": round(sorted_samples[0], 3),
            "std_ms": round(variance ** 0.5, 3),
        }

    def clear(self, operation: Optional[str] = None) -> None:
        """Clear recorded data for an operation (or all operations)."""
        if operation:
            self._data.pop(operation, None)
        else:
            self._data.clear()


# Global singleton
_global_tracker: Optional[LatencyTracker] = None


def get_latency_tracker() -> LatencyTracker:
    """Get or create the global LatencyTracker singleton."""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = LatencyTracker()
    return _global_tracker


def measure_latency(operation: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that measures latency of a function or coroutine.

    Usage:
        @measure_latency("my_operation")
        async def my_coro():
            ...

        @measure_latency("my_sync_op")
        def my_func():
            ...
    """
    tracker = get_latency_tracker()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return await func(*args, **kwargs)  # type: ignore[misc]
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                tracker.record(operation, elapsed_ms)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                tracker.record(operation, elapsed_ms)

        if inspect.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper

    return decorator
