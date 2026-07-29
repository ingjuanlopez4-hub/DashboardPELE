"""
Slippage Model — Realistic order-book-based slippage estimation.

Replaces the legacy mid-price assumption with a depth-aware model that
predicts slippage based on order size relative to book depth at each level.
Supports maker fills (zero slippage), taker fills (depth-dependent), and
backtest simulation (synthetic depth from spread + volume).

Reference: Polymarket 2026 — tick size 0.01, typical book depth 500-5000 USDC
"""

import logging
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger(__name__)

TICK_SIZE = Decimal("0.01")

SlippageConfig = {
    "maker_slippage_bps": Decimal("0"),
    "taker_slippage_bps": Decimal("5"),
    "depth_tiers": [
        {"level": 1, "depth_usdc": Decimal("1000"), "slippage_bps": Decimal("1")},
        {"level": 2, "depth_usdc": Decimal("2500"), "slippage_bps": Decimal("3")},
        {"level": 3, "depth_usdc": Decimal("5000"), "slippage_bps": Decimal("5")},
        {"level": 4, "depth_usdc": Decimal("10000"), "slippage_bps": Decimal("10")},
        {"level": 5, "depth_usdc": Decimal("25000"), "slippage_bps": Decimal("20")},
    ],
    "fallback_slippage_bps": Decimal("15"),
    "min_slippage_bps": Decimal("0"),
    "max_slippage_bps": Decimal("50"),
}


class SlippageEstimator:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = config or SlippageConfig
        self._tiers = sorted(
            self._cfg["depth_tiers"],
            key=lambda t: t["level"],
        )

    def estimate_slippage(
        self,
        order_size_usdc: Decimal,
        side: str,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
        bid_depth: Decimal | None = None,
        ask_depth: Decimal | None = None,
        spread_pct: Decimal | None = None,
        is_maker: bool = False,
    ) -> Decimal:
        """Estimate expected slippage in basis points.

        Parameters
        ----------
        order_size_usdc : Decimal
            Order size in USDC (cost = price * size).
        side : str
            BUY_YES, BUY_NO, SELL_YES, or SELL_NO.
        best_bid : Decimal | None
            Current best bid price (for context).
        best_ask : Decimal | None
            Current best ask price.
        bid_depth : Decimal | None
            Total bid depth in USDC on near levels.
        ask_depth : Decimal | None
            Total ask depth in USDC on near levels.
        spread_pct : Decimal | None
            Current bid-ask spread as decimal (e.g. 0.02 for 2%).
        is_maker : bool
            If True, zero slippage (maker rebate).

        Returns
        -------
        Decimal
            Expected slippage in basis points (bps).
        """
        if is_maker:
            return self._cfg["maker_slippage_bps"]

        available_depth = ask_depth if "BUY" in side.upper() else bid_depth
        available_depth = available_depth or Decimal("5000")

        if available_depth <= 0:
            available_depth = Decimal("5000")

        size_ratio = order_size_usdc / available_depth if available_depth > 0 else Decimal("1")
        size_ratio = min(size_ratio, Decimal("10"))

        slippage_bps = self._interpolate_from_tiers(size_ratio)

        if spread_pct is not None and spread_pct > 0:
            spread_bps = spread_pct * Decimal("100")
            slippage_bps = max(slippage_bps, spread_bps * Decimal("0.3"))

        slippage_bps = max(slippage_bps, self._cfg["min_slippage_bps"])
        slippage_bps = min(slippage_bps, self._cfg["max_slippage_bps"])

        return slippage_bps.quantize(Decimal("0.1"))

    def _interpolate_from_tiers(self, ratio: Decimal) -> Decimal:
        for i, tier in enumerate(self._tiers):
            tier_depth = tier["depth_usdc"]
            normalized = Decimal("1")
            if self._tiers:
                max_depth = self._tiers[-1]["depth_usdc"]
                normalized = min(ratio / (max_depth / Decimal("1000")), Decimal("1"))

            if ratio <= normalized:
                return tier["slippage_bps"]

        return self._cfg["fallback_slippage_bps"]

    def estimate_execution_price(
        self,
        base_price: Decimal,
        order_size_usdc: Decimal,
        side: str,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
        bid_depth: Decimal | None = None,
        ask_depth: Decimal | None = None,
        spread_pct: Decimal | None = None,
        is_maker: bool = False,
    ) -> Decimal:
        """Estimate the actual execution price after slippage.

        Parameters
        ----------
        Same as estimate_slippage.

        Returns
        -------
        Decimal
            Expected execution price (worse than base_price by slippage).
        """
        slippage_bps = self.estimate_slippage(
            order_size_usdc=order_size_usdc,
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            spread_pct=spread_pct,
            is_maker=is_maker,
        )

        slippage_factor = Decimal("1") - (slippage_bps / Decimal("10000"))

        is_buy = "BUY" in side.upper()
        if is_buy:
            execution_price = base_price / slippage_factor
        else:
            execution_price = base_price * slippage_factor

        execution_price = execution_price.quantize(TICK_SIZE, rounding=ROUND_HALF_UP)
        execution_price = max(TICK_SIZE, min(execution_price, Decimal("1") - TICK_SIZE))

        return execution_price

    def compute_effective_edge(
        self,
        raw_edge: Decimal,
        order_size_usdc: Decimal,
        side: str,
        is_maker: bool = False,
    ) -> Decimal:
        """Compute edge after accounting for slippage.

        Parameters
        ----------
        raw_edge : Decimal
            Edge before slippage (as decimal, 0.0-1.0).
        order_size_usdc : Decimal
            Order size in USDC.
        side : str
            Trade direction.
        is_maker : bool
            Whether the order is a maker order.

        Returns
        -------
        Decimal
            Post-slippage edge.
        """
        slippage_bps = self.estimate_slippage(
            order_size_usdc=order_size_usdc,
            side=side,
            is_maker=is_maker,
        )
        slippage_dec = slippage_bps / Decimal("10000")
        return max(Decimal("0"), raw_edge - slippage_dec)

    def price_impact(
        self,
        order_size_usdc: Decimal,
        available_depth: Decimal,
    ) -> Decimal:
        """Estimate the price impact of an order on the book.

        Parameters
        ----------
        order_size_usdc : Decimal
            Order size in USDC.
        available_depth : Decimal
            Available liquidity at near levels.

        Returns
        -------
        Decimal
            Estimated price impact as a decimal (0.0-1.0).
        """
        if available_depth <= 0:
            return Decimal("0.02")
        ratio = order_size_usdc / available_depth
        impact = Decimal("0.01") * min(ratio, Decimal("5"))
        return impact.quantize(Decimal("0.0001"))
