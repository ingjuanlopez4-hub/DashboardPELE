"""
Módulo A — Ingesta y Normalización de eventos del CLOB de Polymarket.

Conecta al WebSocket del market channel, se autentica, se suscribe a todos los
mercados activos, recibe eventos en tiempo real (book, price_change,
tick_size_change, last_trade_price, best_bid_ask, new_market, market_resolved),
los normaliza a un formato interno con Decimal y los deposita en una cola
asyncio para consumo por el Módulo B (estrategia).

v2 improvements (PRODUCTION):
  - Uses ResilientWebSocketClient for zombie detection, auto-reconnect,
    book snapshot sync, and event deduplication.
  - Health status exposed for monitoring.
  - book_synced flag for coordination with strategy/execution modules.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import websockets

from src.live.data_resilience import ResilientWebSocketClient, ConnectionHealth

logger = logging.getLogger("ingesta")

WS_URL = os.getenv("POLYMARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
PING_INTERVAL = 10
QUEUE_MAXSIZE = 10000
DEFAULT_TICK_SIZE = Decimal("0.01")
SIZE_PRECISION = Decimal("0.01")
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
BACKOFF_MULTIPLIER = 2.0


@dataclass
class NormalizedEvent:
    type: str
    market: str
    asset_id: str | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    side: str | None = None
    timestamp_iso: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.type,
            "market": self.market,
            "asset_id": self.asset_id,
            "side": self.side,
            "timestamp_iso": self.timestamp_iso,
        }
        if self.price is not None:
            d["price"] = str(self.price)
        if self.size is not None:
            d["size"] = str(self.size)
        d.update(self.extra)
        return d


class MarketState:
    """Mantiene tick_size por asset_id y mapeo condition_id → asset_ids."""

    def __init__(self, initial_snapshot: list[dict] | None = None):
        self._tick_sizes: dict[str, Decimal] = {}
        self._cond_to_assets: dict[str, list[str]] = {}
        if initial_snapshot:
            self._load_snapshot(initial_snapshot)

    def _load_snapshot(self, markets: list[dict]) -> None:
        for m in markets:
            tick = m.get("tick_size") or m.get("order_price_min_tick_size") or "0.01"
            tick_size = Decimal(str(tick))
            for aid in m.get("asset_ids", m.get("assets_ids", m.get("clob_token_ids", []))):
                self._tick_sizes[aid] = tick_size
            cond_id = m.get("condition_id") or m.get("market")
            if cond_id:
                aids = m.get("asset_ids", m.get("assets_ids", m.get("clob_token_ids", [])))
                self._cond_to_assets[cond_id] = aids

    def get_tick_size(self, asset_id: str) -> Decimal:
        return self._tick_sizes.get(asset_id, DEFAULT_TICK_SIZE)

    def set_tick_size(self, asset_id: str, tick_size: str) -> None:
        self._tick_sizes[asset_id] = Decimal(str(tick_size))

    def ingest_new_market(self, msg: dict) -> None:
        cond_id = msg.get("market") or msg.get("condition_id")
        tick = msg.get("order_price_min_tick_size", "0.01")
        tick_size = Decimal(str(tick))
        for aid in msg.get("assets_ids", msg.get("clob_token_ids", [])):
            self._tick_sizes[aid] = tick_size
        if cond_id:
            aids = msg.get("assets_ids", msg.get("clob_token_ids", []))
            self._cond_to_assets[cond_id] = aids


class IngestaCLOB:
    """Ingesta y normalización de eventos del CLOB de Polymarket.

    Se conecta al WebSocket del market channel, se autentica, se suscribe
    a todos los mercados configurados, recibe eventos, los normaliza a
    objetos NormalizedEvent con Decimal y los deposita en una cola asyncio.

    v2: Uses ResilientWebSocketClient internally for zombie detection,
    auto-reconnect, book snapshot sync, and event deduplication.
    """

    def __init__(
        self,
        asset_ids: list[str] | None = None,
        market_snapshot: list[dict] | None = None,
        clob_api_base: str = "https://clob.polymarket.com",
        zombie_timeout_s: int = 60,
    ) -> None:
        self.api_key = os.environ["POLYMARKET_API_KEY"]
        self.secret = os.environ["POLYMARKET_SECRET"]
        self.passphrase = os.environ["POLYMARKET_PASSPHRASE"]
        self.asset_ids = asset_ids or ["*"]
        self.queue: asyncio.Queue[NormalizedEvent] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.market_state = MarketState(market_snapshot)
        self._running = False

        # v2: ResilientWebSocketClient
        self._resilient_ws: ResilientWebSocketClient | None = None
        self._clob_api_base = clob_api_base
        self._zombie_timeout_s = zombie_timeout_s

        # v2: callbacks for disconnect/reconnect
        self._on_disconnect_cb: Any = None
        self._on_reconnect_cb: Any = None

    def set_disconnect_callback(self, cb: Any) -> None:
        """Set callback for WS disconnect events."""
        self._on_disconnect_cb = cb

    def set_reconnect_callback(self, cb: Any) -> None:
        """Set callback for WS reconnect + book sync events."""
        self._on_reconnect_cb = cb

    @property
    def book_synced(self) -> bool:
        """Check if order book is fully synced after last reconnect."""
        if self._resilient_ws:
            return self._resilient_ws.book_synced
        return False

    @property
    def ws_health(self) -> dict[str, Any]:
        """Get WebSocket health status for monitoring."""
        if self._resilient_ws:
            return self._resilient_ws.get_health_dict()
        return {
            "connected": False,
            "book_synced": False,
            "syncing": False,
        }

    # ── helpers de red ──────────────────────────────────────────────

    async def _connect_and_auth(self) -> Any:
        """Connect to WebSocket, authenticate, and subscribe.

        This is used as the connect_factory for ResilientWebSocketClient.
        """
        ws = await websockets.connect(
            WS_URL,
            ping_interval=None,
            close_timeout=5,
        )
        await self._send_auth(ws)
        await self._subscribe(ws)
        return ws

    async def _send_auth(self, ws: Any) -> None:
        msg = {
            "type": "auth",
            "apiKey": self.api_key,
            "secret": self.secret,
            "passphrase": self.passphrase,
        }
        await ws.send(json.dumps(msg))
        logger.debug("mensaje de autenticación enviado")

    async def _subscribe(self, ws: Any) -> None:
        msg: dict[str, Any] = {
            "assets_ids": self.asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(msg))
        logger.info("suscripción enviada: %d asset(s)", len(self.asset_ids))

    async def _on_message(self, raw_message: str) -> None:
        """Callback for ResilientWebSocketClient message events."""
        await self._dispatch(raw_message)

    # ── normalización ───────────────────────────────────────────────

    @staticmethod
    def _parse_timestamp(ts: str) -> str:
        try:
            raw = int(ts)
            if raw > 1e12:
                return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).isoformat()
            return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            return ts

    def _qprice(self, price: str, asset_id: str) -> Decimal:
        tick = self.market_state.get_tick_size(asset_id)
        return Decimal(str(price)).quantize(tick, rounding=ROUND_HALF_UP)

    @staticmethod
    def _qsize(size: str) -> Decimal:
        return Decimal(str(size)).quantize(SIZE_PRECISION, rounding=ROUND_HALF_UP)

    def _normalize_book(self, msg: dict) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        asset_id = msg.get("asset_id", "")
        market = msg.get("market", "")
        ts = self._parse_timestamp(msg.get("timestamp", ""))

        for side, key in [("BUY", "bids"), ("SELL", "asks")]:
            for entry in msg.get(key, []):
                out.append(NormalizedEvent(
                    type="book",
                    market=market,
                    asset_id=asset_id,
                    price=self._qprice(entry["price"], asset_id),
                    size=self._qsize(entry["size"]),
                    side=side,
                    timestamp_iso=ts,
                    extra={"hash": msg.get("hash")},
                ))
        return out

    def _normalize_price_change(self, msg: dict) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        market = msg.get("market", "")
        ts = self._parse_timestamp(msg.get("timestamp", ""))

        for pc in msg.get("price_changes", []):
            aid = pc.get("asset_id", "")
            out.append(NormalizedEvent(
                type="price_change",
                market=market,
                asset_id=aid,
                price=self._qprice(pc["price"], aid),
                size=self._qsize(pc.get("size", "0")),
                side=pc.get("side"),
                timestamp_iso=ts,
                extra={
                    "hash": pc.get("hash"),
                    "best_bid": pc.get("best_bid"),
                    "best_ask": pc.get("best_ask"),
                },
            ))
        return out

    def _normalize_tick_size_change(self, msg: dict) -> list[NormalizedEvent]:
        aid = msg.get("asset_id", "")
        self.market_state.set_tick_size(aid, msg["new_tick_size"])
        return [NormalizedEvent(
            type="tick_size_change",
            market=msg.get("market", ""),
            asset_id=aid,
            timestamp_iso=self._parse_timestamp(msg.get("timestamp", "")),
            extra={
                "old_tick_size": msg.get("old_tick_size"),
                "new_tick_size": msg.get("new_tick_size"),
            },
        )]

    def _normalize_last_trade_price(self, msg: dict) -> list[NormalizedEvent]:
        aid = msg.get("asset_id", "")
        return [NormalizedEvent(
            type="last_trade_price",
            market=msg.get("market", ""),
            asset_id=aid,
            price=self._qprice(msg["price"], aid),
            size=self._qsize(msg.get("size", "0")),
            side=msg.get("side"),
            timestamp_iso=self._parse_timestamp(msg.get("timestamp", "")),
        )]

    def _normalize_best_bid_ask(self, msg: dict) -> list[NormalizedEvent]:
        return [NormalizedEvent(
            type="best_bid_ask",
            market=msg.get("market", ""),
            asset_id=msg.get("asset_id", ""),
            timestamp_iso=self._parse_timestamp(msg.get("timestamp", "")),
            extra={
                "best_bid": msg.get("best_bid"),
                "best_ask": msg.get("best_ask"),
            },
        )]

    def _normalize_new_market(self, msg: dict) -> list[NormalizedEvent]:
        self.market_state.ingest_new_market(msg)
        return [NormalizedEvent(
            type="new_market",
            market=msg.get("market", ""),
            timestamp_iso=self._parse_timestamp(msg.get("timestamp", "")),
            extra={k: v for k, v in msg.items() if k not in ("event_type", "timestamp")},
        )]

    def _normalize_market_resolved(self, msg: dict) -> list[NormalizedEvent]:
        return [NormalizedEvent(
            type="market_resolved",
            market=msg.get("market", ""),
            timestamp_iso=self._parse_timestamp(msg.get("timestamp", "")),
            extra={
                "winning_outcome": msg.get("winning_outcome"),
                "winning_asset_id": msg.get("winning_asset_id"),
            },
        )]

    _NORMALIZERS: dict[str, Any] = {}

    def _normalize(self, raw: str | bytes) -> list[NormalizedEvent]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        if raw.strip() == "PONG":
            logger.debug("PONG <-")
            return []

        msg = json.loads(raw)
        event_type = msg.get("event_type")
        if not event_type:
            logger.warning("mensaje sin event_type: %.200s", raw)
            return []

        normalizer = self._NORMALIZERS.get(event_type)
        if normalizer is None:
            logger.debug("tipo de evento ignorado: %s", event_type)
            return []

        try:
            return normalizer(self, msg)
        except Exception:
            logger.exception("error normalizando evento %s", event_type)
            return []

    # ── bucle principal ─────────────────────────────────────────────

    async def _dispatch(self, raw: str) -> None:
        events = self._normalize(raw)
        for evt in events:
            try:
                await asyncio.wait_for(self.queue.put(evt), timeout=1.0)
            except asyncio.TimeoutError:
                logger.warning("cola llena — evento descartado: type=%s market=%.20s", evt.type, evt.market)

    # ── API pública ─────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logger.info("IngestaCLOB iniciado")

        # Create ResilientWebSocketClient
        self._resilient_ws = ResilientWebSocketClient(
            connect_factory=self._connect_and_auth,
            message_callback=self._on_message,
            zombie_timeout_s=self._zombie_timeout_s,
            clob_api_base=self._clob_api_base,
        )

        # Set active token IDs for book snapshot sync
        if self.asset_ids and self.asset_ids != ["*"]:
            self._resilient_ws.set_token_ids(self.asset_ids)

        # Wire disconnect/reconnect callbacks
        if self._on_disconnect_cb:
            self._resilient_ws.set_disconnect_callback(self._on_disconnect_cb)
        if self._on_reconnect_cb:
            self._resilient_ws.set_reconnect_callback(self._on_reconnect_cb)

        try:
            await self._resilient_ws.start()
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if self._resilient_ws:
                await self._resilient_ws.stop()
            logger.info("IngestaCLOB detenido")

    def stop(self) -> None:
        self._running = False


# registro de normalizers (fuera de la clase para evitar closures circulares)
IngestaCLOB._NORMALIZERS = {
    "book": IngestaCLOB._normalize_book,
    "price_change": IngestaCLOB._normalize_price_change,
    "tick_size_change": IngestaCLOB._normalize_tick_size_change,
    "last_trade_price": IngestaCLOB._normalize_last_trade_price,
    "best_bid_ask": IngestaCLOB._normalize_best_bid_ask,
    "new_market": IngestaCLOB._normalize_new_market,
    "market_resolved": IngestaCLOB._normalize_market_resolved,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    ingesta = IngestaCLOB()
    try:
        await ingesta.run()
    except KeyboardInterrupt:
        logger.info("interrupción de usuario")
        ingesta.stop()


if __name__ == "__main__":
    asyncio.run(main())
