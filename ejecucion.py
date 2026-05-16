"""
Módulo C — Ejecución de órdenes en el CLOB de Polymarket (MODO LIVE).

Consume señales del Módulo B (estrategia), construye órdenes EIP-712 V2,
las firma con la clave privada de la wallet y las envía al CLOB
vía REST API con autenticación L2.

Incorporaciones para modo live:
  - CircuitBreakerManager (drawdown kill-switch, daily loss, cash reserve)
  - OrderLifecycleManager (timeout-safe placement, stale order cleanup)
  - Dynamic fee calculation (Polymarket 2026 formula)
  - Opportunity windows (Endcycle Sniper pattern)
  - Health HTTP endpoint + Prometheus metrics
  - Startup reconciliation + graceful shutdown
  - Structured JSON logging (no float in trading paths)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time
import base64
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from src.risk.circuit_breakers import CircuitBreakerManager, BotStatus
from src.execution.order_lifecycle import OrderLifecycleManager, OrderOpResult, timeout_cycle
from src.config.live_settings import (
    RISK,
    OPPORTUNITY_WINDOWS,
    dynamic_taker_fee,
    estimate_post_fee_edge,
    position_size_from_score,
    get_live_config,
)
from src.live.order_guard import OrderGuard
from src.live.market_filter import MarketQualifier
from src.live.position_manager import PositionManager
from src.live.performance_tracker import PerformanceTracker
from src.live.monitor import CronMonitor
from src.live.alerting import AlertManager

logger = logging.getLogger("ejecucion")

# ── Constantes de red ──────────────────────────────────────────────────
CHAIN_ID = 137
CLOB_API_BASE = os.getenv("CLOB_API_BASE", "https://clob.polymarket.com")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

USDC_DECIMALS = 6
TOKEN_DECIMALS = 6

ORDER_DOMAIN_NAME = "Polymarket CTF Exchange"
ORDER_DOMAIN_VERSION = "2"
ORDER_PRIMARY_TYPE = "Order"
EIP712_ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
    ],
}
BYTES32_ZERO = "0x" + "00" * 32

SIGNAL_SIDE_MAP = {
    "BUY_YES": "BUY",
    "BUY_NO": "BUY",
    "SELL_YES": "SELL",
    "SELL_NO": "SELL",
}

ERC20_BALANCE_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf",'
    '"outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]'
)

ERC20_ALLOWANCE_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"_owner","type":"address"},'
    '{"name":"_spender","type":"address"}],"name":"allowance",'
    '"outputs":[{"name":"remaining","type":"uint256"}],"type":"function"}]'
)

# Default config from live_settings
LIVE_CONFIG = get_live_config()
RISK_CFG = LIVE_CONFIG["risk"]


def _generate_salt() -> int:
    return int(time.time_ns() // 1000)


def price_to_tick_price(price: Decimal, tick_size: Decimal) -> int:
    return int(price.quantize(tick_size, rounding=ROUND_DOWN) / tick_size)


def size_to_token_amount(size: Decimal, decimals: int = TOKEN_DECIMALS) -> int:
    return int(size * Decimal(10 ** decimals))


class EjecutorOrdenes:
    """Ejecutor de órdenes para Polymarket CLOB con protecciones live.

    Parameters
    ----------
    signal_queue : asyncio.Queue
        Cola de entrada con señales del Módulo B (dict).
    dry_run : bool
        Si es True, las órdenes se simulan sin enviar al CLOB.
    execution_log_queue : asyncio.Queue | None
        Cola opcional para enviar registros al Módulo D (archivo).
    db_path : str
        Ruta a SQLite para persistencia de circuit breakers.
    """

    def __init__(
        self,
        signal_queue: asyncio.Queue,
        dry_run: bool = False,
        execution_log_queue: asyncio.Queue | None = None,
        db_path: str = "bot_state.db",
    ) -> None:
        self.signal_queue = signal_queue
        self.dry_run = dry_run
        self.execution_log_queue = execution_log_queue

        # Wallet desde PRIVATE_KEY
        raw_key = os.environ.get("PRIVATE_KEY", "")
        if not raw_key:
            if dry_run:
                raw_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
            else:
                raise KeyError("PRIVATE_KEY must be set in environment for live trading")
        if not raw_key.startswith("0x"):
            raw_key = "0x" + raw_key
        self._private_key = raw_key
        self._account = Account.from_key(raw_key)
        self._wallet_address = self._account.address

        # L2 credentials
        self._api_key = os.environ.get("POLYMARKET_API_KEY", "")
        self._api_secret = os.environ.get("POLYMARKET_SECRET", "")
        self._api_passphrase = os.environ.get("POLYMARKET_PASSPHRASE", "")

        # Web3 for blockchain queries
        self._w3: Web3 | None = None
        if not dry_run:
            self._w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))

        # Circuit breaker state from old impl (kept for compatibility)
        self._max_daily_loss_pct = Decimal(str(RISK_CFG["max_daily_loss_pct"]))
        self._max_orders_per_minute = 50
        self._max_gas_price_gwei = 200
        self._max_error_rate_pct = Decimal("10")
        self._error_rate_window = 50
        self._profit_window_secs = 86400

        self._daily_pnl: Decimal = Decimal("0")
        self._daily_start_balance: Decimal | None = None
        self._order_timestamps: deque[float] = deque()
        self._order_results: deque[bool] = deque(maxlen=self._error_rate_window)
        self._running = False
        self._semaphore = asyncio.Semaphore(1)
        self._last_balance_check: float = 0
        self._cached_balance: Decimal = Decimal("0")

        # ── CircuitBreakerManager (3-layer protection) ─────────────────
        self._circuit_breaker = CircuitBreakerManager(
            db_path=db_path,
            balance_provider=self._get_usdc_balance,
            inventory_mtm_provider=self._get_inventory_mtm,
            cancel_all_cb=self._cancel_all_orders_cb,
            fetch_open_orders_cb=self._fetch_open_orders_cb,
            cancel_order_cb=self._cancel_order_cb,
            reserve_pct=RISK_CFG["cash_reserve_pct"],
            max_drawdown_pct=RISK_CFG["max_drawdown_pct"],
            max_daily_loss_pct=RISK_CFG["max_daily_loss_pct"],
        )

        # ── OrderLifecycleManager (timeout-safe placement) ────────────
        self._order_lifecycle = OrderLifecycleManager(
            place_order_func=self._send_order_raw,
            cancel_all_func=self._cancel_all_orders_cb,
            cancel_order_func=self._cancel_order_cb,
            fetch_open_orders_func=self._fetch_open_orders_cb,
            op_timeout_s=float(RISK_CFG["op_timeout_s"]),
            cycle_timeout_s=float(RISK_CFG["cycle_timeout_s"]),
            max_retries=int(RISK_CFG["max_retries"]),
            stale_max_age_s=int(RISK_CFG["stale_order_max_age_s"]),
        )

        # HTTP session for CLOB API
        self._session: aiohttp.ClientSession | None = None

        # Health check HTTP server
        self._health_server: asyncio.Server | None = None
        self._health_port = int(os.getenv("HEALTH_PORT", "8080"))

        # Prometheus metrics (lazy init)
        self._prom_gauges: dict[str, Any] = {}

        # Open order tracking for reconciliation
        self._open_orders: dict[str, dict[str, Any]] = {}

        # Market metadata for opportunity windows
        self._market_meta: dict[str, dict[str, Any]] = {}

        # Track last trade time for idle alert
        self._last_trade_time: float = 0.0

        # Track last error for health endpoint
        self._last_error: str = ""

        # WebSocket health (updated by ingesta)
        self._ws_health: dict[str, Any] = {
            "connected": False,
            "book_synced": False,
            "syncing": False,
        }

        # ── v2: OrderGuard ─────────────────────────────────────────────
        self._order_guard = OrderGuard(
            cancel_all_cb=self._cancel_all_orders_cb,
            cancel_order_cb=self._cancel_order_cb,
            fetch_open_orders_cb=self._fetch_open_orders_cb,
            max_order_age_s=int(RISK_CFG.get("stale_order_max_age_s", 120)),
        )

        # ── v2: PerformanceTracker ──────────────────────────────────────
        self._performance_tracker = PerformanceTracker(
            db_path=db_path,
            base_min_edge=RISK_CFG.get("base_min_edge", Decimal("0.05")),
            mae_adjustment_factor=RISK_CFG.get("mae_adjustment_factor", Decimal("1.5")),
            max_min_edge=RISK_CFG.get("max_min_edge", Decimal("0.15")),
        )

        # ── v2: MarketQualifier ─────────────────────────────────────────
        self._market_qualifier = MarketQualifier(
            min_prob=RISK_CFG.get("min_prob", Decimal("0.30")),
            max_prob=RISK_CFG.get("max_prob", Decimal("0.70")),
            min_volume_24h=RISK_CFG.get("min_volume_24h", Decimal("5000")),
            min_hours_to_resolution=int(RISK_CFG.get("min_hours_to_resolution", 336)),
            opportunity_windows=OPPORTUNITY_WINDOWS,
            dynamic_min_edge_provider=lambda: self._performance_tracker.adjusted_min_edge,
        )

        # ── v2: PositionManager ─────────────────────────────────────────
        self._position_manager = PositionManager(
            force_close_cb=self._handle_force_close,
            take_profit_pct=RISK_CFG.get("take_profit_pct", Decimal("50.0")),
            stop_loss_pct=RISK_CFG.get("stop_loss_pct", Decimal("30.0")),
            max_position_age_cycles=int(RISK_CFG.get("max_position_age_cycles", 3)),
            cycle_duration_minutes=int(RISK_CFG.get("cycle_duration_minutes", 15)),
            price_provider=self._get_price_for_asset,
        )

        # ── v2: CronMonitor ─────────────────────────────────────────────
        self._cron_monitor = CronMonitor(
            db_path=db_path,
            balance_provider=self._get_usdc_balance,
            fetch_open_orders_cb=self._fetch_open_orders_cb,
            expected_balance_provider=self._get_expected_balance,
            monitor_interval_s=int(RISK_CFG.get("monitor_interval_seconds", 60)),
            balance_discrepancy_threshold=RISK_CFG.get(
                "balance_discrepancy_threshold_usdc", Decimal("1.0")
            ),
            on_critical_cb=self._on_critical_alert,
        )

        # ── v2: AlertManager ────────────────────────────────────────────
        alert_cfg = LIVE_CONFIG.get("alerting", {})
        self._alert_manager = AlertManager(
            discord_webhook_url=alert_cfg.get("discord_webhook_url", ""),
            telegram_bot_token=alert_cfg.get("telegram_bot_token", ""),
            telegram_chat_id=alert_cfg.get("telegram_chat_id", ""),
            alert_on_critical=alert_cfg.get("alert_on_critical", True),
            alert_on_warning=alert_cfg.get("alert_on_warning", False),
        )

    # ── v2: Callbacks for new modules ──────────────────────────────────

    async def _handle_force_close(self, close_signal: dict[str, Any]) -> None:
        """Handle a force-close signal from PositionManager.

        This is a high-priority signal that must be executed immediately.
        It bypasses market filters but still checks circuit breakers.
        """
        logger.critical(
            "FORCE CLOSE signal received: asset=%s reason=%s",
            close_signal.get("asset_id"), close_signal.get("force_close_reason"),
        )
        # Place as a regular order with high priority bypassing market filters
        await self._place_order_from_signal(close_signal, bypass_filters=True)

    async def _get_price_for_asset(self, asset_id: str) -> Decimal | None:
        """Get current market price for an asset (used by PositionManager).

        Returns the mid-price or None if unavailable.
        """
        # In a full implementation, this would query the CLOB mid-price
        # For now, returns None (TP/SL via price_provider disabled)
        return None

    async def _get_expected_balance(self) -> Decimal:
        """Calculate expected balance based on initial + PnL.

        Used by CronMonitor for balance discrepancy checks.
        """
        if self._daily_start_balance is None:
            return Decimal("0")
        return self._daily_start_balance + self._daily_pnl

    async def _on_critical_alert(self, message: str) -> None:
        """Handle critical alerts from CronMonitor.

        Sends alert via AlertManager and logs.
        """
        logger.critical("CRITICAL ALERT from monitor: %s", message)
        await self._alert_manager.send_alert(
            severity="CRITICAL",
            title="Monitor Alert",
            message=message,
            alert_type="monitor_critical",
        )

    # ── HTTP Session ───────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=float(RISK_CFG["op_timeout_s"]))
            self._session = aiohttp.ClientSession(
                base_url=CLOB_API_BASE,
                timeout=timeout,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── L2 Authentication ──────────────────────────────────────────────

    def _build_l2_headers(
        self, method: str, path: str, body: str = ""
    ) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method}{path}{body}"
        sig = hmac.new(
            base64.b64decode(self._api_secret),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return {
            "POLY_ADDRESS": self._wallet_address,
            "POLY_SIGNATURE": base64.b64encode(sig).decode(),
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self._api_key,
            "POLY_PASSPHRASE": self._api_passphrase,
        }

    # ── Balance & MTM ─────────────────────────────────────────────────

    async def _get_usdc_balance(self) -> Decimal:
        now = time.time()
        if now - self._last_balance_check < 30:
            return self._cached_balance
        if self._w3 is None:
            return Decimal("0")
        try:
            contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(COLLATERAL_TOKEN),
                abi=ERC20_BALANCE_ABI,
            )
            balance_wei = await asyncio.to_thread(
                contract.functions.balanceOf(
                    Web3.to_checksum_address(self._wallet_address)
                ).call
            )
            balance = Decimal(str(balance_wei)) / Decimal(10 ** USDC_DECIMALS)
            self._cached_balance = balance
            self._last_balance_check = now
            return balance
        except Exception:
            logger.exception("error obteniendo balance USDC")
            return self._cached_balance

    async def _get_inventory_mtm(self) -> Decimal:
        """Mark-to-market value of open positions.

        In a full implementation, this would query the CLOB for open
        orders and mark them to the current mid-price. For now returns 0
        since positions resolve to 0 or 1 at settlement.
        """
        return Decimal("0")

    async def _get_gas_price_gwei(self) -> int:
        if self._w3 is None:
            return 0
        try:
            price_wei = await asyncio.to_thread(self._w3.eth.gas_price)
            return int(Web3.from_wei(price_wei, "gwei"))
        except Exception:
            logger.exception("error obteniendo gas price")
            return 0

    # ── Open Orders API (for stale checker & reconciliation) ───────────

    async def _fetch_open_orders_cb(self) -> list[tuple[str, float]]:
        """Fetch open orders from CLOB. Returns [(order_id, created_at), ...]."""
        if self.dry_run:
            return list(self._open_orders.items())

        try:
            session = await self._get_session()
            headers = self._build_l2_headers("GET", "/orders")
            async with session.get(
                "/orders",
                headers=headers,
                params={"status": "OPEN"},
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                orders: list[tuple[str, float]] = []
                for o in data.get("data", []):
                    oid = o.get("id", "")
                    ts = int(o.get("timestamp", "0")) / 1000
                    orders.append((oid, ts))
                return orders
        except Exception:
            logger.exception("error fetching open orders")
            return []

    async def _cancel_all_orders_cb(self) -> None:
        """Cancel ALL open orders via CLOB API."""
        if self.dry_run:
            self._open_orders.clear()
            logger.info("[DRY-RUN] cancel_all orders")
            return

        try:
            session = await self._get_session()
            headers = self._build_l2_headers("DELETE", "/orders")
            async with session.delete("/orders", headers=headers) as resp:
                if resp.status == 200:
                    logger.info("cancel_all OK (%d)", resp.status)
                else:
                    text = await resp.text()
                    logger.warning("cancel_all status=%d: %.200s", resp.status, text)
        except Exception:
            logger.exception("error in cancel_all")

    async def _cancel_order_cb(self, order_id: str) -> None:
        """Cancel a single order by ID."""
        if self.dry_run:
            self._open_orders.pop(order_id, None)
            return

        try:
            session = await self._get_session()
            headers = self._build_l2_headers("DELETE", f"/orders/{order_id}")
            async with session.delete(
                f"/orders/{order_id}", headers=headers
            ) as resp:
                if resp.status == 200:
                    logger.info("cancelled order %s", order_id)
                else:
                    logger.warning("cancel %s status=%d", order_id, resp.status)
        except Exception:
            logger.exception("error cancelling order %s", order_id)

    # ── Legacy Circuit Breakers (pre-checks) ──────────────────────────

    async def _check_circuits(self) -> str | None:
        """Pre-flight circuit breaker check before each order.

        Returns None if OK, or error string if blocked.
        """
        gas = await self._get_gas_price_gwei()
        if gas > self._max_gas_price_gwei:
            return f"gas_price_exceeded: {gas} > {self._max_gas_price_gwei} Gwei"

        # Check 3-layer protection
        blocked, reason = await self._circuit_breaker.is_trading_blocked()
        if blocked:
            return f"circuit_breaker_blocked: {reason}"

        balance = await self._get_usdc_balance()
        if self._daily_start_balance is None:
            self._daily_start_balance = balance

        if self._daily_start_balance > 0:
            loss_pct = (
                (-self._daily_pnl) / self._daily_start_balance
            ) * Decimal("100")
            if loss_pct >= self._max_daily_loss_pct:
                return f"max_daily_loss: {loss_pct:.2f}%"

        now = time.time()
        while self._order_timestamps and now - self._order_timestamps[0] > 60:
            self._order_timestamps.popleft()
        if len(self._order_timestamps) >= self._max_orders_per_minute:
            return f"order_rate_limit: {len(self._order_timestamps)}/min"

        if len(self._order_results) >= self._error_rate_window:
            errors = sum(1 for ok in self._order_results if not ok)
            error_pct = Decimal(str(errors)) / Decimal(str(len(self._order_results))) * Decimal("100")
            if error_pct > self._max_error_rate_pct:
                return f"error_rate: {error_pct:.2f}%"

        return None

    # ── Market Filters (probability, volume, exclusion) ───────────────

    def _market_is_excluded(self, probability: Decimal) -> bool:
        """Check if market probability is in the excluded range."""
        return (
            probability <= RISK_CFG["exclude_prob_below"]
            or probability >= RISK_CFG["exclude_prob_above"]
        )

    def _check_opportunity_window(self, asset_id: str, market_type: str = "default") -> bool:
        """Check if we are within the opportunity window for this market type."""
        meta = self._market_meta.get(asset_id, {})
        end_time_s = meta.get("end_time_s", 0)
        if end_time_s <= 0:
            return True  # no window info available

        remaining_s = end_time_s - time.time()
        window = OPPORTUNITY_WINDOWS.get(market_type, OPPORTUNITY_WINDOWS["default"])
        window_s = window["window_before_end_s"]

        if remaining_s > window_s:
            logger.debug(
                "outside opportunity window for %s: remaining=%.0fs > window=%ds",
                asset_id, remaining_s, window_s,
            )
            return False

        return True

    def _estimate_fee_and_edge(
        self,
        probability: Decimal,
        current_price: Decimal,
        size: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Estimate post-fee edge.

        Returns (fee_rate, post_fee_edge).
        """
        fee_rate = dynamic_taker_fee(current_price)
        post_edge = estimate_post_fee_edge(probability, current_price, size)
        return fee_rate, post_edge

    # ── Order Construction (EIP-712 V2) ───────────────────────────────

    def _build_order_payload(self, signal: dict) -> tuple[dict[str, Any], str]:
        """Build order data dict from signal. Uses Decimal for ALL values."""
        side_raw = signal["side"]
        side = SIGNAL_SIDE_MAP.get(side_raw, "BUY")
        side_int = 0 if side == "BUY" else 1

        # Decimal from string — NEVER from float
        price = Decimal(str(signal["price"]))
        size = Decimal(str(signal["size"]))

        tick_size = DECIMAL_ONE_HUNDREDTH  # default
        if "tick_size" in signal:
            tick_size = Decimal(str(signal["tick_size"]))

        price = price.quantize(tick_size, rounding=ROUND_DOWN)
        size = size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        price_wei = size_to_token_amount(price, USDC_DECIMALS)
        size_wei = size_to_token_amount(size, TOKEN_DECIMALS)

        if side == "BUY":
            maker_amount = int(size_wei * price_wei // (10 ** USDC_DECIMALS))
            taker_amount = size_wei
        else:
            maker_amount = size_wei
            taker_amount = int(size_wei * price_wei // (10 ** USDC_DECIMALS))

        timestamp_ms = str(int(time.time() * 1000))
        salt = _generate_salt()
        token_id = signal["asset_id"]
        neg_risk = signal.get("neg_risk", False)
        exchange = NEG_RISK_EXCHANGE_V2 if neg_risk else EXCHANGE_V2

        order_data = {
            "salt": salt,
            "maker": self._wallet_address,
            "signer": self._wallet_address,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "side": side_int,
            "signatureType": 0,
            "timestamp": int(timestamp_ms),
            "metadata": BYTES32_ZERO,
            "builder": BYTES32_ZERO,
        }

        return order_data, exchange

    def _build_typed_data(self, order_data: dict, exchange: str) -> dict[str, Any]:
        return {
            "primaryType": ORDER_PRIMARY_TYPE,
            "types": EIP712_ORDER_TYPES,
            "domain": {
                "name": ORDER_DOMAIN_NAME,
                "version": ORDER_DOMAIN_VERSION,
                "chainId": CHAIN_ID,
                "verifyingContract": exchange,
            },
            "message": order_data,
        }

    def _sign_order(self, typed_data: dict) -> str:
        encoded = encode_typed_data(full_message=typed_data)
        signed = Account.sign_message(encoded, private_key=self._private_key)
        return "0x" + signed.signature.hex()

    # ── Raw Send (called by OrderLifecycleManager) ────────────────────

    async def _send_order_raw(
        self, order_data: dict, signature: str, signal: dict
    ) -> dict[str, Any]:
        """Low-level send to CLOB. Used by OrderLifecycleManager."""
        side_str = "BUY" if order_data["side"] == 0 else "SELL"
        owner = signal.get("owner", self._wallet_address)

        payload = {
            "order": {
                "salt": str(order_data["salt"]),
                "maker": order_data["maker"],
                "signer": order_data["signer"],
                "tokenId": str(order_data["tokenId"]),
                "makerAmount": str(order_data["makerAmount"]),
                "takerAmount": str(order_data["takerAmount"]),
                "side": side_str,
                "expiration": signal.get("expiration", "0"),
                "signatureType": order_data["signatureType"],
                "timestamp": str(order_data["timestamp"]),
                "metadata": order_data["metadata"],
                "builder": order_data["builder"],
                "signature": signature,
            },
            "owner": owner,
            "orderType": signal.get("order_type", "GTC"),
            "deferExec": signal.get("defer_exec", False),
            "postOnly": signal.get("post_only", False),
        }

        if self.dry_run:
            return {"success": True, "dry_run": True, "payload": payload}

        body = json.dumps(payload, separators=(",", ":"))
        headers = self._build_l2_headers("POST", "/order", body)
        headers["Content-Type"] = "application/json"

        session = await self._get_session()
        async with session.post("/order", data=body, headers=headers) as resp:
            status = resp.status
            text = await resp.text()

            if status == 200:
                result: dict[str, Any] = {"success": True, "status": status}
                try:
                    data = json.loads(text)
                    result["order_id"] = data.get("orderID", data.get("id", ""))
                except (json.JSONDecodeError, KeyError):
                    pass
                return result

            return {"success": False, "status": status, "error": text[:500]}

    # ── Signal Processing ─────────────────────────────────────────────

    async def _log_execution(self, signal: dict, result: dict[str, Any]) -> None:
        if self.execution_log_queue is None:
            return
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "module": "ejecucion",
            "asset_id": str(signal.get("asset_id", "")),
            "market": str(signal.get("market", "")),
            "side": str(signal.get("side", "")),
            "price": str(signal.get("price", "")),
            "size": str(signal.get("size", "")),
            "probability": str(signal.get("probability", "")),
            "ev": str(signal.get("ev", "")),
            "success": result.get("success", False),
            "order_id": str(result.get("order_id", "")),
            "error": str(result.get("error", "")),
        }
        try:
            await asyncio.wait_for(self.execution_log_queue.put(entry), timeout=0.5)
        except asyncio.TimeoutError:
            logger.warning("cola de log llena — entrada descartada")

    def _emit_structured_log(self, event_data: dict[str, Any]) -> None:
        """Emit structured JSON log line."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event_data,
        }
        logger.info("EVENT %s", json.dumps(record, default=str))

    async def _check_market_filters(self, signal: dict) -> str | None:
        """Check all market-level filters. Returns error string or None."""
        probability = Decimal(str(signal.get("probability", "0.5")))
        current_price = Decimal(str(signal.get("current_price", "0.5")))
        size = Decimal(str(signal.get("size", "0")))

        if self._market_is_excluded(probability):
            return f"market_excluded: prob={probability}"
        if self._market_is_excluded(current_price):
            return f"market_excluded: current_price={current_price}"

        fee, post_edge = self._estimate_fee_and_edge(probability, current_price, size)
        min_edge = Decimal(str(RISK_CFG["min_edge_to_trade"]))
        if post_edge < min_edge:
            return f"edge_below_min: post_fee_edge={post_edge} < {min_edge} (fee={fee})"

        asset_id = str(signal.get("asset_id", ""))
        market_type = signal.get("market_type", "default")
        if not self._check_opportunity_window(asset_id, market_type):
            return "outside_opportunity_window"

        return None

    async def _place_order_from_signal(
        self, signal: dict, bypass_filters: bool = False,
    ) -> None:
        """Core order placement logic extracted from _process_signal.

        Parameters
        ----------
        signal : dict
            Trading signal or force-close signal.
        bypass_filters : bool
            If True, skip market qualifier checks (used for force-close).
        """
        asset_id = str(signal.get("asset_id", ""))
        side = str(signal.get("side", ""))
        price = Decimal(str(signal.get("price", "0")))
        size = Decimal(str(signal.get("size", "0")))
        proposed_cost = price * size

        # 1. Check if trading is paused by OrderGuard
        if await self._order_guard.is_trading_paused():
            reason = "trading_paused_by_order_guard_ws_disconnect"
            logger.warning("OrderGuard bloqueó: %s", reason)
            await self._log_execution(signal, {"success": False, "error": reason})
            return

        # 2. Check circuit breakers (v2 unified gate)
        allowed, cb_reason = await self._circuit_breaker.check_all_breakers(
            asset_id, proposed_cost,
        )
        if not allowed:
            logger.warning("circuit breaker bloqueó: %s", cb_reason)
            self._last_error = cb_reason
            result = {"success": False, "error": f"circuit_breaker: {cb_reason}"}
            await self._log_execution(signal, result)
            return

        # 3. Check market filters (skip for force-close)
        if not bypass_filters:
            filter_reason = self._market_qualifier.check_signal(
                signal, self._market_meta.get(asset_id),
                static_min_edge=Decimal(str(RISK_CFG.get("min_edge_to_trade", "0.05"))),
            )
            if filter_reason is not None:
                logger.info("market filter bloqueó: %s", filter_reason)
                return

        # 4. Build and sign order
        try:
            order_data, exchange = self._build_order_payload(signal)
            typed_data = self._build_typed_data(order_data, exchange)
            signature = self._sign_order(typed_data)
        except Exception:
            logger.exception("error construyendo/firmando orden")
            self._circuit_breaker.record_failure()
            await self._log_execution(signal, {"success": False, "error": "build/sign failed"})
            return

        # 5. Track pending buy cost for available cash calculation
        if side in ("BUY_YES", "BUY_NO"):
            self._circuit_breaker.track_pending_buy(
                order_data.get("salt", str(time.time())),
                proposed_cost,
            )

        # 6. Place via OrderLifecycleManager (timeout-safe)
        op_result = await self._order_lifecycle.execute_trading_cycle(
            signal, order_data, signature,
        )

        # Track for circuit breakers
        self._order_timestamps.append(time.time())
        self._order_results.append(op_result.success)
        await self._circuit_breaker.record_order(filled=op_result.success)

        if not op_result.success:
            self._circuit_breaker.record_failure()
            self._last_error = op_result.error
            # Untrack pending buy on failure
            if side in ("BUY_YES", "BUY_NO"):
                self._circuit_breaker.untrack_pending_buy(
                    order_data.get("salt", str(time.time())),
                )

        # Update PnL tracking
        if op_result.success and not self.dry_run:
            if side in ("BUY_YES", "BUY_NO"):
                self._daily_pnl -= proposed_cost
                await self._circuit_breaker.record_pnl(-proposed_cost)
                # Register position with PositionManager
                self._position_manager.open_position(
                    asset_id=asset_id,
                    side=side,
                    entry_price=price,
                    size=size,
                )
            elif side in ("SELL_YES", "SELL_NO"):
                self._daily_pnl += proposed_cost
                await self._circuit_breaker.record_pnl(proposed_cost)
                # Close position in PositionManager
                self._position_manager.close_position(asset_id)
            self._last_trade_time = time.time()

        # Handle result
        if op_result.success:
            oid = op_result.order_id
            self._open_orders[oid] = {
                "asset_id": asset_id,
                "side": side,
                "price": str(price),
                "size": str(size),
                "created_at": time.time(),
            }
            logger.info(
                "orden OK: asset=%s side=%s price=%s size=%s id=%s latency=%.0fms",
                asset_id, side, str(price), str(size),
                oid, op_result.latency_ms,
            )
        else:
            logger.error(
                "orden FALLÓ: asset=%s side=%s error=%s",
                asset_id, side, op_result.error,
            )

        result = {
            "success": op_result.success,
            "order_id": op_result.order_id,
            "error": op_result.error,
        }
        await self._log_execution(signal, result)

        self._emit_structured_log({
            "event_type": "order_result",
            "asset_id": asset_id,
            "side": side,
            "price": str(price),
            "size": str(size),
            "order_id": op_result.order_id,
            "success": op_result.success,
            "error": op_result.error,
            "latency_ms": round(op_result.latency_ms, 2),
        })

    async def _process_signal(self, signal: dict) -> None:
        """Process a single signal with full live protections.

        This is the v2 unified processing pipeline:
          1. Emit structured log
          2. Check OrderGuard (trading paused?)
          3. Check all circuit breakers (unified gate)
          4. Check market qualifier filters
          5. Track pending buys for available cash
          6. Build, sign, and place order via OrderLifecycleManager
          7. Track PnL and positions
          8. Record result
        """
        asset_id = str(signal.get("asset_id", ""))
        side = str(signal.get("side", ""))
        price = Decimal(str(signal.get("price", "0")))
        size = Decimal(str(signal.get("size", "0")))

        self._emit_structured_log({
            "event_type": "signal_received",
            "asset_id": asset_id,
            "side": side,
            "price": str(price),
            "size": str(size),
            "probability": str(signal.get("probability", "")),
            "ev": str(signal.get("ev", "")),
        })

        await self._place_order_from_signal(signal, bypass_filters=False)

    # ── Reconciliation on Startup ──────────────────────────────────────

    async def _reconcile_state(self) -> None:
        """Reconcile local state against Polymarket on startup."""
        logger.info("reconciliation: fetching open orders from Polymarket…")
        try:
            remote_orders = await self._fetch_open_orders_cb()
            remote_ids = {oid for oid, _ in remote_orders}
            local_ids = set(self._open_orders.keys())

            # Orders in remote but not local
            orphaned = remote_ids - local_ids
            for oid in orphaned:
                logger.warning("reconciliation: orphaned remote order %s — tracking", oid)
                self._open_orders[oid] = {
                    "asset_id": "unknown",
                    "side": "unknown",
                    "price": "0",
                    "size": "0",
                    "created_at": time.time(),
                    "reconciled": True,
                }

            # Orders in local but not remote (already resolved/cancelled)
            stale_local = local_ids - remote_ids
            for oid in stale_local:
                logger.info("reconciliation: local order %s not on remote — marking resolved", oid)
                self._open_orders.pop(oid, None)

            logger.info(
                "reconciliation complete: %d remote, %d local, %d orphaned, %d stale",
                len(remote_ids), len(local_ids), len(orphaned), len(stale_local),
            )
        except Exception:
            logger.exception("reconciliation failed — proceeding with current state")

    # ── Health HTTP Server ─────────────────────────────────────────────

    async def _health_handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve health check and Prometheus metrics over HTTP.

        v2: Extended /health endpoint with comprehensive system status:
          - Overall status (OK/DEGRADED/BLOCKED)
          - Balance and PnL
          - Circuit breaker states (including total drawdown, cooldown)
          - WebSocket health and book sync state
          - OrderGuard status
          - Position stats
          - Performance metrics (MAE, adjusted min_edge)
          - Monitor health
        """
        try:
            request_line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("utf-8").strip()
            parts = request_line.split(" ")
            method = parts[0] if len(parts) > 1 else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            balance = await self._get_usdc_balance()
            cb_state = self._circuit_breaker.get_state_snapshot()

            if path == "/metrics":
                body = self._build_prometheus_metrics(cb_state, balance)
                content_type = "text/plain; version=0.0.4"
                response_line = "HTTP/1.1 200 OK\r\n"
            elif path == "/health":
                # v2: Comprehensive health JSON
                ws_health = getattr(self, "_ws_health", {
                    "connected": False, "book_synced": False, "syncing": False,
                })
                order_guard_paused = await self._order_guard.is_trading_paused() if hasattr(self, "_order_guard") else False
                position_stats = self._position_manager.get_stats() if hasattr(self, "_position_manager") else {"open_positions": 0}
                perf_stats = self._performance_tracker.get_stats() if hasattr(self, "_performance_tracker") else {}

                health_data = self._cron_monitor.build_health_response(
                    circuit_breaker_snapshot=cb_state,
                    ws_health=ws_health,
                    order_guard_paused=order_guard_paused,
                    position_stats=position_stats,
                    performance_stats=perf_stats,
                    extra={
                        "dry_run": self.dry_run,
                        "uptime_seconds": round(time.time() - self._start_time) if hasattr(self, "_start_time") else 0,
                        "last_error": self._last_error,
                    },
                )

                overall_status = health_data.get("status", "OK")
                http_status = 200 if overall_status == "OK" else (503 if overall_status == "BLOCKED" else 200)
                body = json.dumps(health_data, indent=2, default=str)
                content_type = "application/json"
                response_line = f"HTTP/1.1 {http_status} OK\r\n"
            else:
                body = json.dumps({"error": "not_found"})
                content_type = "application/json"
                response_line = "HTTP/1.1 404 Not Found\r\n"

            response = (
                f"{response_line}"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception:
            logger.exception("error in health handler")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _build_prometheus_metrics(
        self, cb_state: dict[str, Any], balance: Decimal
    ) -> str:
        lines = [
            "# HELP polymarket_bot_info Polymarket bot information",
            "# TYPE polymarket_bot_info gauge",
            f'polymarket_bot_info{{dry_run="{"true" if self.dry_run else "false"}"}} 1',
            "",
            "# HELP polymarket_balance_usdc Current USDC balance",
            "# TYPE polymarket_balance_usdc gauge",
            f"polymarket_balance_usdc {float(balance)}",
            "",
            "# HELP polymarket_open_orders Number of open orders",
            "# TYPE polymarket_open_orders gauge",
            f"polymarket_open_orders {len(self._open_orders)}",
            "",
            "# HELP polymarket_daily_pnl Daily PnL in USDC",
            "# TYPE polymarket_daily_pnl gauge",
            f"polymarket_daily_pnl {float(self._daily_pnl)}",
            "",
            "# HELP polymarket_total_orders_placed Total orders placed",
            "# TYPE polymarket_total_orders_placed counter",
            f"polymarket_total_orders_placed {cb_state.get('total_orders_placed', 0)}",
            "",
            "# HELP polymarket_total_orders_filled Total orders filled",
            "# TYPE polymarket_total_orders_filled counter",
            f"polymarket_total_orders_filled {cb_state.get('total_orders_filled', 0)}",
            "",
            "# HELP polymarket_blocked Bot blocked status (1=blocked)",
            "# TYPE polymarket_blocked gauge",
            f'polymarket_blocked {1 if cb_state.get("blocked", False) else 0}',
            "",
            # v2 metrics
            "# HELP polymarket_ws_connected WebSocket connected (1=connected)",
            "# TYPE polymarket_ws_connected gauge",
            f'polymarket_ws_connected {1 if self._ws_health.get("connected", False) else 0}',
            "",
            "# HELP polymarket_book_synced Order book synced (1=synced)",
            "# TYPE polymarket_book_synced gauge",
            f'polymarket_book_synced {1 if self._ws_health.get("book_synced", False) else 0}',
            "",
            "# HELP polymarket_total_drawdown_blocked Total drawdown permanent block (1=blocked)",
            "# TYPE polymarket_total_drawdown_blocked gauge",
            f'polymarket_total_drawdown_blocked {1 if cb_state.get("total_drawdown_blocked", False) else 0}',
            "",
            "# HELP polymarket_trading_paused Trading paused by OrderGuard (1=paused)",
            "# TYPE polymarket_trading_paused gauge",
            f"polymarket_trading_paused 0",
            "",
            "# HELP polymarket_positions_open Number of open positions",
            "# TYPE polymarket_positions_open gauge",
            f"polymarket_positions_open {len(self._position_manager.get_all_positions()) if hasattr(self, '_position_manager') else 0}",
            "",
            "# HELP polymarket_mae Mean Absolute Error of predictions",
            "# TYPE polymarket_mae gauge",
            f"polymarket_mae {float(self._performance_tracker.mae) if hasattr(self, '_performance_tracker') else 0}",
            "",
            "# HELP polymarket_adjusted_min_edge Adjusted minimum edge threshold",
            "# TYPE polymarket_adjusted_min_edge gauge",
            f"polymarket_adjusted_min_edge {float(self._performance_tracker.adjusted_min_edge) if hasattr(self, '_performance_tracker') else 0.05}",
            "",
        ]
        return "\n".join(lines)

    async def _start_health_server(self) -> None:
        try:
            self._health_server = await asyncio.start_server(
                self._health_handler,
                host="0.0.0.0",
                port=self._health_port,
            )
            logger.info(
                "health server listening on 0.0.0.0:%d",
                self._health_port,
            )
        except Exception:
            logger.warning(
                "could not start health server on port %d",
                self._health_port,
            )

    async def _stop_health_server(self) -> None:
        if self._health_server:
            self._health_server.close()
            await self._health_server.wait_closed()

    # ── Idle Alert ────────────────────────────────────────────────────

    async def _idle_alert_loop(self) -> None:
        """Alert if bot goes more than 5 minutes without trading."""
        idle_minutes = int(LIVE_CONFIG["monitoring"]["alert_if_idle_minutes"])
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            elapsed = time.time() - self._last_trade_time
            if elapsed > idle_minutes * 60 and self._last_trade_time > 0:
                logger.warning(
                    "IDLE ALERT: no trades for %.0f minutes (threshold=%d)",
                    elapsed / 60, idle_minutes,
                )

    # ── Graceful Shutdown ─────────────────────────────────────────────

    def _setup_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._shutdown(s)))

    def update_ws_health(self, health: dict[str, Any]) -> None:
        """Update WebSocket health from ingesta module."""
        self._ws_health = health

    async def _shutdown(self, sig: signal.Signals) -> None:
        logger.info("signal %s received — starting graceful shutdown", sig.name)
        self._running = False

        # Cancel all open orders
        logger.info("shutdown: cancelling all open orders…")
        await self._circuit_breaker.cancel_all_orders()
        await self._order_lifecycle.cleanup_on_shutdown()

        # v2: Stop new modules
        await self._order_guard.shutdown()
        await self._position_manager.stop()
        await self._cron_monitor.stop()
        await self._performance_tracker.stop()
        await self._alert_manager.stop()

        # Stop circuit breaker
        await self._circuit_breaker.stop()

        # Stop health server
        await self._stop_health_server()

        # Close HTTP session
        await self.close()

        logger.info("shutdown complete — goodbye")

    # ── Main Loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._start_time = time.time()
        logger.info("EjecutorOrdenes iniciado (dry_run=%s)", self.dry_run)

        if not self.dry_run:
            logger.info("MODO LIVE — todas las protecciones activas")

        # Initialize circuit breakers
        await self._circuit_breaker.start()

        # ── v2: Initialize new modules ─────────────────────────────────
        await self._performance_tracker.start()
        await self._position_manager.start()
        await self._cron_monitor.start()
        await self._alert_manager.start()

        # v2: Clean start — cancel all residual orders via OrderGuard
        await self._order_guard.clean_start()
        await self._order_guard.start_watchdog()

        # Also run existing startup cleanup
        await self._circuit_breaker.startup_cancel_all()
        await self._order_lifecycle.cleanup_on_startup()

        # Reconcile state
        await self._reconcile_state()

        # Start health server
        await self._start_health_server()

        # Set up signal handlers
        try:
            self._setup_signal_handlers()
        except (NotImplementedError, RuntimeError):
            logger.debug("signal handlers not available on this platform")

        # Start idle alert loop
        idle_task = asyncio.create_task(self._idle_alert_loop())

        try:
            while self._running:
                try:
                    signal = await asyncio.wait_for(
                        self.signal_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Check if signal is a force-close (from PositionManager)
                if signal.get("is_force_close", False):
                    logger.warning("Processing force-close signal: %s", signal.get("asset_id"))
                    await self._place_order_from_signal(signal, bypass_filters=True)
                else:
                    async with self._semaphore:
                        await self._process_signal(signal)

        except asyncio.CancelledError:
            logger.info("EjecutorOrdenes cancelado")
        finally:
            self._running = False
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass

            # v2: Stop new modules
            await self._order_guard.shutdown()
            await self._position_manager.stop()
            await self._cron_monitor.stop()
            await self._performance_tracker.stop()
            await self._alert_manager.stop()

            await self._circuit_breaker.stop()
            await self._order_lifecycle.cleanup_on_shutdown()
            await self._stop_health_server()
            await self.close()
            logger.info("EjecutorOrdenes detenido")

    def stop(self) -> None:
        self._running = False


DECIMAL_ONE_HUNDREDTH = Decimal("0.01")


# ── main de prueba ─────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    q: asyncio.Queue = asyncio.Queue()
    executor = EjecutorOrdenes(signal_queue=q, dry_run=True)

    test_signal = {
        "asset_id": "123456",
        "market": "test-market",
        "side": "BUY_YES",
        "price": "0.52",
        "size": "10",
        "probability": "0.55",
        "current_price": "0.52",
        "ev": "0.03",
        "tick_size": "0.01",
    }
    await q.put(test_signal)

    try:
        await asyncio.wait_for(executor.run(), timeout=10)
    except asyncio.TimeoutError:
        executor.stop()


if __name__ == "__main__":
    asyncio.run(main())
