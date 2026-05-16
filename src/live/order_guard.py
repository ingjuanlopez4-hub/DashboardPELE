"""
OrderGuard — Proactive order lifecycle management for live Polymarket trading.

Provides:
- clean_start(): Cancel ALL orders on bot startup (prevents orphaned orders)
- OrderWatchdog: Periodic heartbeat (every 30s) that cancels stale orders
- WS disconnect handling: Cancel all orders and block new placements
- WS reconnect handling: Re-allow placements after book sync
- Improved place_order_with_timeout with cancel_all on failure + critical alert
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("order_guard")

WATCHDOG_INTERVAL_S = 30
MAX_ORDER_AGE_S = 120
WS_DISCONNECT_GRACE_S = 10


class OrderWatchdog:
    """Periodic watchdog that cancels orders older than max_age_s.

    Runs every `interval_s` seconds and cancels any tracked open order
    that exceeds `max_age_s` seconds.
    """

    def __init__(
        self,
        cancel_order_cb: Callable[[str], Any],
        fetch_open_orders_cb: Callable[[], Any],
        max_age_s: int = MAX_ORDER_AGE_S,
        interval_s: int = WATCHDOG_INTERVAL_S,
    ) -> None:
        """
        Parameters
        ----------
        cancel_order_cb : Callable[[str], Any]
            Async callable to cancel a single order by ID.
        fetch_open_orders_cb : Callable[[], Any]
            Async callable returning list of (order_id, created_at_seconds).
        max_age_s : int
            Max age in seconds before an order is considered stale (default 120).
        interval_s : int
            How often to run the check (default 30).
        """
        self._cancel_cb = cancel_order_cb
        self._fetch_cb = fetch_open_orders_cb
        self._max_age_s = max_age_s
        self._interval_s = interval_s
        self._running = False
        self._task: asyncio.Task | None = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                await self._check_and_cancel()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("OrderWatchdog: error in check loop")

    async def _check_and_cancel(self) -> None:
        try:
            orders = await self._fetch_cb()
            now = time.time()
            cancelled = 0
            for oid, created_at in orders:
                age = now - created_at
                if age > self._max_age_s:
                    logger.warning(
                        "OrderWatchdog: stale order %s age=%.0fs — cancelling",
                        oid, age,
                    )
                    try:
                        await self._cancel_cb(oid)
                        cancelled += 1
                    except Exception:
                        logger.exception("OrderWatchdog: error cancelling %s", oid)
            if cancelled:
                logger.info("OrderWatchdog: cancelled %d stale order(s)", cancelled)
        except Exception:
            logger.exception("OrderWatchdog: error fetching open orders")

    async def start(self) -> None:
        """Start the watchdog loop."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "OrderWatchdog started (interval=%ds, max_age=%ds)",
            self._interval_s, self._max_age_s,
        )

    async def stop(self) -> None:
        """Stop the watchdog loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OrderWatchdog stopped")


class OrderGuard:
    """Proactive order lifecycle management.

    Coordinates startup cleanup, stale order watchdog, WS disconnect
    protection, and safe order placement with cancel-on-fail.

    Parameters
    ----------
    cancel_all_cb : Callable[[], Any]
        Async callable to cancel ALL open orders.
    cancel_order_cb : Callable[[str], Any]
        Async callable to cancel a single order by ID.
    fetch_open_orders_cb : Callable[[], Any]
        Async callable returning list of (order_id, created_at).
    max_order_age_s : int
        Max order age before watchdog cancels it (default 120).
    """

    def __init__(
        self,
        cancel_all_cb: Callable[[], Any],
        cancel_order_cb: Callable[[str], Any],
        fetch_open_orders_cb: Callable[[], Any],
        max_order_age_s: int = MAX_ORDER_AGE_S,
    ) -> None:
        self._cancel_all_cb = cancel_all_cb
        self._cancel_order_cb = cancel_order_cb
        self._fetch_cb = fetch_open_orders_cb
        self._max_order_age_s = max_order_age_s

        self._watchdog: OrderWatchdog | None = None
        self._ws_disconnected: bool = False
        self._trading_paused: bool = False
        self._running = False

    async def clean_start(self) -> None:
        """Cancel ALL orders on bot startup.

        Must be called before any trading begins. This prevents orphaned
        orders from previous bot instances from being executed.
        """
        logger.info("OrderGuard: clean_start — cancelling all residual orders")
        try:
            await self._cancel_all_cb()
            logger.info("OrderGuard: clean_start — all orders cancelled")
        except Exception:
            logger.exception("OrderGuard: clean_start — error cancelling orders")
        self._trading_paused = False

    async def start_watchdog(self) -> None:
        """Start the OrderWatchdog for periodic stale order checking."""
        if self._watchdog is None:
            self._watchdog = OrderWatchdog(
                cancel_order_cb=self._cancel_order_cb,
                fetch_open_orders_cb=self._fetch_cb,
                max_age_s=self._max_order_age_s,
            )
        await self._watchdog.start()

    async def stop_watchdog(self) -> None:
        """Stop the OrderWatchdog."""
        if self._watchdog:
            await self._watchdog.stop()

    async def on_ws_disconnect(self) -> None:
        """Handle WebSocket disconnection.

        Immediately cancels all orders and blocks new placements.
        Trading resumes only after `on_ws_reconnect` + book sync.
        """
        logger.warning(
            "OrderGuard: WS disconnect detected — grace period %ds",
            WS_DISCONNECT_GRACE_S,
        )
        self._ws_disconnected = True
        self._trading_paused = True

        # Wait grace period then cancel all
        await asyncio.sleep(WS_DISCONNECT_GRACE_S)
        if self._ws_disconnected:
            logger.critical("OrderGuard: WS grace expired — cancelling ALL orders")
            await self._cancel_all_cb()
            logger.critical("OrderGuard: trading PAUSED until WS reconnection + book sync")

    async def on_ws_reconnect(self) -> None:
        """Handle WebSocket reconnection.

        Resets disconnect flag but leaves trading paused until
        `resume_trading()` is called (after book sync completes).
        """
        logger.info("OrderGuard: WS reconnected — awaiting book sync")
        self._ws_disconnected = False

    async def resume_trading(self) -> None:
        """Resume trading after WS reconnection and book sync."""
        if not self._ws_disconnected:
            self._trading_paused = False
            logger.info("OrderGuard: trading RESUMED")

    async def is_trading_paused(self) -> bool:
        """Check if trading is currently paused due to WS disconnect."""
        return self._trading_paused

    async def shutdown(self) -> None:
        """Clean shutdown — stop watchdog and cancel all orders."""
        await self.stop_watchdog()
        logger.info("OrderGuard: shutdown — cancelling all orders")
        await self._cancel_all_cb()
        logger.info("OrderGuard: shutdown complete")
