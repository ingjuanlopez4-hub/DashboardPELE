"""
PositionManager — Manages open positions with take-profit, stop-loss,
and max position age enforcement.

Features:
- Configurable take-profit and stop-loss per position.
- Price monitoring: receives price updates and checks TP/SL thresholds.
- Force-close stale positions that exceed max age.
- Force-close signals are emitted as high-priority signals to the executor.
- Tracks position age in candle cycles.

All monetary values use Decimal for precision.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

logger = logging.getLogger("position_manager")


@staticmethod
def _quantize(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.0001"))


class TrackedPosition:
    """Internal representation of an open position with TP/SL levels."""

    __slots__ = (
        "asset_id", "side", "entry_price", "size", "entry_time",
        "take_profit_price", "stop_loss_price", "cycles_active",
        "market_type", "cycle_duration_minutes",
    )

    def __init__(
        self,
        asset_id: str,
        side: str,
        entry_price: Decimal,
        size: Decimal,
        entry_time: float,
        take_profit_pct: Decimal | None = None,
        stop_loss_pct: Decimal | None = None,
        market_type: str = "default",
        cycle_duration_minutes: int = 15,
    ) -> None:
        self.asset_id = asset_id
        self.side = side
        self.entry_price = entry_price
        self.size = size
        self.entry_time = entry_time
        self.market_type = market_type
        self.cycle_duration_minutes = cycle_duration_minutes

        # Calculate TP/SL price levels
        is_long = side in ("BUY_YES", "BUY_NO")
        if take_profit_pct is not None and take_profit_pct > 0:
            if is_long:
                self.take_profit_price = entry_price * (Decimal("1") + take_profit_pct / Decimal("100"))
            else:
                self.take_profit_price = entry_price * (Decimal("1") - take_profit_pct / Decimal("100"))
            self.take_profit_price = _quantize(self.take_profit_price)
        else:
            self.take_profit_price = None

        if stop_loss_pct is not None and stop_loss_pct > 0:
            if is_long:
                self.stop_loss_price = entry_price * (Decimal("1") - stop_loss_pct / Decimal("100"))
            else:
                self.stop_loss_price = entry_price * (Decimal("1") + stop_loss_pct / Decimal("100"))
            self.stop_loss_price = _quantize(self.stop_loss_price)
        else:
            self.stop_loss_price = None

        self.cycles_active = 0

    @property
    def age_cycles(self) -> int:
        """Number of full candle cycles this position has been open."""
        elapsed = time.time() - self.entry_time
        cycles = elapsed / (self.cycle_duration_minutes * 60)
        return int(cycles)

    def check_price(self, current_price: Decimal) -> str | None:
        """Check if current price triggers TP or SL.

        Returns action string: 'take_profit', 'stop_loss', or None.
        """
        is_long = self.side in ("BUY_YES", "BUY_NO")

        if self.take_profit_price is not None:
            if is_long and current_price >= self.take_profit_price:
                return "take_profit"
            if not is_long and current_price <= self.take_profit_price:
                return "take_profit"

        if self.stop_loss_price is not None:
            if is_long and current_price <= self.stop_loss_price:
                return "stop_loss"
            if not is_long and current_price >= self.stop_loss_price:
                return "stop_loss"

        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "side": self.side,
            "entry_price": str(self.entry_price),
            "size": str(self.size),
            "entry_time": self.entry_time,
            "take_profit_price": str(self.take_profit_price) if self.take_profit_price else None,
            "stop_loss_price": str(self.stop_loss_price) if self.stop_loss_price else None,
            "cycles_active": self.age_cycles,
            "market_type": self.market_type,
        }


class PositionManager:
    """Manages open positions with TP/SL monitoring and age enforcement.

    Parameters
    ----------
    force_close_cb : Callable[[dict], Any] | None
        Async callback to force-close a position. Receives a close signal dict.
    take_profit_pct : Decimal
        Default take-profit percentage (default 50%).
    stop_loss_pct : Decimal
        Default stop-loss percentage (default 30%).
    max_position_age_cycles : int
        Max candle cycles before force-close (default 3).
    cycle_duration_minutes : int
        Duration of one candle cycle in minutes (default 15).
    price_provider : Callable[[str], Awaitable[Decimal]] | None
        Async callable that returns the current price for an asset.
    """

    def __init__(
        self,
        force_close_cb: Callable | None = None,
        take_profit_pct: Decimal = Decimal("50.0"),
        stop_loss_pct: Decimal = Decimal("30.0"),
        max_position_age_cycles: int = 3,
        cycle_duration_minutes: int = 15,
        price_provider: Callable | None = None,
    ) -> None:
        self._force_close_cb = force_close_cb
        self._default_tp_pct = take_profit_pct
        self._default_sl_pct = stop_loss_pct
        self._max_age_cycles = max_position_age_cycles
        self._cycle_duration_minutes = cycle_duration_minutes
        self._price_provider = price_provider

        self._positions: dict[str, TrackedPosition] = {}  # asset_id -> position
        self._running = False
        self._monitor_task: asyncio.Task | None = None

    def open_position(
        self,
        asset_id: str,
        side: str,
        entry_price: Decimal,
        size: Decimal,
        take_profit_pct: Decimal | None = None,
        stop_loss_pct: Decimal | None = None,
        market_type: str = "default",
    ) -> None:
        """Register a new open position with optional TP/SL levels.

        If TP/SL pct is None, the defaults from constructor are used.
        Set to Decimal('0') to disable TP/SL for this position.
        """
        tp = take_profit_pct if take_profit_pct is not None else self._default_tp_pct
        sl = stop_loss_pct if stop_loss_pct is not None else self._default_sl_pct

        position = TrackedPosition(
            asset_id=asset_id,
            side=side,
            entry_price=entry_price,
            size=size,
            entry_time=time.time(),
            take_profit_pct=tp if tp > 0 else None,
            stop_loss_pct=sl if sl > 0 else None,
            market_type=market_type,
            cycle_duration_minutes=self._cycle_duration_minutes,
        )
        self._positions[asset_id] = position
        logger.info(
            "Position opened: asset=%s side=%s entry=%s size=%s tp=%s sl=%s",
            asset_id, side, str(entry_price), str(size),
            str(position.take_profit_price) if position.take_profit_price else "none",
            str(position.stop_loss_price) if position.stop_loss_price else "none",
        )

    def close_position(self, asset_id: str) -> TrackedPosition | None:
        """Remove a position from tracking (after manual close).

        Returns the removed position or None if not found.
        """
        return self._positions.pop(asset_id, None)

    def get_position(self, asset_id: str) -> TrackedPosition | None:
        """Get tracked position for an asset."""
        return self._positions.get(asset_id)

    def get_all_positions(self) -> dict[str, TrackedPosition]:
        """Get all tracked positions."""
        return dict(self._positions)

    async def update_price(self, asset_id: str, current_price: Decimal) -> str | None:
        """Update price for a tracked position and check TP/SL.

        Returns action string if TP/SL triggered, else None.
        """
        position = self._positions.get(asset_id)
        if position is None:
            return None

        action = position.check_price(current_price)
        if action is not None:
            logger.info(
                "%s triggered for %s: price=%s entry=%s",
                action.upper(), asset_id, str(current_price),
                str(position.entry_price),
            )
            await self._force_close(asset_id, reason=action, price=current_price)
            return action

        return None

    async def check_age_limit(self) -> list[str]:
        """Check all positions for age limit violations.

        Returns list of asset_ids that were force-closed due to age.
        """
        closed: list[str] = []
        for asset_id, position in list(self._positions.items()):
            if position.age_cycles >= self._max_age_cycles:
                logger.warning(
                    "Position %s exceeded max age: %d cycles >= %d cycles",
                    asset_id, position.age_cycles, self._max_age_cycles,
                )
                await self._force_close(
                    asset_id,
                    reason="max_age_exceeded",
                    reason_detail=f"age={position.age_cycles}cycles",
                )
                closed.append(asset_id)
        return closed

    async def _force_close(
        self,
        asset_id: str,
        reason: str = "unknown",
        price: Decimal | None = None,
        reason_detail: str = "",
    ) -> None:
        """Send a force-close signal to the executor."""
        position = self._positions.get(asset_id)
        if position is None:
            return

        close_signal = {
            "asset_id": asset_id,
            "market": "",
            "side": "SELL_YES" if position.side in ("BUY_YES", "BUY_NO") else "BUY_YES",
            "price": str(price) if price is not None else str(position.entry_price),
            "size": str(position.size),
            "is_force_close": True,
            "force_close_reason": reason,
            "force_close_detail": reason_detail,
            "probability": str(position.entry_price),
            "ev": "0",
            "priority": "high",
        }

        logger.critical(
            "FORCE CLOSE: asset=%s reason=%s detail=%s signal=%s",
            asset_id, reason, reason_detail, close_signal,
        )

        if self._force_close_cb:
            try:
                await self._force_close_cb(close_signal)
            except Exception:
                logger.exception("Force close callback failed for %s", asset_id)

        # Remove from tracking
        self._positions.pop(asset_id, None)

    async def _monitor_loop(self) -> None:
        """Periodic monitor that checks all positions."""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30s

                # Check age limits
                age_closed = await self.check_age_limit()
                if age_closed:
                    logger.info("Age-limited force-closed: %s", age_closed)

                # Check TP/SL via price provider
                if self._price_provider:
                    for asset_id in list(self._positions.keys()):
                        try:
                            price = await self._price_provider(asset_id)
                            if price is not None:
                                await self.update_price(asset_id, price)
                        except Exception:
                            logger.debug("Error checking price for %s", asset_id)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in position monitor loop")

    async def start(self) -> None:
        """Start the position monitoring loop."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "PositionManager started: max_age=%d cycles, TP=%s%%, SL=%s%%",
            self._max_age_cycles, str(self._default_tp_pct), str(self._default_sl_pct),
        )

    async def stop(self) -> None:
        """Stop the position monitoring loop."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("PositionManager stopped (%d positions remaining)", len(self._positions))

    def get_stats(self) -> dict[str, Any]:
        """Return position statistics for monitoring."""
        return {
            "open_positions": len(self._positions),
            "positions": [
                {
                    "asset_id": p.asset_id,
                    "side": p.side,
                    "entry_price": str(p.entry_price),
                    "size": str(p.size),
                    "age_cycles": p.age_cycles,
                    "tp_price": str(p.take_profit_price) if p.take_profit_price else None,
                    "sl_price": str(p.stop_loss_price) if p.stop_loss_price else None,
                }
                for p in self._positions.values()
            ],
        }
