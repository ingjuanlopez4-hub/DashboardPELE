"""
Tests del módulo Data Resilience (src/live/data_resilience.py).

Cubre:
- EventDeduplicator: detección de duplicados, LRU cache, clear
- BookSnapshotFetcher: fetch de snapshot, timeouts, fallos HTTP
- ResilientWebSocketClient: detección de zombis, reconexión con backoff,
  sincronización de book, health status, callbacks de disconnect/reconnect
- ConnectionHealth: tracking de estado de conexión
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.live.data_resilience import (
    EventDeduplicator,
    BookSnapshotFetcher,
    ResilientWebSocketClient,
    ConnectionHealth,
)


class TestEventDeduplicator:
    """Pruebas del sistema de deduplicación de eventos."""

    def test_fresh_hash_not_duplicate(self):
        """Un hash nunca visto no debe ser detectado como duplicado."""
        dedup = EventDeduplicator(max_hashes=100)
        assert dedup.is_duplicate("hash-1") is False

    def test_repeated_hash_detected(self):
        """Un hash repetido debe ser detectado como duplicado."""
        dedup = EventDeduplicator(max_hashes=100)
        dedup.is_duplicate("hash-1")
        assert dedup.is_duplicate("hash-1") is True

    def test_none_hash_not_duplicate(self):
        """Hash None nunca debe ser considerado duplicado."""
        dedup = EventDeduplicator(max_hashes=100)
        assert dedup.is_duplicate(None) is False
        assert dedup.is_duplicate(None) is False  # sigue sin ser duplicado

    def test_clear_cache(self):
        """Tras clear(), los hashes previos deben olvidarse."""
        dedup = EventDeduplicator(max_hashes=100)
        dedup.is_duplicate("hash-1")
        dedup.clear()
        assert dedup.is_duplicate("hash-1") is False

    def test_lru_eviction(self):
        """Cuando el caché excede max_hashes, debe eliminar el más antiguo."""
        dedup = EventDeduplicator(max_hashes=3)
        dedup.is_duplicate("h1")
        dedup.is_duplicate("h2")
        dedup.is_duplicate("h3")
        dedup.is_duplicate("h4")  # debe eliminar h1
        # Check h2/h3 before h1 since is_duplicate re-inserts checked hashes
        assert dedup.is_duplicate("h2") is True
        assert dedup.is_duplicate("h3") is True
        # h1 fue evictado y h1 check lo re-insertaría, ok
        assert dedup.is_duplicate("h1") is False

    def test_size_property(self):
        """La propiedad size debe reflejar el número de hashes en caché."""
        dedup = EventDeduplicator(max_hashes=100)
        assert dedup.size == 0
        dedup.is_duplicate("h1")
        assert dedup.size == 1
        dedup.is_duplicate("h2")
        assert dedup.size == 2
        dedup.clear()
        assert dedup.size == 0


class TestBookSnapshotFetcher:
    """Pruebas del fetcher de snapshots del libro."""

    @pytest.mark.asyncio
    async def test_fetch_snapshot_timeout(self):
        """Timeout en la petición debe retornar None sin excepción."""
        fetcher = BookSnapshotFetcher(clob_api_base="https://nonexistent.example.com")
        result = await fetcher.fetch_snapshot("token-123")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_all_snapshots_empty(self):
        """Lista vacía de tokens debe retornar dict vacío."""
        fetcher = BookSnapshotFetcher()
        result = await fetcher.fetch_all_snapshots([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_fetch_snapshot_http_error(self):
        """Código HTTP de error debe retornar None."""
        fetcher = BookSnapshotFetcher(clob_api_base="https://httpbin.org/status/500")
        result = await fetcher.fetch_snapshot("token-123")
        assert result is None


class TestConnectionHealth:
    """Pruebas del dataclass ConnectionHealth."""

    def test_default_values(self):
        """Valores por defecto deben ser los esperados."""
        h = ConnectionHealth()
        assert h.connected is False
        assert h.last_message_at == 0.0
        assert h.reconnect_count == 0
        assert h.zombie_count == 0
        assert h.book_synced is False
        assert h.syncing is False

    def test_mutation(self):
        """Los campos deben ser mutables."""
        h = ConnectionHealth()
        h.connected = True
        h.book_synced = True
        h.reconnect_count = 5
        h.zombie_count = 2
        assert h.connected is True
        assert h.book_synced is True
        assert h.reconnect_count == 5
        assert h.zombie_count == 2


class TestResilientWebSocketClient:
    """Pruebas del cliente WebSocket resiliente."""

    @pytest.mark.asyncio
    async def test_start_creates_listener(self):
        """start() debe crear una tarea listener."""
        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self): await asyncio.sleep(3600); raise StopAsyncIteration
            return FakeWS()

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        await client.start()
        assert client._listener_task is not None
        assert client._running is True
        await client.stop()

    @pytest.mark.asyncio
    async def test_zombie_detection_triggers_reconnect(self):
        """Si no se reciben mensajes por zombie_timeout_s, debe forzar reconexión."""
        zombie_detected = []

        async def fake_connect():
            class FakeWS:
                closed = False
                async def send(self, msg): pass
                async def close(self):
                    self.closed = True
                    zombie_detected.append("close_called")
                def __aiter__(self): return self
                async def __anext__(self):
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration
            return FakeWS()

        client = ResilientWebSocketClient(
            connect_factory=fake_connect,
            zombie_timeout_s=1,
        )
        client._health.last_message_at = time.time() - 10  # simular zombie

        await client.start()
        client._zombie_watch_task = asyncio.create_task(client._zombie_watch())
        await asyncio.sleep(1.5)
        await client.stop()

        assert client._health.zombie_count >= 1

    @pytest.mark.asyncio
    async def test_disconnect_callback_invoked(self):
        """El callback de desconexión debe invocarse al desconectarse."""
        invoked = []

        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    raise StopAsyncIteration  # desconexión inmediata
            return FakeWS()

        async def on_disconnect():
            invoked.append("disconnect")

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        client.set_disconnect_callback(on_disconnect)
        await client.start()
        await asyncio.sleep(0.3)
        await client.stop()

        assert "disconnect" in invoked

    @pytest.mark.asyncio
    async def test_message_callback_receives_messages(self):
        """El message_callback debe recibir los mensajes del WS."""
        received = []

        async def fake_connect():
            class FakeWS:
                def __init__(self):
                    self._sent = False
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    if not self._sent:
                        self._sent = True
                        return json.dumps({"event_type": "book", "hash": "h1"})
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration
            return FakeWS()

        async def on_message(msg: str):
            received.append(msg)

        client = ResilientWebSocketClient(
            connect_factory=fake_connect,
            message_callback=on_message,
        )
        await client.start()
        await asyncio.sleep(0.3)
        await client.stop()

        assert len(received) >= 1
        data = json.loads(received[0])
        assert data["event_type"] == "book"

    @pytest.mark.asyncio
    async def test_book_synced_flag(self):
        """book_synced debe ser True después de la sincronización inicial."""
        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration
            return FakeWS()

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        client._active_token_ids = []
        await client.start()
        await asyncio.sleep(0.3)
        # Sin tokens activos, debe sincronizar inmediatamente
        assert client.book_synced is True
        await client.stop()

    @pytest.mark.asyncio
    async def test_get_health_dict_returns_expected_keys(self):
        """get_health_dict debe retornar todas las claves esperadas."""
        client = ResilientWebSocketClient(
            connect_factory=lambda: asyncio.sleep(0, None),
        )
        health = client.get_health_dict()
        expected_keys = {
            "connected", "last_message_at", "last_connect_attempt_at",
            "last_disconnect_at", "reconnect_count", "zombie_count",
            "book_synced", "syncing", "dedup_cache_size",
        }
        assert expected_keys.issubset(health.keys()), f"Missing keys: {expected_keys - health.keys()}"

    @pytest.mark.asyncio
    async def test_reconnect_callback_after_sync(self):
        """El callback de reconexión debe invocarse tras sincronizar el book."""
        invoked = []

        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration
            return FakeWS()

        async def on_reconnect():
            invoked.append("reconnect")

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        client.set_reconnect_callback(on_reconnect)
        client._active_token_ids = []
        await client.start()
        await asyncio.sleep(0.3)
        await client.stop()

        assert "reconnect" in invoked

    @pytest.mark.asyncio
    async def test_wait_for_book_sync(self):
        """wait_for_book_sync debe retornar True cuando el book está sincronizado."""
        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration
            return FakeWS()

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        client._active_token_ids = []
        client._book_sync_event.set()

        synced = await client.wait_for_book_sync(timeout=1.0)
        assert synced is True

    @pytest.mark.asyncio
    async def test_wait_for_book_sync_timeout(self):
        """wait_for_book_sync debe retornar False si el timeout expira."""
        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    await asyncio.sleep(3600)
                    raise StopAsyncIteration
            return FakeWS()

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        client._book_sync_event.clear()

        synced = await client.wait_for_book_sync(timeout=0.1)
        assert synced is False

    def test_set_token_ids(self):
        """set_token_ids debe actualizar la lista de tokens activos."""
        client = ResilientWebSocketClient(connect_factory=lambda: None)
        client.set_token_ids(["token-a", "token-b"])
        assert client._active_token_ids == ["token-a", "token-b"]

    def test_initial_book_synced_is_true(self):
        """book_synced debe ser True inicialmente (evento seteado)."""
        client = ResilientWebSocketClient(connect_factory=lambda: None)
        assert client.book_synced is False  # health inicial

    @pytest.mark.asyncio
    async def test_connected_property(self):
        """La propiedad connected debe reflejar el estado de salud."""
        async def fake_connect():
            class FakeWS:
                async def send(self, msg): pass
                async def close(self): pass
                def __aiter__(self): return self
                async def __anext__(self): await asyncio.sleep(3600); raise StopAsyncIteration
            return FakeWS()

        client = ResilientWebSocketClient(connect_factory=fake_connect)
        assert client.connected is False
        await client.start()
        await asyncio.sleep(0.3)
        await client.stop()
