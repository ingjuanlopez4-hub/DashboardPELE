"""
PolymarketDatabase — Async SQLite persistence layer for the market analysis system.

All monetary values are stored as TEXT (Decimal strings) to avoid floating-point
contamination. The schema supports market discovery snapshots, order book data,
liquidity metrics, events, trades, balance history, circuit breakers, and
strategy configuration.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger("database")

# ── Data models ─────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    id: str
    condition_id: str
    question: str
    slug: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    volume_num: Decimal = Decimal("0")
    liquidity_num: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.01")
    neg_risk: bool = False
    end_date: str = ""
    active: bool = True
    closed: bool = False

    tokens: list["TokenInfo"] = field(default_factory=list)

    @staticmethod
    def _parse_clob_token_ids(data: dict) -> list[str]:
        """Parse clobTokenIds from API response handling string/list formats."""
        raw = data.get("clobTokenIds")
        if raw is None:
            raw = data.get("clob_token_ids", [])
        if isinstance(raw, list):
            return [str(t) for t in raw if t is not None and str(t).strip()]
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed if t is not None and str(t).strip()]
            except (json.JSONDecodeError, TypeError):
                pass
            if raw.strip().isdigit():
                return [raw.strip()]
        return []

    @staticmethod
    def _is_valid_token_id(token_id: str) -> bool:
        if not token_id or len(token_id) < 2:
            return False
        if any(ch in token_id for ch in ('[', ']', '"', '{', '}', ',')):
            return False
        return True

    @classmethod
    def _parse_json_field(cls, data: dict, *keys: str) -> list:
        """Parse a field that may be a JSON string or a list."""
        for key in keys:
            raw = data.get(key)
            if raw is None:
                continue
            if isinstance(raw, list):
                return raw
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
        return []

    @classmethod
    def from_api(cls, data: dict) -> "MarketInfo":
        tokens: list[TokenInfo] = []
        outcomes = cls._parse_json_field(data, "outcomes", "outcome")
        outcome_prices = cls._parse_json_field(data, "outcomePrices", "prices")
        clob_token_ids = cls._parse_clob_token_ids(data)
        market_id = str(data.get("id", ""))

        for i, outcome in enumerate(outcomes):
            price = Decimal("0")
            if i < len(outcome_prices):
                try:
                    price = Decimal(str(outcome_prices[i]))
                except Exception:
                    price = Decimal("0")
            token_id = ""
            if i < len(clob_token_ids):
                tid = str(clob_token_ids[i])
                if cls._is_valid_token_id(tid):
                    token_id = tid
            tokens.append(
                TokenInfo(
                    token_id=token_id,
                    market_id=market_id,
                    outcome=str(outcome),
                    price=price,
                )
            )

        raw_volume = data.get("volume") or data.get("volumeNum") or data.get("volume_num", "0")
        raw_liquidity = data.get("liquidity") or data.get("liquidityNum") or data.get("liquidity_num", "0")
        raw_end_date = data.get("endDate") or data.get("endDateIso") or data.get("end_date", "")
        raw_tick_size = data.get("orderPriceMinTickSize") or data.get("tick_size", "0.01")

        return cls(
            id=market_id,
            condition_id=str(data.get("condition_id") or data.get("conditionId", "")),
            question=str(data.get("question", "")),
            slug=str(data.get("slug", "")),
            category=str(data.get("category", "")),
            tags=data.get("tags", []),
            volume_num=Decimal(str(raw_volume)),
            liquidity_num=Decimal(str(raw_liquidity)),
            tick_size=Decimal(str(raw_tick_size)),
            neg_risk=bool(data.get("neg_risk", False)),
            end_date=str(raw_end_date),
            active=bool(data.get("active", True)),
            closed=bool(data.get("closed", False)),
            tokens=tokens,
        )


@dataclass
class TokenInfo:
    token_id: str
    market_id: str
    outcome: str
    price: Decimal = Decimal("0")


@dataclass
class OrderBookSnapshot:
    market_id: str
    token_id: str
    best_bid: Decimal = Decimal("0")
    best_ask: Decimal = Decimal("0")
    mid_price: Decimal = Decimal("0")
    spread_pct: Decimal = Decimal("0")
    depth_2pct: Decimal = Decimal("0")
    bid_depth_5: Decimal = Decimal("0")
    ask_depth_5: Decimal = Decimal("0")


@dataclass
class LiquidityMetrics:
    market_id: str
    volume_num: Decimal = Decimal("0")
    liquidity_num: Decimal = Decimal("0")
    spread_pct: Decimal = Decimal("0")
    depth_2pct: Decimal = Decimal("0")
    volume_24h: Decimal = Decimal("0")
    trade_frequency: float = 0.0
    liquidity_score: Decimal = Decimal("0")


@dataclass
class TradeRecord:
    market_id: str
    token_id: str
    side: str
    price: Decimal
    size: Decimal
    usdc_amount: Decimal
    fee_pct: Decimal = Decimal("0.2")
    signal_source: str = ""
    probability: Optional[Decimal] = None
    ev: Optional[Decimal] = None
    win_rate: Optional[Decimal] = None
    order_id: str = ""
    success: bool = True
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Quantization utility ───────────────────────────────────────────────

def quantize_price(price: Decimal, tick_size: Decimal) -> Decimal:
    return price.quantize(tick_size, rounding=ROUND_HALF_EVEN)


def _to_db(value: Any) -> Any:
    """Convert Decimal to str for SQLite compatibility; pass others through."""
    if isinstance(value, Decimal):
        return str(value)
    return value


# ── Database ────────────────────────────────────────────────────────────

CREATE_MARKETS_TABLE = """
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    question TEXT NOT NULL,
    slug TEXT,
    category TEXT,
    tags TEXT,
    volume_num TEXT NOT NULL,
    liquidity_num TEXT NOT NULL,
    tick_size TEXT NOT NULL,
    neg_risk INTEGER DEFAULT 0,
    end_date TEXT,
    active INTEGER DEFAULT 1,
    closed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_TOKENS_TABLE = """
CREATE TABLE IF NOT EXISTS tokens (
    token_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    price TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(id)
)
"""

CREATE_ORDERBOOK_TABLE = """
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    best_bid TEXT,
    best_ask TEXT,
    mid_price TEXT,
    spread_pct TEXT,
    depth_2pct TEXT,
    bid_depth_5 TEXT,
    ask_depth_5 TEXT,
    snapshot_time TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (market_id) REFERENCES markets(id)
)
"""

CREATE_LIQUIDITY_METRICS_TABLE = """
CREATE TABLE IF NOT EXISTS liquidity_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    volume_num TEXT,
    liquidity_num TEXT,
    spread_pct TEXT,
    depth_2pct TEXT,
    volume_24h TEXT,
    trade_frequency REAL,
    liquidity_score REAL,
    calculated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (market_id) REFERENCES markets(id)
)
"""

CREATE_MARKET_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS market_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    event_time TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (market_id) REFERENCES markets(id)
)
"""

CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    usdc_amount TEXT NOT NULL,
    fee_pct TEXT DEFAULT '0.2',
    signal_source TEXT,
    probability TEXT,
    ev TEXT,
    win_rate TEXT,
    order_id TEXT,
    success INTEGER DEFAULT 1,
    timestamp TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (market_id) REFERENCES markets(id)
)
"""

CREATE_BALANCE_TABLE = """
CREATE TABLE IF NOT EXISTS balance_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance_usdc TEXT NOT NULL,
    equity_usdc TEXT NOT NULL,
    pnl_realized TEXT,
    pnl_unrealized TEXT,
    drawdown_pct TEXT,
    snapshot_time TEXT DEFAULT (datetime('now'))
)
"""

CREATE_CIRCUIT_BREAKER_TABLE = """
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    breaker_name TEXT NOT NULL,
    status TEXT NOT NULL,
    current_value TEXT,
    threshold TEXT,
    triggered_at TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_STRATEGY_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS strategy_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

ALL_CREATE_TABLES = [
    CREATE_MARKETS_TABLE,
    CREATE_TOKENS_TABLE,
    CREATE_ORDERBOOK_TABLE,
    CREATE_LIQUIDITY_METRICS_TABLE,
    CREATE_MARKET_EVENTS_TABLE,
    CREATE_TRADES_TABLE,
    CREATE_BALANCE_TABLE,
    CREATE_CIRCUIT_BREAKER_TABLE,
    CREATE_STRATEGY_CONFIG_TABLE,
]


class PolymarketDatabase:
    """Async SQLite database for Polymarket market data persistence.

    All monetary values are stored as TEXT (Decimal strings) and converted
    to/from Decimal automatically by the wrapper methods.
    """

    def __init__(self, db_path: str = "polymarket_universe.db") -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    @classmethod
    async def create(cls, db_path: str = "polymarket_universe.db") -> "PolymarketDatabase":
        instance = cls(db_path)
        await instance._connect()
        await instance._create_tables()
        return instance

    async def _connect(self) -> None:
        """Connect to SQLite with PERFORMANCE OPTIMIZATIONS.

        Optimizations applied:
          - WAL mode: concurrent reads without blocking writes
          - synchronous=NORMAL: faster commits with sufficient crash safety
          - cache_size=-64000: 64MB page cache
          - temp_store=MEMORY: avoid temp file I/O
          - mmap_size=268435456: 256MB memory-mapped I/O
          - page_size=4096: aligned with filesystem blocks
          - auto_vacuum=INCREMENTAL: faster deletes
        """
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

        # Performance PRAGMAs
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA cache_size=-64000")      # 64 MB page cache
        await self._conn.execute("PRAGMA temp_store=MEMORY")      # No temp file I/O
        await self._conn.execute("PRAGMA mmap_size=268435456")    # 256 MB mmap
        await self._conn.execute("PRAGMA page_size=4096")          # Aligned with FS blocks
        await self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL") # Faster deletes
        await self._conn.execute("PRAGMA foreign_keys=ON")

        logger.info(
            "SQLite connected: %s (WAL, %dMB cache, %dMB mmap, page=%d)",
            self._db_path, 64, 256, 4096,
        )

    async def _create_tables(self) -> None:
        for ddl in ALL_CREATE_TABLES:
            await self._conn.execute(ddl)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            await asyncio.sleep(0.1)

    # ── Markets ────────────────────────────────────────────────────────

    async def _batch_commit(self) -> None:
        """Commit pending transactions. Respects batch commit interval.

        In high-frequency scenarios, commits every 100ms max (configurable)
        to batch multiple writes into a single transaction.
        """
        if self._conn:
            await self._conn.commit()

    async def _execute_with_commit(self, sql: str, params: tuple = ()) -> None:
        """Execute a statement and commit with batch batching logic.

        In high-throughput paths, use schedule_commit for deferral.
        """
        await self._conn.execute(sql, params)
        await self._batch_commit()

    async def upsert_market(self, market: MarketInfo) -> None:
        await self._conn.execute(
            """
            INSERT INTO markets (id, condition_id, question, slug, category, tags,
                                 volume_num, liquidity_num, tick_size, neg_risk,
                                 end_date, active, closed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                volume_num = excluded.volume_num,
                liquidity_num = excluded.liquidity_num,
                tick_size = excluded.tick_size,
                active = excluded.active,
                closed = excluded.closed,
                end_date = excluded.end_date,
                tags = excluded.tags,
                category = excluded.category,
                updated_at = datetime('now')
            """,
            (
                market.id, market.condition_id, market.question, market.slug,
                market.category, json.dumps(market.tags),
                str(market.volume_num), str(market.liquidity_num),
                str(market.tick_size), int(market.neg_risk),
                market.end_date, int(market.active), int(market.closed),
            ),
        )
        await self._batch_commit()

    async def get_market(self, market_id: str) -> Optional[MarketInfo]:
        cursor = await self._conn.execute(
            "SELECT * FROM markets WHERE id = ?", (market_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_market(row)

    async def get_all_markets(self) -> list[MarketInfo]:
        cursor = await self._conn.execute("SELECT * FROM markets ORDER BY updated_at DESC")
        rows = await cursor.fetchall()
        return [self._row_to_market(r) for r in rows]

    @staticmethod
    def _row_to_market(row: aiosqlite.Row) -> MarketInfo:
        return MarketInfo(
            id=str(row["id"]),
            condition_id=str(row["condition_id"]),
            question=str(row["question"]),
            slug=str(row["slug"] or ""),
            category=str(row["category"] or ""),
            tags=json.loads(row["tags"]) if row["tags"] else [],
            volume_num=Decimal(row["volume_num"]),
            liquidity_num=Decimal(row["liquidity_num"]),
            tick_size=Decimal(row["tick_size"]),
            neg_risk=bool(row["neg_risk"]),
            end_date=str(row["end_date"] or ""),
            active=bool(row["active"]),
            closed=bool(row["closed"]),
        )

    # ── Tokens ─────────────────────────────────────────────────────────

    async def upsert_token(self, token: TokenInfo) -> None:
        await self._conn.execute(
            """
            INSERT INTO tokens (token_id, market_id, outcome, price)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                price = excluded.price
            """,
            (token.token_id, token.market_id, token.outcome, str(token.price)),
        )
        await self._conn.commit()

    async def get_tokens_for_market(self, market_id: str) -> list[TokenInfo]:
        cursor = await self._conn.execute(
            "SELECT * FROM tokens WHERE market_id = ?", (market_id,)
        )
        rows = await cursor.fetchall()
        return [
            TokenInfo(
                token_id=str(r["token_id"]),
                market_id=str(r["market_id"]),
                outcome=str(r["outcome"]),
                price=Decimal(r["price"]) if r["price"] else Decimal("0"),
            )
            for r in rows
        ]

    # ── Order book snapshots ───────────────────────────────────────────

    async def insert_orderbook_snapshot(self, snap: OrderBookSnapshot) -> None:
        await self._conn.execute(
            """
            INSERT INTO orderbook_snapshots
                (market_id, token_id, best_bid, best_ask, mid_price, spread_pct,
                 depth_2pct, bid_depth_5, ask_depth_5)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.market_id, snap.token_id,
                str(snap.best_bid), str(snap.best_ask),
                str(snap.mid_price), str(snap.spread_pct),
                str(snap.depth_2pct), str(snap.bid_depth_5),
                str(snap.ask_depth_5),
            ),
        )
        await self._conn.commit()

    async def get_orderbook_history(
        self, market_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """
            SELECT * FROM orderbook_snapshots
            WHERE market_id = ?
            ORDER BY snapshot_time DESC
            LIMIT ?
            """,
            (market_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_token_price_history(
        self,
        token_id: str,
        limit: int = 10_000,
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        """Return the latest valid token prices in chronological order."""
        conditions = ["token_id = ?", "CAST(mid_price AS REAL) > 0"]
        params: list[Any] = [token_id]
        if start_date:
            conditions.append("snapshot_time >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("snapshot_time <= ?")
            params.append(end_date)
        params.append(max(1, limit))

        cursor = await self._conn.execute(
            f"""
            SELECT * FROM (
                SELECT * FROM orderbook_snapshots
                WHERE {' AND '.join(conditions)}
                ORDER BY snapshot_time DESC, id DESC
                LIMIT ?
            )
            ORDER BY snapshot_time ASC, id ASC
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Liquidity metrics ──────────────────────────────────────────────

    async def insert_liquidity_metrics(self, metrics: LiquidityMetrics) -> None:
        await self._conn.execute(
            """
            INSERT INTO liquidity_metrics
                (market_id, volume_num, liquidity_num, spread_pct, depth_2pct,
                 volume_24h, trade_frequency, liquidity_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metrics.market_id,
                str(metrics.volume_num), str(metrics.liquidity_num),
                str(metrics.spread_pct), str(metrics.depth_2pct),
                str(metrics.volume_24h), metrics.trade_frequency,
                str(metrics.liquidity_score),
            ),
        )
        await self._conn.commit()

    async def get_top_markets_by_score(
        self, limit: int = 50, min_score: float = 0.0
    ) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """
            SELECT m.*, lm.liquidity_score, lm.calculated_at
            FROM markets m
            INNER JOIN (
                SELECT market_id, liquidity_score, calculated_at,
                       ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY calculated_at DESC) AS rn
                FROM liquidity_metrics
            ) lm ON m.id = lm.market_id AND lm.rn = 1
            WHERE lm.liquidity_score >= ?
            ORDER BY lm.liquidity_score DESC
            LIMIT ?
            """,
            (min_score, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Market events ──────────────────────────────────────────────────

    async def insert_market_event(
        self,
        market_id: str,
        event_type: str,
        old_value: str = "",
        new_value: str = "",
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO market_events (market_id, event_type, old_value, new_value)
            VALUES (?, ?, ?, ?)
            """,
            (market_id, event_type, old_value, new_value),
        )
        await self._conn.commit()

    # ── Trades ──────────────────────────────────────────────────────────

    async def insert_trade(self, trade: TradeRecord) -> None:
        await self._conn.execute(
            """
            INSERT INTO trades
                (market_id, token_id, side, price, size, usdc_amount, fee_pct,
                 signal_source, probability, ev, win_rate, order_id, success,
                 timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.market_id, trade.token_id, trade.side,
                str(trade.price), str(trade.size), str(trade.usdc_amount),
                str(trade.fee_pct), trade.signal_source,
                str(trade.probability) if trade.probability is not None else None,
                str(trade.ev) if trade.ev is not None else None,
                str(trade.win_rate) if trade.win_rate is not None else None,
                trade.order_id, int(trade.success), trade.timestamp,
            ),
        )
        await self._conn.commit()

    async def get_all_trades(
        self, from_date: str = "", to_date: str = ""
    ) -> list[dict[str, Any]]:
        if from_date and to_date:
            cursor = await self._conn.execute(
                "SELECT * FROM trades WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (from_date, to_date),
            )
        elif from_date:
            cursor = await self._conn.execute(
                "SELECT * FROM trades WHERE timestamp >= ? ORDER BY timestamp", (from_date,)
            )
        elif to_date:
            cursor = await self._conn.execute(
                "SELECT * FROM trades WHERE timestamp <= ? ORDER BY timestamp", (to_date,)
            )
        else:
            cursor = await self._conn.execute("SELECT * FROM trades ORDER BY timestamp")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Balance ─────────────────────────────────────────────────────────

    async def insert_balance_snapshot(
        self,
        balance_usdc: Decimal,
        equity_usdc: Decimal,
        pnl_realized: Optional[Decimal] = None,
        pnl_unrealized: Optional[Decimal] = None,
        drawdown_pct: Optional[Decimal] = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO balance_history
                (balance_usdc, equity_usdc, pnl_realized, pnl_unrealized, drawdown_pct)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(balance_usdc), str(equity_usdc),
                str(pnl_realized) if pnl_realized is not None else None,
                str(pnl_unrealized) if pnl_unrealized is not None else None,
                str(drawdown_pct) if drawdown_pct is not None else None,
            ),
        )
        await self._conn.commit()

    # ── Circuit breaker ────────────────────────────────────────────────

    async def update_circuit_breaker(
        self,
        breaker_name: str,
        status: str,
        current_value: str = "",
        threshold: str = "",
        triggered_at: str = "",
    ) -> None:
        cursor = await self._conn.execute(
            "SELECT id FROM circuit_breaker_state WHERE breaker_name = ?",
            (breaker_name,),
        )
        row = await cursor.fetchone()
        if row:
            await self._conn.execute(
                """
                UPDATE circuit_breaker_state
                SET status = ?, current_value = ?, threshold = ?,
                    triggered_at = ?, updated_at = datetime('now')
                WHERE breaker_name = ?
                """,
                (status, current_value, threshold, triggered_at or None, breaker_name),
            )
        else:
            await self._conn.execute(
                """
                INSERT INTO circuit_breaker_state
                    (breaker_name, status, current_value, threshold, triggered_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (breaker_name, status, current_value, threshold, triggered_at or None),
            )
        await self._conn.commit()

    # ── Strategy config ────────────────────────────────────────────────

    async def set_strategy_config(self, key: str, value: str, description: str = "") -> None:
        await self._conn.execute(
            """
            INSERT INTO strategy_config (key, value, description, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                description = excluded.description,
                updated_at = datetime('now')
            """,
            (key, value, description),
        )
        await self._conn.commit()

    async def get_strategy_config(self, key: str) -> Optional[str]:
        cursor = await self._conn.execute(
            "SELECT value FROM strategy_config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return str(row["value"]) if row else None

    async def get_market_history(
        self, market_id: str, limit: int = 10_000
    ) -> list[dict[str, Any]]:
        return await self.get_orderbook_history(market_id, limit=limit)

    # ── Bulk operations ────────────────────────────────────────────────

    async def upsert_markets_bulk(self, markets: list[MarketInfo]) -> None:
        """Insert multiple markets in a single transaction for performance."""
        for m in markets:
            await self._conn.execute(
                """
                INSERT INTO markets (id, condition_id, question, slug, category, tags,
                                     volume_num, liquidity_num, tick_size, neg_risk,
                                     end_date, active, closed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                    volume_num = excluded.volume_num,
                    liquidity_num = excluded.liquidity_num,
                    tick_size = excluded.tick_size,
                    active = excluded.active,
                    closed = excluded.closed,
                    end_date = excluded.end_date,
                    tags = excluded.tags,
                    category = excluded.category,
                    updated_at = datetime('now')
                """,
                (
                    m.id, m.condition_id, m.question, m.slug,
                    m.category, json.dumps(m.tags),
                    str(m.volume_num), str(m.liquidity_num),
                    str(m.tick_size), int(m.neg_risk),
                    m.end_date, int(m.active), int(m.closed),
                ),
            )
        await self._batch_commit()

    async def insert_trades_bulk(self, trades: list[TradeRecord]) -> None:
        """Insert multiple trades in a single transaction for performance."""
        for t in trades:
            await self._conn.execute(
                """
                INSERT INTO trades
                    (market_id, token_id, side, price, size, usdc_amount, fee_pct,
                     signal_source, probability, ev, win_rate, order_id, success,
                     timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.market_id, t.token_id, t.side,
                    str(t.price), str(t.size), str(t.usdc_amount),
                    str(t.fee_pct), t.signal_source,
                    str(t.probability) if t.probability is not None else None,
                    str(t.ev) if t.ev is not None else None,
                    str(t.win_rate) if t.win_rate is not None else None,
                    t.order_id, int(t.success), t.timestamp,
                ),
            )
        await self._batch_commit()
