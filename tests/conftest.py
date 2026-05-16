import asyncio
import json
import os
import time
from decimal import Decimal
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from ingesta import IngestaCLOB, NormalizedEvent
from src.risk.circuit_breakers import CircuitBreakerManager
from src.execution.order_lifecycle import OrderLifecycleManager


@pytest.fixture(autouse=True)
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_API_KEY", "test-api-key")
    monkeypatch.setenv("POLYMARKET_SECRET", base64_test_secret())
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("PRIVATE_KEY", "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")


def base64_test_secret() -> str:
    import base64
    return base64.b64encode(b"x" * 32).decode()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    try:
        loop.run_until_complete(asyncio.sleep(0.1))
    except RuntimeError:
        pass
    loop.shutdown_asyncgens()
    loop.close()


@pytest.fixture
def event_queue() -> asyncio.Queue:
    return asyncio.Queue()


@pytest.fixture
def signal_queue() -> asyncio.Queue:
    return asyncio.Queue()


@pytest.fixture
def execution_log_queue() -> asyncio.Queue:
    return asyncio.Queue()


@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    db = await aiosqlite.connect(":memory:")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            asset_id TEXT,
            market TEXT,
            side TEXT,
            price TEXT,
            size TEXT,
            usdc_amount TEXT,
            order_id TEXT,
            success INTEGER
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS balance_history (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            balance TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS daily_loss_tracker (
            date TEXT PRIMARY KEY,
            loss_accrued TEXT NOT NULL,
            start_balance TEXT NOT NULL
        )
    """)
    await db.commit()
    yield db
    await db.close()
    await asyncio.sleep(0)


class MockWebSocket:
    def __init__(self, messages: list[str] | None = None):
        self._messages = messages or []
        self._sent: list[str] = []
        self._index = 0
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, message: str) -> None:
        self._sent.append(message)

    async def recv(self) -> str:
        if self._index < len(self._messages):
            msg = self._messages[self._index]
            self._index += 1
            return msg
        await asyncio.sleep(3600)
        raise RuntimeError("no more messages")

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._index < len(self._messages):
            msg = self._messages[self._index]
            self._index += 1
            return msg
        raise StopAsyncIteration

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


@pytest.fixture
def mock_ws() -> MockWebSocket:
    return MockWebSocket()


class MockClobAPI:
    def __init__(self):
        self._orders: dict[str, dict[str, Any]] = {}
        self._call_count: dict[str, int] = {}
        self._fail_on: dict[str, int] = {}
        self._timeout_on: list[str] = []
        self._cancelled: list[str] = []

    def post_order(self, payload: dict) -> dict[str, Any]:
        self._call_count["post_order"] = self._call_count.get("post_order", 0) + 1
        if "post_order" in self._fail_on:
            fail_at = self._fail_on["post_order"]
            if self._call_count["post_order"] <= fail_at:
                return {"success": False, "error": "simulated_failure"}
        order_id = f"ord-{len(self._orders) + 1}"
        self._orders[order_id] = payload
        return {"success": True, "order_id": order_id}

    async def post_order_async(self, payload: dict) -> dict[str, Any]:
        if "post_order" in self._timeout_on:
            await asyncio.sleep(3600)
        return self.post_order(payload)

    def cancel_all(self) -> None:
        self._call_count["cancel_all"] = self._call_count.get("cancel_all", 0) + 1
        self._cancelled.extend(list(self._orders.keys()))
        self._orders.clear()

    async def cancel_all_async(self) -> None:
        self.cancel_all()

    def cancel_order(self, order_id: str) -> bool:
        self._call_count["cancel_order"] = self._call_count.get("cancel_order", 0) + 1
        if order_id in self._orders:
            self._cancelled.append(order_id)
            del self._orders[order_id]
            return True
        return False

    async def cancel_order_async(self, order_id: str) -> bool:
        return self.cancel_order(order_id)

    def fetch_open_orders(self) -> list[tuple[str, float]]:
        self._call_count["fetch_open"] = self._call_count.get("fetch_open", 0) + 1
        return [(oid, 1.0) for oid in self._orders]

    async def fetch_open_orders_async(self) -> list[tuple[str, float]]:
        return self.fetch_open_orders()

    def reset(self) -> None:
        self._orders.clear()
        self._call_count.clear()
        self._fail_on.clear()
        self._timeout_on.clear()
        self._cancelled.clear()


@pytest.fixture
def mock_clob() -> MockClobAPI:
    return MockClobAPI()


class MockFinBERT:
    def __init__(self, responses: list[dict[str, Any]] | None = None):
        self._responses = responses or [
            {"label": "positive", "score": 0.9},
            {"label": "neutral", "score": 0.7},
            {"label": "negative", "score": 0.8},
        ]
        self._call_count = 0

    async def __call__(self, texts: list[str]) -> list[dict[str, Any]]:
        self._call_count += 1
        return self._responses[:len(texts)]


@pytest.fixture
def mock_finbert() -> MockFinBERT:
    return MockFinBERT()


@pytest_asyncio.fixture
async def mock_balance_provider():
    async def provider() -> Decimal:
        return Decimal("1000")
    return provider


@pytest_asyncio.fixture
async def circuit_breaker_mgr(tmp_path, mock_clob) -> AsyncGenerator[CircuitBreakerManager, None]:
    db_path = str(tmp_path / "test_cb.db")

    cancelled = False
    async def cancel_all_cb():
        nonlocal cancelled
        cancelled = True
        mock_clob.cancel_all()

    async def fetch_open_cb() -> list[tuple[str, float]]:
        return mock_clob.fetch_open_orders_async()

    async def cancel_order_cb(oid: str) -> None:
        await mock_clob.cancel_order_async(oid)

    cb = CircuitBreakerManager(
        db_path=db_path,
        balance_provider=lambda: asyncio.sleep(0, Decimal("1000")),
        inventory_mtm_provider=lambda: asyncio.sleep(0, Decimal("0")),
        cancel_all_cb=cancel_all_cb,
        fetch_open_orders_cb=fetch_open_cb,
        cancel_order_cb=cancel_order_cb,
    )
    await cb.start()
    yield cb
    await cb.stop()


@pytest_asyncio.fixture
async def order_lifecycle(mock_clob) -> AsyncGenerator[OrderLifecycleManager, None]:
    async def place_func(od, sig, signal):
        result = await mock_clob.post_order_async(od)
        return result

    ol = OrderLifecycleManager(
        place_order_func=place_func,
        cancel_all_func=mock_clob.cancel_all_async,
        cancel_order_func=mock_clob.cancel_order_async,
        fetch_open_orders_func=mock_clob.fetch_open_orders_async,
        op_timeout_s=0.5,
        cycle_timeout_s=1.0,
        max_retries=2,
        stale_max_age_s=120,
    )
    yield ol


@pytest_asyncio.fixture
async def ingesta():
    ing = IngestaCLOB(
        asset_ids=["0xabc"],
        market_snapshot=[{
            "condition_id": "cond-1",
            "market": "Test market",
            "asset_ids": ["0xabc"],
            "tick_size": "0.01",
        }],
    )
    yield ing


@pytest.fixture
def market_snapshot() -> list[dict]:
    return [
        {
            "condition_id": "cond-1",
            "market": "Test market",
            "asset_ids": ["0xabc", "0xdef"],
            "tick_size": "0.01",
        },
        {
            "condition_id": "cond-2",
            "market": "Another market",
            "asset_ids": ["0x123"],
            "tick_size": "0.05",
        },
    ]


@pytest.fixture
def sample_book_event() -> dict:
    return {
        "event_type": "book",
        "asset_id": "0xabc",
        "market": "Test market",
        "bids": [{"price": "0.48", "size": "100.50"}],
        "asks": [{"price": "0.52", "size": "200.00"}],
        "timestamp": "1747216800000",
        "hash": "abc123",
    }


@pytest.fixture
def sample_signal() -> dict[str, Any]:
    return {
        "asset_id": "123456",
        "market": "Test market",
        "side": "BUY_YES",
        "price": "0.52",
        "size": "10.00",
        "probability": "0.55",
        "current_price": "0.52",
        "ev": "0.03",
        "tick_size": "0.01",
    }


@pytest.fixture
def mock_order_lifecycle():
    """Fixture que proporciona un OrderLifecycleManager mockeado con AsyncMock."""
    lifecycle = MagicMock()
    lifecycle.place_order_with_timeout = AsyncMock(return_value=MagicMock(success=True, order_id="0x123"))
    lifecycle._cancel_all_safe = AsyncMock()
    lifecycle.cancel_stale_orders = AsyncMock(return_value=0)
    lifecycle.clean_start = AsyncMock()
    lifecycle.cleanup_on_startup = AsyncMock()
    lifecycle.cleanup_on_shutdown = AsyncMock()
    lifecycle.execute_trading_cycle = AsyncMock()
    return lifecycle


@pytest.fixture
def mock_finbert_pipeline(monkeypatch):
    """Evita que MotorEstrategia cargue FinBERT desde HuggingFace."""
    monkeypatch.setattr(
        "estrategia.MotorEstrategia._ensure_sentiment_pipeline",
        AsyncMock(return_value="dummy"),
    )


@pytest.mark.parametrize("price_str, tick_str, expected_str", [
    ("0.515", "0.01", "0.52"),
    ("0.514", "0.01", "0.51"),
    ("0.10", "0.05", "0.10"),
    ("0.13", "0.05", "0.15"),
])
def test_price_quantization_regression(price_str: str, tick_str: str, expected_str: str) -> None:
    from decimal import Decimal, ROUND_HALF_UP
    tick = Decimal(tick_str)
    price = Decimal(price_str).quantize(tick, rounding=ROUND_HALF_UP)
    assert str(price) == expected_str, f"Expected {expected_str}, got {price}"
    assert isinstance(price, Decimal)
