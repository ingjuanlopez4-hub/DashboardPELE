import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("data_resilience")

ZOMBIE_TIMEOUT_S = 60
SNAPSHOT_TIMEOUT_S = 10
MAX_DEDUP_HASHES = 1000
WS_PING_INTERVAL = 10


@dataclass
class ConnectionHealth:
    connected: bool = False
    last_message_at: float = 0.0
    last_connect_attempt_at: float = 0.0
    last_disconnect_at: float = 0.0
    reconnect_count: int = 0
    zombie_count: int = 0
    book_synced: bool = False
    syncing: bool = False


class EventDeduplicator:
    def __init__(self, max_hashes: int = MAX_DEDUP_HASHES) -> None:
        self._max = max_hashes
        self._hashes: OrderedDict[str, float] = OrderedDict()

    def is_duplicate(self, event_hash: str | None) -> bool:
        if not event_hash:
            return False
        if event_hash in self._hashes:
            return True
        self._add(event_hash)
        return False

    def _add(self, event_hash: str) -> None:
        self._hashes[event_hash] = time.time()
        if len(self._hashes) > self._max:
            self._hashes.popitem(last=False)

    def clear(self) -> None:
        self._hashes.clear()

    @property
    def size(self) -> int:
        return len(self._hashes)


class BookSnapshotFetcher:
    def __init__(self, clob_api_base: str = "https://clob.polymarket.com") -> None:
        self._api_base = clob_api_base

    async def fetch_snapshot(
        self,
        token_id: str,
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any] | None:
        if session is None:
            async with aiohttp.ClientSession() as own_session:
                return await self._do_fetch(token_id, own_session)
        return await self._do_fetch(token_id, session)

    async def _do_fetch(
        self,
        token_id: str,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any] | None:
        try:
            url = f"{self._api_base}/book"
            params = {"token_id": token_id}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=SNAPSHOT_TIMEOUT_S)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug(
                        "Book snapshot for %s: %d bids, %d asks",
                        token_id,
                        len(data.get("bids", [])),
                        len(data.get("asks", [])),
                    )
                    return data
                else:
                    logger.warning(
                        "Book snapshot fetch failed for %s: HTTP %d",
                        token_id, resp.status,
                    )
                    return None
        except asyncio.TimeoutError:
            logger.warning("Book snapshot timeout for %s", token_id)
            return None
        except Exception:
            logger.exception("Book snapshot error for %s", token_id)
            return None

    async def fetch_all_snapshots(
        self,
        token_ids: list[str],
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not token_ids:
            return {}

        if session is None:
            async with aiohttp.ClientSession() as own_session:
                return await self._fetch_all(token_ids, own_session)
        return await self._fetch_all(token_ids, session)

    async def _fetch_all(
        self,
        token_ids: list[str],
        session: aiohttp.ClientSession,
    ) -> dict[str, dict[str, Any]]:
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = {
                    tid: tg.create_task(self._do_fetch(tid, session))
                    for tid in token_ids
                }

            results: dict[str, dict[str, Any]] = {}
            for tid, task in tasks.items():
                try:
                    result = await task
                    if result is not None:
                        results[tid] = result
                except Exception:
                    logger.exception("Error in snapshot task for %s", tid)
            return results
        except Exception:
            logger.exception("Error fetching all snapshots")
            return {}


class ResilientWebSocketClient:
    def __init__(
        self,
        connect_factory: Callable[[], Any],
        message_callback: Callable[[str], Any] | None = None,
        zombie_timeout_s: int = ZOMBIE_TIMEOUT_S,
        clob_api_base: str = "https://clob.polymarket.com",
    ) -> None:
        self._connect_factory = connect_factory
        self._message_cb = message_callback
        self._zombie_timeout_s = zombie_timeout_s
        self._clob_api_base = clob_api_base

        self._ws: Any = None
        self._running = False
        self._health = ConnectionHealth()
        self._dedup = EventDeduplicator()
        self._snapshot_fetcher = BookSnapshotFetcher(clob_api_base)
        self._active_token_ids: list[str] = []
        self._zombie_watch_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None
        self._on_disconnect_cb: Callable[[], Any] | None = None
        self._on_reconnect_cb: Callable[[], Any] | None = None

        self._backoff = 1.0
        self._backoff_max = 60.0
        self._backoff_mult = 2.0

        self._book_sync_event = asyncio.Event()
        self._book_sync_event.set()

    def set_token_ids(self, token_ids: list[str]) -> None:
        self._active_token_ids = token_ids

    def set_disconnect_callback(self, cb: Callable[[], Any]) -> None:
        self._on_disconnect_cb = cb

    def set_reconnect_callback(self, cb: Callable[[], Any]) -> None:
        self._on_reconnect_cb = cb

    @property
    def book_synced(self) -> bool:
        return self._health.book_synced

    @property
    def health(self) -> ConnectionHealth:
        return self._health

    @property
    def connected(self) -> bool:
        return self._health.connected

    async def wait_for_book_sync(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._book_sync_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _zombie_watch(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._zombie_timeout_s / 2)
                if not self._health.connected:
                    continue

                elapsed = time.time() - self._health.last_message_at
                if elapsed > self._zombie_timeout_s and self._health.last_message_at > 0:
                    logger.warning(
                        "ZOMBIE DETECTED: no message for %.0fs (timeout=%ds) — forcing reconnect",
                        elapsed, self._zombie_timeout_s,
                    )
                    self._health.zombie_count += 1
                    if self._ws:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Zombie watch error")

    async def _sync_book_snapshot(self, session: aiohttp.ClientSession | None = None) -> None:
        if not self._active_token_ids:
            self._health.book_synced = True
            self._health.syncing = False
            self._book_sync_event.set()
            if self._on_reconnect_cb:
                try:
                    await self._on_reconnect_cb()
                except Exception:
                    logger.exception("Reconnect callback error")
            return

        self._health.syncing = True
        self._health.book_synced = False
        self._book_sync_event.clear()

        logger.info(
            "Syncing book snapshots for %d tokens…",
            len(self._active_token_ids),
        )

        snapshots = await self._snapshot_fetcher.fetch_all_snapshots(
            self._active_token_ids, session=session,
        )

        logger.info(
            "Book sync complete: %d/%d snapshots fetched",
            len(snapshots), len(self._active_token_ids),
        )

        self._health.book_synced = True
        self._health.syncing = False
        self._book_sync_event.set()

        if self._on_reconnect_cb:
            try:
                await self._on_reconnect_cb()
            except Exception:
                logger.exception("Reconnect callback error")

    async def _listener(self, session: aiohttp.ClientSession | None = None) -> None:
        self._backoff = 1.0

        while self._running:
            try:
                logger.info("ResilientWS: connecting…")
                self._health.last_connect_attempt_at = time.time()
                self._ws = await self._connect_factory()
                self._health.connected = True
                self._health.last_message_at = time.time()
                self._backoff = 1.0

                logger.info("ResilientWS: connected")

                await self._sync_book_snapshot(session=session)

                self._dedup.clear()

                self._zombie_watch_task = asyncio.create_task(self._zombie_watch())

                async for message in self._ws:
                    self._health.connected = True
                    self._health.last_message_at = time.time()

                    try:
                        msg_data = json.loads(message)
                        event_hash = msg_data.get("hash")
                        if event_hash and self._dedup.is_duplicate(event_hash):
                            logger.debug("Dedup: skipped event %s", event_hash)
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass

                    if self._message_cb:
                        await self._message_cb(message)

            except asyncio.CancelledError:
                logger.info("ResilientWS: listener cancelled")
                self._running = False
            except Exception:
                logger.exception("ResilientWS: connection error")
            finally:
                self._health.connected = False
                self._health.last_disconnect_at = time.time()
                self._health.reconnect_count += 1
                self._health.book_synced = False
                self._book_sync_event.clear()

                if self._zombie_watch_task and not self._zombie_watch_task.done():
                    self._zombie_watch_task.cancel()
                    try:
                        await self._zombie_watch_task
                    except asyncio.CancelledError:
                        pass
                self._zombie_watch_task = None

                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

                if self._on_disconnect_cb:
                    try:
                        await self._on_disconnect_cb()
                    except Exception:
                        logger.exception("Disconnect callback error")

                if self._running:
                    logger.info(
                        "ResilientWS: reconnecting in %.0fs…",
                        self._backoff,
                    )
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(
                        self._backoff * self._backoff_mult,
                        self._backoff_max,
                    )

    async def start(self, session: aiohttp.ClientSession | None = None) -> None:
        self._running = True
        self._listener_task = asyncio.create_task(self._listener(session=session))
        logger.info("ResilientWebSocketClient started")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._zombie_watch_task and not self._zombie_watch_task.done():
            self._zombie_watch_task.cancel()
            try:
                await self._zombie_watch_task
            except asyncio.CancelledError:
                pass
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._book_sync_event.set()
        logger.info("ResilientWebSocketClient stopped")

    def get_health_dict(self) -> dict[str, Any]:
        h = self._health
        return {
            "connected": h.connected,
            "last_message_at": datetime.fromtimestamp(h.last_message_at, tz=timezone.utc).isoformat() if h.last_message_at > 0 else "never",
            "last_connect_attempt_at": datetime.fromtimestamp(h.last_connect_attempt_at, tz=timezone.utc).isoformat() if h.last_connect_attempt_at > 0 else "never",
            "last_disconnect_at": datetime.fromtimestamp(h.last_disconnect_at, tz=timezone.utc).isoformat() if h.last_disconnect_at > 0 else "never",
            "reconnect_count": h.reconnect_count,
            "zombie_count": h.zombie_count,
            "book_synced": h.book_synced,
            "syncing": h.syncing,
            "dedup_cache_size": self._dedup.size,
        }
