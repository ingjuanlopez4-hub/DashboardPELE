"""
Order Lifecycle Manager — Timeout-safe order placement with retry and cancel-on-fail.

Wraps the CLOB client to provide:
- Per-operation timeout via asyncio.wait_for
- Configurable retry budget (max 2 retries)
- Unconditional cancel_all on timeout/failure
- Stale order periodic checker
- @timeout_cycle decorator for trading cycle methods
- clean_start() for startup order cancellation
- OrderWatchdog integration hooks
"""

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger("order_lifecycle")

DEFAULT_OP_TIMEOUT_S = 5
DEFAULT_CYCLE_TIMEOUT_S = 15
DEFAULT_MAX_RETRIES = 2
DEFAULT_STALE_MAX_AGE_S = 120


# ── Decorator ──────────────────────────────────────────────────────────

def timeout_cycle(timeout_s: float = DEFAULT_CYCLE_TIMEOUT_S):
    """Decorator that wraps a trading cycle method with a wall-clock timeout.

    If the method exceeds timeout_s, all open orders are cancelled
    and the exception is re-raised.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return await asyncio.wait_for(
                    func(self, *args, **kwargs),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                logger.critical(
                    "CYCLE TIMEOUT: %s exceeded %ss — cancelling all orders",
                    func.__name__, timeout_s,
                )
                if hasattr(self, "_cancel_all_cb") and self._cancel_all_cb:
                    await self._cancel_all_cb()
                raise
        return wrapper
    return decorator


# ── Operation Result ───────────────────────────────────────────────────

@functools.total_ordering
class OrderOpResult:
    """Result of a single order placement operation."""

    def __init__(
        self,
        success: bool,
        order_id: str = "",
        error: str = "",
        latency_ms: float = 0.0,
        payload: dict | None = None,
    ) -> None:
        self.success = success
        self.order_id = order_id
        self.error = error
        self.latency_ms = latency_ms
        self.payload = payload or {}

    def __bool__(self) -> bool:
        return self.success

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, OrderOpResult):
            return NotImplemented
        return self.latency_ms < other.latency_ms


# ── Order Lifecycle Manager ───────────────────────────────────────────

class OrderLifecycleManager:
    """Manages the full lifecycle of order placement with safety guarantees.

    Parameters
    ----------
    place_order_func : Callable
        Async callable that places a single order. Receives (order_data, signature, signal).
        Must return dict with keys: success, order_id, error.
    cancel_all_func : Callable
        Async callable that cancels ALL open orders.
    cancel_order_func : Callable[[str], Any]
        Async callable that cancels a single order by ID.
    fetch_open_orders_func : Callable
        Async callable returning list of (order_id, created_at_seconds).
    op_timeout_s : float
        Timeout for a single order placement operation (default 5s).
    cycle_timeout_s : float
        Timeout for a full trading cycle (default 15s).
    max_retries : int
        Max retries per order placement (default 2).
    stale_max_age_s : int
        Max age in seconds before an order is considered stale (default 120).
    """

    def __init__(
        self,
        place_order_func: Callable | None = None,
        cancel_all_func: Callable | None = None,
        cancel_order_func: Callable | None = None,
        fetch_open_orders_func: Callable | None = None,
        op_timeout_s: float = DEFAULT_OP_TIMEOUT_S,
        cycle_timeout_s: float = DEFAULT_CYCLE_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        stale_max_age_s: int = DEFAULT_STALE_MAX_AGE_S,
    ) -> None:
        self._place_func = place_order_func
        self._cancel_all_func = cancel_all_func
        self._cancel_order_func = cancel_order_func
        self._fetch_func = fetch_open_orders_func
        self._op_timeout_s = op_timeout_s
        self._cycle_timeout_s = cycle_timeout_s
        self._max_retries = max_retries
        self._stale_max_age_s = stale_max_age_s

        # Tracking for lifecycle
        self._open_orders: dict[str, float] = {}  # order_id -> created_at
        self._last_cycle_start: float = 0.0
        self._cancel_all_cb = cancel_all_func  # used by @timeout_cycle decorator

    # ── Clean Start (call on bot startup) ──────────────────────────────

    async def clean_start(self) -> None:
        """Cancel ALL orders on bot startup to clear residual orders.

        Must be called before any trading begins. Cancels all open orders
        and clears local open order tracking.
        """
        logger.info("clean_start: cancelling all residual orders")
        await self._cancel_all_safe()
        self._open_orders.clear()
        logger.info("clean_start: complete — local state cleared")

    # ── Place Order with Timeout & Retry ───────────────────────────────

    async def place_order_with_timeout(
        self,
        order_data: dict[str, Any],
        signature: str,
        signal: dict[str, Any],
    ) -> OrderOpResult:
        """Place an order with timeout, retries, and cancel-on-fail.

        On failure (all retries exhausted or timeout): cancels ALL orders.
        """
        start = time.perf_counter()
        last_error = ""

        for attempt in range(1, self._max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self._place_func(order_data, signature, signal),
                    timeout=self._op_timeout_s,
                )
                elapsed = (time.perf_counter() - start) * 1000

                if result.get("success"):
                    oid = result.get("order_id", "")
                    logger.info(
                        "order placed: attempt=%d/%d order_id=%s latency=%.0fms",
                        attempt, self._max_retries, oid, elapsed,
                    )
                    return OrderOpResult(
                        success=True,
                        order_id=oid,
                        latency_ms=elapsed,
                    )

                last_error = result.get("error", "unknown_error")
                logger.warning(
                    "order attempt %d/%d failed: %s",
                    attempt, self._max_retries, last_error,
                )

                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)

            except asyncio.TimeoutError:
                last_error = f"timeout_exceeded_{self._op_timeout_s}s"
                logger.error(
                    "order attempt %d/%d TIMEOUT (%ss)",
                    attempt, self._max_retries, self._op_timeout_s,
                )
                if attempt < self._max_retries:
                    continue

            except Exception as exc:
                last_error = str(exc)
                logger.exception(
                    "order attempt %d/%d exception",
                    attempt, self._max_retries,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)

        # All retries exhausted — cancel everything
        logger.critical(
            "ORDER FAILED after %d retries — cancelling all orders",
            self._max_retries,
        )
        await self._cancel_all_safe()
        return OrderOpResult(
            success=False,
            error=f"failed_after_{self._max_retries}_retries: {last_error}",
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    async def _cancel_all_safe(self) -> None:
        """Cancel all orders, catching and logging any errors."""
        if self._cancel_all_func:
            try:
                await asyncio.wait_for(
                    self._cancel_all_func(),
                    timeout=self._op_timeout_s,
                )
                logger.info("cancel_all completed successfully")
            except asyncio.TimeoutError:
                logger.critical("cancel_all TIMEOUT — orders may be orphaned!")
            except Exception:
                logger.exception("error in cancel_all")

    # ── Cancel Stale Orders ────────────────────────────────────────────

    async def cancel_stale_orders(self, max_age_s: int | None = None) -> int:
        """Find and cancel orders older than max_age_s.

        Returns the number of orders cancelled.
        """
        max_age = max_age_s or self._stale_max_age_s
        if not self._fetch_func or not self._cancel_order_func:
            return 0

        try:
            orders = await asyncio.wait_for(
                self._fetch_func(),
                timeout=self._op_timeout_s,
            )
        except Exception:
            logger.exception("error fetching open orders for stale check")
            return 0

        now = time.time()
        cancelled = 0

        for oid, created_at in orders:
            age = now - created_at
            if age > max_age:
                logger.warning("cancelling stale order %s (age=%.0fs)", oid, age)
                try:
                    await asyncio.wait_for(
                        self._cancel_order_func(oid),
                        timeout=self._op_timeout_s,
                    )
                    cancelled += 1
                except Exception:
                    logger.exception("error cancelling stale order %s", oid)

        if cancelled:
            logger.info("cancelled %d stale order(s)", cancelled)

        return cancelled

    # ── Full Cycle ─────────────────────────────────────────────────────

    @timeout_cycle(DEFAULT_CYCLE_TIMEOUT_S)
    async def execute_trading_cycle(
        self,
        signal: dict[str, Any],
        order_data: dict[str, Any],
        signature: str,
    ) -> OrderOpResult:
        """Execute a full trading cycle: place order + verify.

        This method is wrapped with @timeout_cycle, so if the entire
        cycle exceeds the timeout, cancel_all is triggered automatically.
        """
        self._last_cycle_start = time.time()
        result = await self.place_order_with_timeout(order_data, signature, signal)

        if result.success:
            oid = result.order_id
            self._open_orders[oid] = time.time()
            logger.info(
                "cycle complete: order=%s latency=%.0fms",
                oid, result.latency_ms,
            )
        else:
            logger.error("cycle failed: %s", result.error)

        return result

    async def cleanup_on_startup(self) -> None:
        """Cancel all orders on startup and clear local state."""
        logger.info("startup cleanup: cancelling residual orders")
        await self._cancel_all_safe()
        self._open_orders.clear()

    async def cleanup_on_shutdown(self) -> None:
        """Cancel all orders on shutdown."""
        logger.info("shutdown cleanup: cancelling all orders")
        await self._cancel_all_safe()
        self._open_orders.clear()
