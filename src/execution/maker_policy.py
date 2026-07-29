"""
Maker Policy — Maker-first execution strategy for Polymarket CLOB.

Implements the "maker, never taker" philosophy for Polymarket 2026:
  - Always place limit orders at best_bid + 1 tick (improving the bid).
  - If order doesn't fill within maker_timeout_seconds, cancel and re-evaluate.
  - Only cross the spread (take liquidity) when edge > 0.0156 + spread_pct.
  - Maker orders qualify for rebates and pay zero taker fees.

The dynamic taker fee at 50% probability is ~1.56%, making it uneconomical
to take liquidity for small edges. Maker rebates make even zero-spread
trades profitable for market makers.

Reference: Polymarket 2026 fee formula — fee = C × 0.25 × (p × (1-p))²
"""

import asyncio
import logging
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable

from src.config.live_settings import dynamic_taker_fee

logger = logging.getLogger(__name__)

# ── Defaults ───────────────────────────────────────────────────────────

DEFAULT_MAKER_TIMEOUT_S = 15.0
DEFAULT_CROSS_SPREAD_EDGE_THRESHOLD = Decimal("0.03")  # 3% edge needed to cross spread
DEFAULT_TICK_SIZE = Decimal("0.01")
TICKER_SIZE = Decimal("0.01")
MAX_SPREAD_CROSS_ATTEMPTS = 1  # Only cross once, then re-evaluate


class MakerPolicy:
    """Maker-first execution policy.

    Parameters
    ----------
    maker_timeout_s : float
        Seconds to wait for a maker order to fill before cancelling (default 15).
    cross_spread_edge_threshold : Decimal
        Minimum edge (as fraction) required to cross the spread as taker (default 0.03).
    tick_size : Decimal
        Market tick size for price quantization (default 0.01).
    cancel_order_cb : Callable | None
        Async callback to cancel an order by ID.
    place_limit_order_cb : Callable | None
        Async callback to place a limit order. Returns {success, order_id}.
    place_market_order_cb : Callable | None
        Async callback to place a market order (for forced closes only).
    get_order_status_cb : Callable | None
        Async callback to check if an order was filled: returns dict or None.
    """

    def __init__(
        self,
        maker_timeout_s: float = DEFAULT_MAKER_TIMEOUT_S,
        cross_spread_edge_threshold: Decimal = DEFAULT_CROSS_SPREAD_EDGE_THRESHOLD,
        tick_size: Decimal = DEFAULT_TICK_SIZE,
        cancel_order_cb: Callable | None = None,
        place_limit_order_cb: Callable | None = None,
        place_market_order_cb: Callable | None = None,
        get_order_status_cb: Callable | None = None,
    ) -> None:
        self._maker_timeout_s = maker_timeout_s
        self._cross_edge_threshold = cross_spread_edge_threshold
        self._tick_size = tick_size
        self._cancel_cb = cancel_order_cb
        self._place_limit_cb = place_limit_order_cb
        self._place_market_cb = place_market_order_cb
        self._get_status_cb = get_order_status_cb

    def compute_maker_price(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
        side: str,
        tick_size: Decimal | None = None,
    ) -> Decimal:
        """Compute the maker limit price — improve the current best bid/ask.

        For BUY orders: place at best_bid + 1 tick (improve the bid).
        For SELL orders: place at best_ask - 1 tick (improve the ask).

        Parameters
        ----------
        best_bid : Decimal
            Current best bid price.
        best_ask : Decimal
            Current best ask price.
        side : str
            "BUY" or "SELL".
        tick_size : Decimal | None
            Market tick size. Defaults to instance tick_size.

        Returns
        -------
        Decimal
            The maker limit price, quantized to tick_size.
        """
        ts = tick_size or self._tick_size

        if side.upper() == "BUY":
            if best_bid > 0:
                maker_price = best_bid + ts
            else:
                # No bid — use conservative estimate
                maker_price = (best_ask - ts * 2) if best_ask > 0 else Decimal("0.01")
        else:  # SELL
            if best_ask > 0:
                maker_price = best_ask - ts
            else:
                maker_price = (best_bid + ts * 2) if best_bid > 0 else Decimal("0.99")

        # Ensure valid range [tick_size, 1-tick_size]
        maker_price = max(ts, min(maker_price, Decimal("1") - ts))
        maker_price = maker_price.quantize(ts, rounding=ROUND_DOWN)
        return maker_price

    def should_cross_spread(
        self,
        edge: Decimal,
        probability: Decimal,
        spread_pct: Decimal,
    ) -> tuple[bool, str]:
        """Determine whether crossing the spread (taker) is justified.

        Only cross when edge exceeds the taker fee plus spread percentage.
        The taker fee is dynamically calculated based on the probability.

        Parameters
        ----------
        edge : Decimal
            Expected edge of the trade (as fraction, 0.0-1.0).
        probability : Decimal
            Current market probability.
        spread_pct : Decimal
            Current bid-ask spread as percentage (e.g., 2.0 for 2%).

        Returns
        -------
        (should_cross: bool, reason: str)
        """
        fee_rate = dynamic_taker_fee(probability)
        spread_cost = spread_pct / Decimal("100")

        total_cost = fee_rate + spread_cost
        threshold = self._cross_edge_threshold

        if edge > total_cost and edge > threshold:
            return True, (
                f"edge={edge:.4f} > fee={fee_rate:.4f}+spread={spread_cost:.4f}={total_cost:.4f}"
            )

        return False, (
            f"edge={edge:.4f} <= max(fee+spread={total_cost:.4f}, threshold={threshold:.4f})"
        )

    async def execute_maker_order(
        self,
        order_data: dict[str, Any],
        best_bid: Decimal,
        best_ask: Decimal,
        tick_size: Decimal | None = None,
    ) -> dict[str, Any]:
        """Execute a maker-first order placement with timeout.

        Flow:
          1. Compute maker price (improve best bid/ask by 1 tick).
          2. Place limit order at maker price.
          3. Wait up to maker_timeout_s for fill.
          4. If unfilled after timeout, cancel and return "timed_out".
          5. Return result dict with status.

        Parameters
        ----------
        order_data : dict
            Order data from EjecutorOrdenes._build_order_payload.
        best_bid : Decimal
            Current best bid.
        best_ask : Decimal
            Current best ask.
        tick_size : Decimal | None
            Market tick size.

        Returns
        -------
        dict with keys: success, order_id, status, maker_price, latency_ms
        """
        side = "BUY" if order_data.get("side") == 0 else "SELL"
        size = Decimal(str(order_data.get("makerAmount", "0"))) / Decimal(10 ** 6)  # Convert from wei

        ts = tick_size or self._tick_size

        # Compute maker price
        maker_price = self.compute_maker_price(
            best_bid=best_bid,
            best_ask=best_ask,
            side=side,
            tick_size=ts,
        )

        logger.info(
            "Maker order: side=%s size=%s maker_price=%s (best_bid=%s best_ask=%s)",
            side, size, maker_price, best_bid, best_ask,
        )

        # Update order_data with maker price
        maker_order = dict(order_data)
        price_wei = int(maker_price * Decimal(10 ** 6))
        if side == "BUY":
            maker_order["makerAmount"] = int(size * Decimal(10 ** 6) * price_wei // (10 ** 6))
        else:
            maker_order["takerAmount"] = price_wei

        # Place limit order
        if self._place_limit_cb:
            start = time.perf_counter()
            result = await self._place_limit_cb(maker_order)
            latency = (time.perf_counter() - start) * 1000
            order_id = result.get("order_id", "")
            success = result.get("success", False)

            if not success:
                return {
                    "success": False,
                    "order_id": "",
                    "status": "placement_failed",
                    "maker_price": str(maker_price),
                    "latency_ms": latency,
                    "error": result.get("error", "unknown"),
                }

            # Wait for fill with timeout
            start_wait = time.time()
            filled = False
            while (time.time() - start_wait) < self._maker_timeout_s:
                if self._get_status_cb:
                    status = await self._get_status_cb(order_id)
                    if status and status.get("status") in ("FILLED", "MATCHED"):
                        filled = True
                        break
                await asyncio.sleep(0.5)

            if filled:
                total_latency = (time.perf_counter() - start) * 1000
                logger.info(
                    "Maker order filled: id=%s price=%s latency=%.0fms",
                    order_id, maker_price, total_latency,
                )
                return {
                    "success": True,
                    "order_id": order_id,
                    "status": "filled",
                    "maker_price": str(maker_price),
                    "latency_ms": total_latency,
                }
            else:
                # Timeout — cancel order
                if self._cancel_cb:
                    await self._cancel_cb(order_id)
                total_latency = (time.perf_counter() - start) * 1000
                logger.info(
                    "Maker order timed out: id=%s price=%s — cancelled",
                    order_id, maker_price,
                )
                return {
                    "success": False,
                    "order_id": order_id,
                    "status": "timed_out",
                    "maker_price": str(maker_price),
                    "latency_ms": total_latency,
                }
        else:
            return {
                "success": False,
                "order_id": "",
                "status": "no_place_callback",
                "maker_price": str(maker_price),
                "latency_ms": 0,
            }

    async def execute_forced_close(
        self,
        order_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a forced close via market order (taker).

        This is the ONLY case where we cross the spread as taker.
        Used for stop-loss and emergency closes only.

        Parameters
        ----------
        order_data : dict
            Order data for the close.

        Returns
        -------
        dict with result.
        """
        logger.critical("FORCED MARKET CLOSE: %s", order_data)
        if self._place_market_cb:
            result = await self._place_market_cb(order_data)
            return {
                "success": result.get("success", False),
                "order_id": result.get("order_id", ""),
                "status": "market_close",
                "error": result.get("error", ""),
            }
        return {
            "success": False,
            "status": "no_market_callback",
        }
