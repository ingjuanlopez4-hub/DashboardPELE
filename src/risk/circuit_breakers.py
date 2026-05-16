import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Callable

import aiosqlite

logger = logging.getLogger("circuit_breaker")

DB_PATH_DEFAULT = "bot_state.db"
RESERVE_PCT_DEFAULT = Decimal("20")
DRAWDOWN_MAX_DEFAULT = Decimal("10")
DAILY_LOSS_MAX_DEFAULT = Decimal("5")
STALE_ORDER_AGE_S = 120
CYCLE_TIMEOUT_S = 15
SINGLE_OP_TIMEOUT_S = 5
MAX_RETRIES = 2
WS_DISCONNECT_GRACE_S = 30


class BotStatus(Enum):
    HEALTHY = auto()
    DEGRADED = auto()
    BLOCKED = auto()


class BlockReason(Enum):
    NONE = "none"
    DRAWDOWN = "drawdown_exceeded"
    TOTAL_DRAWDOWN = "total_drawdown_exceeded"
    DAILY_LOSS = "daily_loss_exceeded"
    CASH_RESERVE = "cash_reserve_breach"
    ORDER_TIMEOUT = "order_timeout"
    STALE_ORDERS = "stale_orders"
    WS_DISCONNECT = "websocket_disconnected"
    EXPOSURE_MARKET = "exposure_per_market_exceeded"
    EXPOSURE_TOTAL = "exposure_total_exceeded"
    FAILURE_COOLDOWN = "failure_cooldown_active"
    MANUAL = "manual_block"


@dataclass
class CircuitBreakerState:
    high_water_mark: Decimal = Decimal("0")
    high_water_mark_ts: str = ""
    blocked: bool = False
    block_reason: str = "none"
    blocked_at: str = ""
    daily_start_balance: Decimal = Decimal("0")
    daily_loss_accrued: Decimal = Decimal("0")
    daily_loss_date: str = ""
    total_orders_placed: int = 0
    total_orders_filled: int = 0
    total_pnl: Decimal = Decimal("0")

    total_drawdown_blocked: bool = False
    total_drawdown_peak_balance: Decimal = Decimal("0")
    total_drawdown_peak_ts: str = ""
    total_drawdown_blocked_at: str = ""

    open_positions: str = "{}"
    pending_buy_orders: str = "{}"


class CircuitBreakerManager:
    def __init__(
        self,
        db_path: str = DB_PATH_DEFAULT,
        balance_provider: Callable | None = None,
        inventory_mtm_provider: Callable | None = None,
        cancel_all_cb: Callable | None = None,
        fetch_open_orders_cb: Callable | None = None,
        cancel_order_cb: Callable | None = None,
        reserve_pct: Decimal = RESERVE_PCT_DEFAULT,
        max_drawdown_pct: Decimal = DRAWDOWN_MAX_DEFAULT,
        max_daily_loss_pct: Decimal = DAILY_LOSS_MAX_DEFAULT,
        max_total_drawdown_pct: Decimal | None = None,
        max_exposure_per_market_pct: Decimal | None = None,
        max_total_exposure_pct: Decimal | None = None,
        failure_window_seconds: int = 1800,
        max_consecutive_failures: int = 5,
        cooldown_seconds: int = 3600,
    ) -> None:
        self._db_path = db_path
        self._balance_provider = balance_provider
        self._inventory_mtm_provider = inventory_mtm_provider
        self._cancel_all_cb = cancel_all_cb
        self._fetch_open_orders_cb = fetch_open_orders_cb
        self._cancel_order_cb = cancel_order_cb
        self._reserve_pct = reserve_pct
        self._max_drawdown_pct = max_drawdown_pct
        self._max_daily_loss_pct = max_daily_loss_pct

        self._max_total_drawdown_pct = max_total_drawdown_pct or Decimal("25.0")
        self._max_exposure_per_market_pct = max_exposure_per_market_pct or Decimal("10.0")
        self._max_total_exposure_pct = max_total_exposure_pct or Decimal("50.0")
        self._failure_window_seconds = failure_window_seconds
        self._max_consecutive_failures = max_consecutive_failures
        self._cooldown_seconds = cooldown_seconds

        self._state = CircuitBreakerState()
        self._status = BotStatus.HEALTHY
        self._db: aiosqlite.Connection | None = None

        self._failure_timestamps: deque[float] = deque(maxlen=max_consecutive_failures * 2)
        self._cooldown_until: float = 0.0

        self._pending_buy_costs: dict[str, Decimal] = {}

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS daily_loss_tracker (
                    date TEXT PRIMARY KEY,
                    loss_accrued TEXT NOT NULL,
                    start_balance TEXT NOT NULL
                )
            """)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker_v2_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await self._db.commit()
        return self._db

    async def _persist(self) -> None:
        db = await self._ensure_db()
        mapping = {
            "high_water_mark": str(self._state.high_water_mark),
            "high_water_mark_ts": self._state.high_water_mark_ts,
            "blocked": str(int(self._state.blocked)),
            "block_reason": self._state.block_reason,
            "blocked_at": self._state.blocked_at,
            "daily_start_balance": str(self._state.daily_start_balance),
            "daily_loss_accrued": str(self._state.daily_loss_accrued),
            "daily_loss_date": self._state.daily_loss_date,
            "total_orders_placed": str(self._state.total_orders_placed),
            "total_orders_filled": str(self._state.total_orders_filled),
            "total_pnl": str(self._state.total_pnl),
        }
        for k, v in mapping.items():
            await db.execute(
                "INSERT OR REPLACE INTO circuit_breaker_state (key, value) VALUES (?, ?)",
                (k, v),
            )

        v2_mapping = {
            "total_drawdown_blocked": str(int(self._state.total_drawdown_blocked)),
            "total_drawdown_peak_balance": str(self._state.total_drawdown_peak_balance),
            "total_drawdown_peak_ts": self._state.total_drawdown_peak_ts,
            "total_drawdown_blocked_at": self._state.total_drawdown_blocked_at,
            "open_positions": self._state.open_positions,
            "pending_buy_orders": self._state.pending_buy_orders,
        }
        for k, v in v2_mapping.items():
            await db.execute(
                "INSERT OR REPLACE INTO circuit_breaker_v2_state (key, value) VALUES (?, ?)",
                (k, v),
            )

        await db.commit()

    async def _load_state(self) -> None:
        db = await self._ensure_db()
        cursor = await db.execute("SELECT key, value FROM circuit_breaker_state")
        rows = await cursor.fetchall()
        if rows:
            mapping = dict(rows)
            self._state.high_water_mark = Decimal(mapping.get("high_water_mark", "0"))
            self._state.high_water_mark_ts = mapping.get("high_water_mark_ts", "")
            self._state.blocked = bool(int(mapping.get("blocked", "0")))
            self._state.block_reason = mapping.get("block_reason", "none")
            self._state.blocked_at = mapping.get("blocked_at", "")
            self._state.daily_start_balance = Decimal(mapping.get("daily_start_balance", "0"))
            self._state.daily_loss_accrued = Decimal(mapping.get("daily_loss_accrued", "0"))
            self._state.daily_loss_date = mapping.get("daily_loss_date", "")
            self._state.total_orders_placed = int(mapping.get("total_orders_placed", "0"))
            self._state.total_orders_filled = int(mapping.get("total_orders_filled", "0"))
            self._state.total_pnl = Decimal(mapping.get("total_pnl", "0"))
            if self._state.blocked:
                self._status = BotStatus.BLOCKED

        cursor2 = await db.execute("SELECT key, value FROM circuit_breaker_v2_state")
        rows2 = await cursor2.fetchall()
        if rows2:
            mapping2 = dict(rows2)
            self._state.total_drawdown_blocked = bool(int(mapping2.get("total_drawdown_blocked", "0")))
            self._state.total_drawdown_peak_balance = Decimal(mapping2.get("total_drawdown_peak_balance", "0"))
            self._state.total_drawdown_peak_ts = mapping2.get("total_drawdown_peak_ts", "")
            self._state.total_drawdown_blocked_at = mapping2.get("total_drawdown_blocked_at", "")
            self._state.open_positions = mapping2.get("open_positions", "{}")
            self._state.pending_buy_orders = mapping2.get("pending_buy_orders", "{}")

        if self._state.total_drawdown_blocked:
            self._status = BotStatus.BLOCKED
            self._state.block_reason = BlockReason.TOTAL_DRAWDOWN.value

    async def _apply_daily_loss_recovery(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT date, loss_accrued, start_balance FROM daily_loss_tracker WHERE date = ?",
            (today,),
        )
        row = await cursor.fetchone()
        if row:
            self._state.daily_loss_date = row[0]
            self._state.daily_loss_accrued = Decimal(row[1])
            self._state.daily_start_balance = Decimal(row[2])
        else:
            self._state.daily_loss_accrued = Decimal("0")
            self._state.daily_loss_date = today
            balance = await self._get_balance()
            self._state.daily_start_balance = balance
            await db.execute(
                "INSERT OR REPLACE INTO daily_loss_tracker (date, loss_accrued, start_balance) VALUES (?, ?, ?)",
                (today, "0", str(balance)),
            )
            await db.commit()

    async def _get_balance(self) -> Decimal:
        if self._balance_provider:
            try:
                return await self._balance_provider()
            except Exception:
                logger.exception("error getting balance")
        return Decimal("0")

    async def _get_equity(self) -> Decimal:
        balance = await self._get_balance()
        mtm = Decimal("0")
        if self._inventory_mtm_provider:
            try:
                mtm = await self._inventory_mtm_provider()
            except Exception:
                logger.exception("error getting inventory MTM")
        return balance + mtm

    async def check_drawdown(self) -> str | None:
        try:
            equity = await self._get_equity()
            if equity > self._state.high_water_mark:
                self._state.high_water_mark = equity
                self._state.high_water_mark_ts = datetime.now(timezone.utc).isoformat()
                await self._persist()
                return None

            if self._state.high_water_mark <= 0:
                return None

            dd_pct = ((self._state.high_water_mark - equity) / self._state.high_water_mark) * Decimal("100")
            if dd_pct >= self._max_drawdown_pct:
                msg = (
                    f"drawdown_kill_switch: {dd_pct:.2f}% > {self._max_drawdown_pct}% "
                    f"(equity={equity} hwm={self._state.high_water_mark})"
                )
                logger.critical("DRAWDOWN KILL-SWITCH TRIGGERED: %s", msg)
                await self._block_trading(BlockReason.DRAWDOWN, msg)
                return msg

            return None
        except Exception as exc:
            logger.exception("error in drawdown check")
            return f"drawdown_check_failed: {exc}"

    async def check_total_drawdown(self) -> str | None:
        if self._state.total_drawdown_blocked:
            return (
                f"total_drawdown_permanently_blocked: "
                f"peak={self._state.total_drawdown_peak_balance} "
                f"blocked_at={self._state.total_drawdown_blocked_at}"
            )

        try:
            equity = await self._get_equity()

            if equity > self._state.total_drawdown_peak_balance:
                self._state.total_drawdown_peak_balance = equity
                self._state.total_drawdown_peak_ts = datetime.now(timezone.utc).isoformat()
                await self._persist()
                return None

            if self._state.total_drawdown_peak_balance <= 0:
                return None

            dd_pct = (
                (self._state.total_drawdown_peak_balance - equity)
                / self._state.total_drawdown_peak_balance
            ) * Decimal("100")

            if dd_pct >= self._max_total_drawdown_pct:
                msg = (
                    f"total_drawdown_kill_switch: {dd_pct:.2f}% > {self._max_total_drawdown_pct}% "
                    f"(equity={equity} peak={self._state.total_drawdown_peak_balance})"
                )
                logger.critical("TOTAL DRAWDOWN KILL-SWITCH TRIGGERED (PERMANENT): %s", msg)
                self._state.total_drawdown_blocked = True
                self._state.total_drawdown_blocked_at = datetime.now(timezone.utc).isoformat()
                await self._persist()
                await self._block_trading(BlockReason.TOTAL_DRAWDOWN, msg)
                return msg

            return None
        except Exception as exc:
            logger.exception("error in total drawdown check")
            return f"total_drawdown_check_failed: {exc}"

    async def check_daily_loss(self) -> str | None:
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._state.daily_loss_date != today:
                await self._apply_daily_loss_recovery()

            if self._state.daily_start_balance <= 0:
                return None

            loss_pct = (self._state.daily_loss_accrued / self._state.daily_start_balance) * Decimal("100")
            if loss_pct >= self._max_daily_loss_pct:
                msg = (
                    f"daily_loss_exceeded: {loss_pct:.2f}% > {self._max_daily_loss_pct}% "
                    f"(loss={self._state.daily_loss_accrued})"
                )
                logger.critical("DAILY LOSS LIMIT REACHED: %s", msg)
                await self._block_trading(BlockReason.DAILY_LOSS, msg)
                return msg

            return None
        except Exception as exc:
            logger.exception("error in daily loss check")
            return f"daily_loss_check_failed: {exc}"

    async def record_pnl(self, pnl: Decimal) -> None:
        self._state.total_pnl += pnl
        if pnl < 0:
            self._state.daily_loss_accrued += abs(pnl)
            db = await self._ensure_db()
            await db.execute(
                "UPDATE daily_loss_tracker SET loss_accrued = ? WHERE date = ?",
                (str(self._state.daily_loss_accrued), self._state.daily_loss_date),
            )
            await db.commit()
        await self._persist()

    async def record_order(self, filled: bool = False) -> None:
        self._state.total_orders_placed += 1
        if filled:
            self._state.total_orders_filled += 1
        await self._persist()

    async def check_cash_reserve(self, proposed_cost: Decimal) -> str | None:
        try:
            balance = await self._get_balance()
            free_cash = balance - proposed_cost
            reserve_floor = balance * (self._reserve_pct / Decimal("100"))
            if free_cash < reserve_floor:
                msg = (
                    f"cash_reserve_breach: free_cash={free_cash} < reserve_floor={reserve_floor} "
                    f"(balance={balance}, proposed={proposed_cost})"
                )
                logger.warning("CASH RESERVE GUARD: %s", msg)
                return msg
            return None
        except Exception as exc:
            logger.exception("error in cash reserve check")
            return f"cash_reserve_check_failed: {exc}"

    async def get_available_cash(self) -> Decimal:
        balance = await self._get_balance()
        pending_total = sum(self._pending_buy_costs.values(), Decimal("0"))
        available = balance - pending_total
        return max(available, Decimal("0"))

    def track_pending_buy(self, order_id: str, cost: Decimal) -> None:
        self._pending_buy_costs[order_id] = cost
        self._state.pending_buy_orders = json.dumps(
            {k: str(v) for k, v in self._pending_buy_costs.items()}
        )

    def untrack_pending_buy(self, order_id: str) -> None:
        self._pending_buy_costs.pop(order_id, None)
        self._state.pending_buy_orders = json.dumps(
            {k: str(v) for k, v in self._pending_buy_costs.items()}
        )

    async def get_exposure_by_market(self, asset_id: str) -> Decimal:
        positions = json.loads(self._state.open_positions)
        asset_exposure = Decimal(str(positions.get(asset_id, "0")))

        for oid, cost in self._pending_buy_costs.items():
            asset_exposure += cost

        return asset_exposure

    async def get_total_exposure(self) -> Decimal:
        positions = json.loads(self._state.open_positions)
        total = sum(Decimal(str(v)) for v in positions.values())

        total += sum(self._pending_buy_costs.values(), Decimal("0"))

        return total

    async def check_exposure_per_market(self, asset_id: str, proposed_cost: Decimal) -> str | None:
        try:
            balance = await self._get_balance()
            if balance <= 0:
                return None

            current_exposure = await self.get_exposure_by_market(asset_id)
            new_exposure = current_exposure + proposed_cost
            max_exposure = balance * (self._max_exposure_per_market_pct / Decimal("100"))

            if new_exposure > max_exposure:
                msg = (
                    f"exposure_per_market_exceeded: {new_exposure} > {max_exposure} "
                    f"(asset={asset_id}, current={current_exposure}, proposed={proposed_cost})"
                )
                logger.warning("EXPOSURE LIMIT: %s", msg)
                return msg

            return None
        except Exception as exc:
            logger.exception("error in exposure per market check")
            return f"exposure_per_market_check_failed: {exc}"

    async def check_total_exposure(self, proposed_cost: Decimal) -> str | None:
        try:
            balance = await self._get_balance()
            if balance <= 0:
                return None

            current_total = await self.get_total_exposure()
            new_total = current_total + proposed_cost
            max_total = balance * (self._max_total_exposure_pct / Decimal("100"))

            if new_total > max_total:
                msg = (
                    f"exposure_total_exceeded: {new_total} > {max_total} "
                    f"(current={current_total}, proposed={proposed_cost})"
                )
                logger.warning("TOTAL EXPOSURE LIMIT: %s", msg)
                return msg

            return None
        except Exception as exc:
            logger.exception("error in total exposure check")
            return f"exposure_total_check_failed: {exc}"

    def record_failure(self) -> None:
        now = time.time()
        self._failure_timestamps.append(now)

        cutoff = now - self._failure_window_seconds
        while self._failure_timestamps and self._failure_timestamps[0] < cutoff:
            self._failure_timestamps.popleft()

        if len(self._failure_timestamps) >= self._max_consecutive_failures:
            self._cooldown_until = now + self._cooldown_seconds
            logger.critical(
                "FAILURE COOLDOWN ACTIVATED: %d failures in %ds — paused until %.0f",
                len(self._failure_timestamps), self._failure_window_seconds,
                self._cooldown_until,
            )

    async def check_failure_cooldown(self) -> str | None:
        now = time.time()
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            return (
                f"failure_cooldown_active: {remaining:.0f}s remaining "
                f"(failures={len(self._failure_timestamps)} in {self._failure_window_seconds}s)"
            )
        return None

    async def check_order_timeout(self, order_id: str, age_seconds: float) -> str | None:
        if age_seconds > STALE_ORDER_AGE_S:
            msg = f"order_timeout: {order_id} age={age_seconds:.0f}s > {STALE_ORDER_AGE_S}s"
            logger.warning("ORDER TIMEOUT: %s", msg)
            if self._cancel_order_cb:
                try:
                    await self._cancel_order_cb(order_id)
                except Exception:
                    logger.exception("error cancelling timed-out order %s", order_id)
            return msg
        return None

    async def cancel_all_orders(self) -> None:
        logger.warning("cancel_all_orders invoked")
        if self._cancel_all_cb:
            try:
                await self._cancel_all_cb()
            except Exception:
                logger.exception("error in cancel_all callback")

    async def _block_trading(self, reason: BlockReason, detail: str = "") -> None:
        self._state.blocked = True
        self._state.block_reason = reason.value
        self._state.blocked_at = datetime.now(timezone.utc).isoformat()
        self._status = BotStatus.BLOCKED
        await self._persist()
        await self.cancel_all_orders()
        self._emit_alert("CRITICAL", f"Trading BLOCKED: {reason.value} — {detail}")

    async def unblock_trading(self) -> None:
        self._state.blocked = False
        self._state.block_reason = "none"
        self._state.blocked_at = ""
        self._status = BotStatus.HEALTHY
        self._cooldown_until = 0.0
        await self._persist()
        logger.info("trading unblocked manually")

    async def unblock_total_drawdown(self) -> None:
        self._state.total_drawdown_blocked = False
        self._state.total_drawdown_peak_balance = Decimal("0")
        self._state.total_drawdown_peak_ts = ""
        self._state.total_drawdown_blocked_at = ""
        self._state.blocked = False
        self._state.block_reason = "none"
        self._state.blocked_at = ""
        self._status = BotStatus.HEALTHY
        await self._persist()
        logger.warning("total drawdown block cleared MANUALLY — user override")

    async def is_trading_blocked(self) -> tuple[bool, str]:
        if self._state.total_drawdown_blocked:
            return True, f"total_drawdown_permanently_blocked_{self._state.total_drawdown_blocked_at}"

        if self._state.blocked:
            return True, self._state.block_reason

        dd = await self.check_drawdown()
        if dd is not None:
            return True, dd

        tdd = await self.check_total_drawdown()
        if tdd is not None:
            return True, tdd

        dl = await self.check_daily_loss()
        if dl is not None:
            return True, dl

        fc = await self.check_failure_cooldown()
        if fc is not None:
            return True, fc

        return False, "ok"

    async def check_all_breakers(
        self,
        asset_id: str,
        proposed_cost: Decimal,
    ) -> tuple[bool, str]:
        tdd = await self.check_total_drawdown()
        if tdd is not None:
            return False, tdd

        if self._state.blocked:
            return False, self._state.block_reason

        dd = await self.check_drawdown()
        if dd is not None:
            return False, dd

        dl = await self.check_daily_loss()
        if dl is not None:
            return False, dl

        fc = await self.check_failure_cooldown()
        if fc is not None:
            return False, fc

        cr = await self.check_cash_reserve(proposed_cost)
        if cr is not None:
            return False, cr

        em = await self.check_exposure_per_market(asset_id, proposed_cost)
        if em is not None:
            return False, em

        te = await self.check_total_exposure(proposed_cost)
        if te is not None:
            return False, te

        return True, "ok"

    def get_status(self) -> BotStatus:
        return self._status

    def get_state_snapshot(self) -> dict[str, Any]:
        s = self._state
        return {
            "status": self._status.name,
            "blocked": s.blocked,
            "block_reason": s.block_reason,
            "high_water_mark": str(s.high_water_mark),
            "daily_start_balance": str(s.daily_start_balance),
            "daily_loss_accrued": str(s.daily_loss_accrued),
            "daily_loss_pct": (
                str((s.daily_loss_accrued / s.daily_start_balance * Decimal("100")).quantize(Decimal("0.01")))
                if s.daily_start_balance > 0 else "0.00"
            ),
            "total_orders_placed": s.total_orders_placed,
            "total_orders_filled": s.total_orders_filled,
            "total_pnl": str(s.total_pnl),
            "total_drawdown_blocked": s.total_drawdown_blocked,
            "total_drawdown_peak_balance": str(s.total_drawdown_peak_balance),
            "total_drawdown_blocked_at": s.total_drawdown_blocked_at,
            "cooldown_active": time.time() < self._cooldown_until if hasattr(self, "_cooldown_until") else False,
            "cooldown_remaining_s": max(0, self._cooldown_until - time.time()) if hasattr(self, "_cooldown_until") else 0,
            "failure_count": len(self._failure_timestamps) if hasattr(self, "_failure_timestamps") else 0,
        }

    def _emit_alert(self, severity: str, message: str) -> None:
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "source": "circuit_breaker",
            "message": message,
        }
        logger.critical("ALERT: %s — %s", severity, json.dumps(alert))

    async def on_ws_disconnect(self) -> None:
        logger.warning("WS disconnect detected — starting grace timer %ds", WS_DISCONNECT_GRACE_S)
        await asyncio.sleep(WS_DISCONNECT_GRACE_S)
        logger.critical("WS grace period expired — cancelling all orders")
        await self.cancel_all_orders()
        await self._block_trading(BlockReason.WS_DISCONNECT, "WS disconnected > grace period")

    async def on_ws_reconnect(self) -> None:
        self._status = BotStatus.HEALTHY
        if self._state.block_reason == BlockReason.WS_DISCONNECT.value and self._state.blocked:
            self._state.blocked = False
            self._state.block_reason = "none"
            await self._persist()
            logger.info("WS reconnected — trading unblocked")

    async def start(self) -> None:
        await self._ensure_db()
        await self._load_state()
        await self._apply_daily_loss_recovery()
        logger.info(
            "CircuitBreakerManager started: blocked=%s reason=%s total_dd_blocked=%s",
            self._state.blocked, self._state.block_reason,
            self._state.total_drawdown_blocked,
        )

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
        logger.info("CircuitBreakerManager stopped")

    async def startup_cancel_all(self) -> None:
        logger.info("startup: cancelling all residual orders")
        await self.cancel_all_orders()
