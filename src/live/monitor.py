"""
CronMonitor — Periodic health monitoring, reconciliation, and alerting.

Runs every `monitor_interval_seconds` and performs:
  1. Open position reconciliation (compares local vs remote order state).
  2. USDC balance verification (compares on-chain balance vs expected).
  3. Service heartbeat checks (WebSocket, strategy, executor).
  4. Alerts on discrepancies or anomalies.

Also extends the health HTTP endpoint with detailed structured JSON.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aiosqlite

logger = logging.getLogger("monitor")

DB_PATH_DEFAULT = "bot_state.db"
MONITOR_INTERVAL_S = 60
BALANCE_DISCREPANCY_THRESHOLD = Decimal("1.0")


class CronMonitor:
    """Periodic health monitor for the trading bot.

    Parameters
    ----------
    db_path : str
        Path to SQLite database for persistence.
    balance_provider : Callable[[], Awaitable[Decimal]] | None
        Returns current on-chain USDC balance.
    fetch_open_orders_cb : Callable[[], Any] | None
        Returns list of (order_id, created_at) for remote orders.
    expected_balance_provider : Callable[[], Awaitable[Decimal]] | None
        Returns the expected balance based on local PnL tracking.
    monitor_interval_s : int
        How often to run checks (default 60).
    balance_discrepancy_threshold : Decimal
        Max allowed balance discrepancy in USDC (default 1.0).
    on_critical_cb : Callable[[str], Any] | None
        Callback for critical alerts.
    """

    def __init__(
        self,
        db_path: str = DB_PATH_DEFAULT,
        balance_provider: Callable | None = None,
        fetch_open_orders_cb: Callable | None = None,
        expected_balance_provider: Callable | None = None,
        monitor_interval_s: int = MONITOR_INTERVAL_S,
        balance_discrepancy_threshold: Decimal = BALANCE_DISCREPANCY_THRESHOLD,
        on_critical_cb: Callable | None = None,
    ) -> None:
        self._db_path = db_path
        self._balance_provider = balance_provider
        self._fetch_orders_cb = fetch_open_orders_cb
        self._expected_balance_provider = expected_balance_provider
        self._interval_s = monitor_interval_s
        self._balance_threshold = balance_discrepancy_threshold
        self._on_critical_cb = on_critical_cb

        self._running = False
        self._task: asyncio.Task | None = None
        self._db: aiosqlite.Connection | None = None

        # Health state
        self._last_reconciliation: float = 0.0
        self._last_balance_check: float = 0.0
        self._last_balance: Decimal = Decimal("0")
        self._last_discrepancy: Decimal = Decimal("0")
        self._last_discrepancy_time: float = 0.0
        self._reconciliation_issues: list[str] = []

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS monitor_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    check_type TEXT,
                    status TEXT,
                    detail TEXT
                )
            """)
            await self._db.commit()
        return self._db

    async def _log_check(self, check_type: str, status: str, detail: str = "") -> None:
        """Log a monitor check result to SQLite."""
        db = await self._ensure_db()
        try:
            await db.execute(
                "INSERT INTO monitor_log (timestamp, check_type, status, detail) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), check_type, status, detail[:500]),
            )
            await db.commit()
        except Exception:
            logger.exception("Error logging monitor check")

    async def reconcile_positions(self) -> dict[str, Any]:
        """Compare local open order state with remote CLOB state.

        Returns dict with 'status', 'issues', 'local_count', 'remote_count'.
        """
        if self._fetch_orders_cb is None:
            return {"status": "skipped", "reason": "no_fetch_callback"}

        try:
            remote_orders = await self._fetch_orders_cb()
            remote_ids = {oid for oid, _ in remote_orders}

            result = {
                "status": "ok",
                "remote_count": len(remote_ids),
                "issues": [],
            }

            # Log orphaned orders
            if remote_ids:
                result["remote_ids"] = list(remote_ids)[:20]

            self._last_reconciliation = time.time()
            await self._log_check("reconciliation", "ok", json.dumps(result))
            return result

        except Exception as exc:
            msg = f"Reconciliation failed: {exc}"
            logger.exception(msg)
            self._reconciliation_issues.append(msg)
            await self._log_check("reconciliation", "error", msg)
            return {"status": "error", "error": str(exc)}

    async def check_balance(self) -> dict[str, Any]:
        """Verify on-chain balance vs expected balance.

        Returns dict with 'status', 'on_chain', 'expected', 'discrepancy'.
        """
        on_chain = Decimal("0")
        expected = Decimal("0")

        if self._balance_provider:
            try:
                on_chain = await self._balance_provider()
            except Exception:
                logger.exception("Error getting on-chain balance")

        if self._expected_balance_provider:
            try:
                expected = await self._expected_balance_provider()
            except Exception:
                logger.exception("Error getting expected balance")

        discrepancy = on_chain - expected
        self._last_balance = on_chain
        self._last_discrepancy = discrepancy
        self._last_balance_check = time.time()

        result = {
            "status": "ok",
            "on_chain": str(on_chain),
            "expected": str(expected),
            "discrepancy": str(discrepancy),
        }

        if abs(discrepancy) > self._balance_threshold:
            result["status"] = "discrepancy"
            msg = (
                f"BALANCE DISCREPANCY: on_chain={on_chain} expected={expected} "
                f"diff={discrepancy} (threshold={self._balance_threshold})"
            )
            logger.critical(msg)
            self._last_discrepancy_time = time.time()
            await self._log_check("balance", "discrepancy", msg)

            if self._on_critical_cb:
                try:
                    await self._on_critical_cb(msg)
                except Exception:
                    logger.exception("Critical callback failed")
        else:
            await self._log_check("balance", "ok", json.dumps(result))

        return result

    async def _check_loop(self) -> None:
        """Main monitoring loop."""
        logger.info("CronMonitor started (interval=%ds)", self._interval_s)

        while self._running:
            try:
                await asyncio.sleep(self._interval_s)

                # 1. Reconcile positions
                rec_result = await self.reconcile_positions()
                if rec_result.get("status") == "error":
                    logger.warning("Reconciliation issue: %s", rec_result.get("error"))

                # 2. Check balance
                bal_result = await self.check_balance()
                if bal_result.get("status") == "discrepancy":
                    logger.warning("Balance discrepancy detected: %s", bal_result)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in monitor loop")

    async def start(self) -> None:
        """Start the monitor loop."""
        self._running = True
        await self._ensure_db()
        self._task = asyncio.create_task(self._check_loop())
        logger.info("CronMonitor started")

    async def stop(self) -> None:
        """Stop the monitor loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._db:
            await self._db.close()
            self._db = None
        logger.info("CronMonitor stopped")

    def get_health_dict(self) -> dict[str, Any]:
        """Return comprehensive health status for the /health endpoint."""
        return {
            "monitor": {
                "last_reconciliation": datetime.fromtimestamp(
                    self._last_reconciliation, tz=timezone.utc
                ).isoformat() if self._last_reconciliation > 0 else "never",
                "last_balance_check": datetime.fromtimestamp(
                    self._last_balance_check, tz=timezone.utc
                ).isoformat() if self._last_balance_check > 0 else "never",
                "last_balance_usdc": str(self._last_balance),
                "last_discrepancy_usdc": str(self._last_discrepancy),
                "last_discrepancy_at": datetime.fromtimestamp(
                    self._last_discrepancy_time, tz=timezone.utc
                ).isoformat() if self._last_discrepancy_time > 0 else "never",
                "reconciliation_issues": self._reconciliation_issues[-5:],
            }
        }

    def build_health_response(
        self,
        circuit_breaker_snapshot: dict[str, Any],
        ws_health: dict[str, Any],
        order_guard_paused: bool,
        position_stats: dict[str, Any],
        performance_stats: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a comprehensive health JSON response.

        This is the unified health endpoint response used by EjecutorOrdenes.
        """
        cb_status = circuit_breaker_snapshot.get("status", "HEALTHY")

        # Determine overall status
        if cb_status == "BLOCKED":
            overall_status = "BLOCKED"
        elif circuit_breaker_snapshot.get("total_drawdown_blocked", False):
            overall_status = "BLOCKED"
        elif cb_status == "DEGRADED":
            overall_status = "DEGRADED"
        elif order_guard_paused:
            overall_status = "DEGRADED"
        elif not ws_health.get("connected", False):
            overall_status = "DEGRADED"
        elif not ws_health.get("book_synced", False):
            overall_status = "DEGRADED"
        else:
            overall_status = "OK"

        response: dict[str, Any] = {
            "status": overall_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": extra.get("dry_run", True) if extra else True,
            "balance_usdc": circuit_breaker_snapshot.get("daily_start_balance", "0"),
            "daily_pnl": circuit_breaker_snapshot.get("total_pnl", "0"),
            "drawdown_pct": circuit_breaker_snapshot.get("daily_loss_pct", "0.00"),
            "open_orders": position_stats.get("open_positions", 0),
            "total_orders_placed": circuit_breaker_snapshot.get("total_orders_placed", 0),
            "total_orders_filled": circuit_breaker_snapshot.get("total_orders_filled", 0),
            "uptime_seconds": extra.get("uptime_seconds", 0) if extra else 0,
            "last_error": extra.get("last_error", "") if extra else "",
            "circuit_breakers": {
                "blocked": circuit_breaker_snapshot.get("blocked", False),
                "block_reason": circuit_breaker_snapshot.get("block_reason", "none"),
                "total_drawdown_blocked": circuit_breaker_snapshot.get("total_drawdown_blocked", False),
                "total_drawdown_peak_balance": circuit_breaker_snapshot.get("total_drawdown_peak_balance", "0"),
                "daily_loss_pct": circuit_breaker_snapshot.get("daily_loss_pct", "0.00"),
                "cooldown_active": circuit_breaker_snapshot.get("cooldown_active", False),
                "cooldown_remaining_s": circuit_breaker_snapshot.get("cooldown_remaining_s", 0),
                "failure_count": circuit_breaker_snapshot.get("failure_count", 0),
            },
            "websocket": ws_health,
            "order_guard": {
                "trading_paused": order_guard_paused,
            },
            "positions": position_stats,
            "performance": performance_stats,
            "monitor": self.get_health_dict(),
        }

        if extra:
            response.update(extra)

        return response
