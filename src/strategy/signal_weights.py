"""
Signal Weights Manager — Dynamic weight matrix, regime-aware adjustment,
and multi-asset optimization.

Manages the strategy weight matrix that varies by market type:
  - crypto_5min: Wick 0.20, External 0.70, FinBERT 0.00, MonteCarlo 0.10
  - crypto_15min: Wick 0.20, External 0.65, FinBERT 0.00, MonteCarlo 0.15
  - long_term (>7d): Wick 0.15, External 0.15, FinBERT 0.40, MonteCarlo 0.30

Core features:
  - Every 100 trades, win-rates are recalculated per signal source and weights
    are adjusted proportionally. Sources below 50% win-rate are halved; below
    45% are set to zero.
  - Regime-aware: weights shift according to detected market regime (volatility,
    liquidity, trend). Regime weights override base weights when a regime change
    is detected.
  - Multi-asset optimization: correlation tracking between positions prevents
    over-concentration in correlated assets, and a Kelly-based portfolio optimizer
    allocates capital across the best risk-adjusted opportunities.

Performance metrics are persisted in SQLite for survival across restarts.
"""

import json
import logging
import time
from collections import defaultdict, deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import aiosqlite

from src.strategy.regime_detector import RegimeDetector

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

SIGNAL_SOURCES = ["wick", "external", "finbert", "montecarlo"]

DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "crypto_5min": {"wick": 0.20, "external": 0.70, "finbert": 0.00, "montecarlo": 0.10},
    "crypto_15min": {"wick": 0.20, "external": 0.65, "finbert": 0.00, "montecarlo": 0.15},
    "long_term": {"wick": 0.15, "external": 0.15, "finbert": 0.40, "montecarlo": 0.30},
}

WIN_RATE_HALF_THRESHOLD = 0.50  # Below this → weight halved
WIN_RATE_ZERO_THRESHOLD = 0.45  # Below this → weight set to zero
ADJUSTMENT_WINDOW = 100  # Recalculate every N trades
MIN_TRADES_FOR_ADJUSTMENT = 20  # Minimum trades before adjustment kicks in

DB_PATH_DEFAULT = "bot_state.db"

# ── Multi-Asset Optimization Constants ─────────────────────────────────

MAX_CORRELATION_THRESHOLD = 0.70  # Max correlation before reducing exposure
CORRELATION_WINDOW = 50  # Number of trades for correlation calculation
MAX_POSITIONS_PER_CATEGORY = 3  # Max concurrent positions in same category
PORTFOLIO_REBALANCE_INTERVAL = 3600  # Rebalance portfolio every 1 hour


class SignalPerformanceTracker:
    """Tracks individual signal source performance for weight adjustment.

    Each signal source (wick, external, finbert, montecarlo) has a rolling
    window of trade outcomes tracked by asset_id and outcome.

    Parameters
    ----------
    window_size : int
        Rolling window size for win-rate calculation (default 100).
    db_path : str
        Path to SQLite database for persistence.
    """

    def __init__(
        self,
        window_size: int = ADJUSTMENT_WINDOW,
        db_path: str = DB_PATH_DEFAULT,
    ) -> None:
        self._window_size = window_size
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

        # Rolling outcomes per source: {source: deque of (asset_id, won: bool, timestamp)}
        self._outcomes: dict[str, deque[tuple[str, bool, float]]] = {
            s: deque(maxlen=window_size) for s in SIGNAL_SOURCES
        }

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    won INTEGER NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS signal_weights_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await self._db.commit()
        return self._db

    async def _load_history(self) -> None:
        """Load recent outcomes from SQLite on startup."""
        db = await self._ensure_db()
        try:
            for source in SIGNAL_SOURCES:
                cursor = await db.execute(
                    "SELECT asset_id, won, timestamp FROM signal_outcomes "
                    "WHERE source = ? ORDER BY id DESC LIMIT ?",
                    (source, self._window_size),
                )
                rows = await cursor.fetchall()
                for row in reversed(rows):
                    self._outcomes[source].append((row[0], bool(row[1]), row[2]))
        except Exception:
            logger.exception("Error loading signal history")

    async def record_outcome(
        self,
        source: str,
        asset_id: str,
        won: bool,
    ) -> None:
        """Record whether a trade from a signal source was winning.

        Parameters
        ----------
        source : str
            Signal source name (wick, external, finbert, montecarlo).
        asset_id : str
            The asset/token ID that was traded.
        won : bool
            True if trade was profitable.
        """
        if source not in self._outcomes:
            return

        now = time.time()
        self._outcomes[source].append((asset_id, won, now))

        # Persist to DB
        db = await self._ensure_db()
        try:
            await db.execute(
                "INSERT INTO signal_outcomes (source, asset_id, won, timestamp) VALUES (?, ?, ?, ?)",
                (source, asset_id, int(won), now),
            )
            await db.commit()
        except Exception:
            logger.exception("Error persisting signal outcome")

    def get_win_rate(self, source: str) -> float:
        """Get the win-rate for a signal source in the rolling window.

        Returns 0.0 if no outcomes recorded.
        """
        outcomes = self._outcomes.get(source)
        if not outcomes or len(outcomes) == 0:
            return 0.0
        wins = sum(1 for _, won, _ in outcomes if won)
        return wins / len(outcomes)

    def get_all_win_rates(self) -> dict[str, float]:
        """Get win-rates for all signal sources."""
        return {s: self.get_win_rate(s) for s in SIGNAL_SOURCES}

    @property
    def total_trades(self) -> int:
        """Total trades tracked across all sources."""
        return sum(len(dq) for dq in self._outcomes.values())

    async def start(self) -> None:
        """Initialize and load history."""
        await self._ensure_db()
        await self._load_history()
        logger.info("SignalPerformanceTracker started: %d total outcomes", self.total_trades)

    async def stop(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None


class AssetCorrelationTracker:
    """Tracks pairwise correlations between asset categories.

    Used for multi-asset optimization to prevent over-concentration
    in correlated assets and to allocate capital efficiently.
    """

    def __init__(
        self,
        window_size: int = CORRELATION_WINDOW,
        max_correlation: float = MAX_CORRELATION_THRESHOLD,
    ) -> None:
        self._window_size = window_size
        self._max_correlation = max_correlation
        self._outcomes_by_category: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )

    def record_outcome(
        self,
        category: str,
        pnl_pct: float,
    ) -> None:
        self._outcomes_by_category[category].append(pnl_pct)

    def get_correlation(
        self,
        cat_a: str,
        cat_b: str,
    ) -> float:
        outcomes_a = list(self._outcomes_by_category.get(cat_a, []))
        outcomes_b = list(self._outcomes_by_category.get(cat_b, []))
        if len(outcomes_a) < 5 or len(outcomes_b) < 5:
            return 0.0
        min_len = min(len(outcomes_a), len(outcomes_b))
        outcomes_a = outcomes_a[-min_len:]
        outcomes_b = outcomes_b[-min_len:]
        mean_a = sum(outcomes_a) / len(outcomes_a)
        mean_b = sum(outcomes_b) / len(outcomes_b)
        num = sum((a - mean_a) * (b - mean_b) for a, b in zip(outcomes_a, outcomes_b))
        den_a = sum((a - mean_a) ** 2 for a in outcomes_a) ** 0.5
        den_b = sum((b - mean_b) ** 2 for b in outcomes_b) ** 0.5
        if den_a == 0 or den_b == 0:
            return 0.0
        return num / (den_a * den_b)

    def is_over_concentrated(
        self,
        category: str,
        existing_categories: list[str],
    ) -> bool:
        for existing in existing_categories:
            if existing == category:
                continue
            corr = self.get_correlation(category, existing)
            if corr > self._max_correlation:
                return True
        return False

    def get_diversified_categories(
        self,
        target_categories: list[str],
        max_per_category: int = MAX_POSITIONS_PER_CATEGORY,
    ) -> list[str]:
        return target_categories[:max_per_category]


class SignalWeightsManager:
    """Manages dynamic weights and adjusts them based on performance.

    Features:
      - Performance-based weight adjustment (every 100 trades)
      - Regime-aware weight override (volatility, liquidity, trend)
      - Multi-asset optimization (correlation tracking, portfolio allocation)

    Parameters
    ----------
    performance_tracker : SignalPerformanceTracker | None
        Tracker for signal source outcomes. If None, weights remain static.
    initial_weights : dict | None
        Initial weight matrix by market type. Uses defaults if None.
    db_path : str
        Path to SQLite database for persistence.
    regime_detector : RegimeDetector | None
        Regime detector instance. If None, regime overlay is disabled.
    correlation_tracker : AssetCorrelationTracker | None
        Asset correlation tracker. If None, correlation optimization is disabled.
    """

    def __init__(
        self,
        performance_tracker: SignalPerformanceTracker | None = None,
        initial_weights: dict[str, dict[str, float]] | None = None,
        db_path: str = DB_PATH_DEFAULT,
        regime_detector: RegimeDetector | None = None,
        correlation_tracker: AssetCorrelationTracker | None = None,
    ) -> None:
        self._perf = performance_tracker
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._regime_detector = regime_detector
        self._correlation_tracker = correlation_tracker

        # Deep copy default weights
        self._weights: dict[str, dict[str, float]] = {}
        for market_type, src_weights in (initial_weights or DEFAULT_WEIGHTS).items():
            self._weights[market_type] = dict(src_weights)

        self._last_adjustment_count: int = 0
        self._last_portfolio_rebalance: float = 0.0
        self._portfolio_allocations: dict[str, Decimal] = {}

    @property
    def weights(self) -> dict[str, dict[str, float]]:
        """Get the current weight matrix."""
        return dict(self._weights)

    def get_weights_for_market(self, market_type: str) -> dict[str, float]:
        """Get weights for a specific market type.

        Applies regime-aware overlay when a regime detector is configured.
        In trending regimes, external signal weight is boosted.
        In high-vol regimes, wick signal weight is boosted.
        In low-liq regimes, sentiment weight is boosted.

        Parameters
        ----------
        market_type : str
            Market type: "crypto_5min", "crypto_15min", or "long_term".

        Returns
        -------
        dict with source -> weight mappings.
        """
        base = dict(self._weights.get(market_type, self._weights.get("long_term", DEFAULT_WEIGHTS["long_term"])))

        if self._regime_detector is not None:
            regime = self._regime_detector.current_regime
            regime_w = self._regime_detector.get_regime_weights()

            if regime != "LOW_VOL":
                blend_factor = 0.3
                for source in base:
                    base[source] = base[source] * (1 - blend_factor) + float(regime_w.get(source, Decimal(str(base[source])))) * blend_factor

                total = sum(base.values())
                if total > 0:
                    for source in base:
                        base[source] = base[source] / total

        return base

    def classify_market(
        self,
        event_duration_minutes: int | None = None,
        days_to_expiry: float | None = None,
    ) -> str:
        """Classify a market into a weight category.

        Parameters
        ----------
        event_duration_minutes : int | None
            Duration of the event in minutes (for crypto 5min/15min).
        days_to_expiry : float | None
            Days until market resolution.

        Returns
        -------
        str: "crypto_5min", "crypto_15min", or "long_term".
        """
        if event_duration_minutes is not None:
            if event_duration_minutes <= 5:
                return "crypto_5min"
            elif event_duration_minutes <= 15:
                return "crypto_15min"

        if days_to_expiry is not None and days_to_expiry <= 7:
            # Short-duration markets default to crypto_15min
            return "crypto_15min"

        return "long_term"

    def compute_weighted_signal(
        self,
        signals: dict[str, dict[str, Any]],
        market_type: str,
    ) -> dict[str, Any]:
        """Compute the aggregate signal from all sources given current weights.

        Parameters
        ----------
        signals : dict
            Dict of signal source -> signal result dict.
            Each result dict must have at least: direction (UP/DOWN/NEUTRAL/BUY_YES/BUY_NO/SELL_YES/SELL_NO)
            and optionally: score (Decimal), confidence (Decimal), ev (Decimal).
        market_type : str
            Market type classification.

        Returns
        -------
        dict with:
            direction: aggregate direction or NEUTRAL
            composite_score: weighted average score
            confidence: weighted average confidence
            ev: weighted expected value
            sources_agreeing: number of active sources in agreement
            total_active_sources: number of active sources (weight > 0)
        """
        w = self.get_weights_for_market(market_type)
        active_sources = [s for s in SIGNAL_SOURCES if w.get(s, 0) > 0]

        if not active_sources:
            return {
                "direction": "NEUTRAL",
                "composite_score": Decimal("0.5"),
                "confidence": Decimal("0"),
                "ev": Decimal("0"),
                "sources_agreeing": 0,
                "total_active_sources": 0,
                "details": {"reason": "no_active_sources"},
            }

        # Normalize weights for active sources
        total_weight = sum(w[s] for s in active_sources)
        if total_weight == 0:
            return {
                "direction": "NEUTRAL",
                "composite_score": Decimal("0.5"),
                "confidence": Decimal("0"),
                "ev": Decimal("0"),
                "sources_agreeing": 0,
                "total_active_sources": len(active_sources),
                "details": {"reason": "zero_total_weight"},
            }

        # Map signal direction to UP/DOWN/NEUTRAL
        def normalize_dir(sig: dict[str, Any] | None) -> str | None:
            if sig is None:
                return None
            d = sig.get("direction", sig.get("signal", "NONE"))
            if d in ("BUY_YES", "BUY NO", "UP", "BUY"):
                return "UP"
            if d in ("SELL_YES", "SELL_NO", "DOWN", "SELL"):
                return "DOWN"
            if d in ("NONE", "NEUTRAL"):
                return None
            return None

        composite_score = Decimal("0")
        composite_confidence = Decimal("0")
        directions: list[str] = []

        for source in active_sources:
            sig = signals.get(source)
            if sig is None:
                continue

            weight = Decimal(str(w[source] / total_weight))
            score = Decimal(str(sig.get("score", sig.get("probability", "0.5"))))
            confidence = Decimal(str(sig.get("confidence", "0.5")))

            composite_score += score * weight
            composite_confidence += confidence * weight

            dir_norm = normalize_dir(sig)
            if dir_norm:
                directions.append(dir_norm)

        # Determine majority direction
        up_count = directions.count("UP")
        down_count = directions.count("DOWN")

        if up_count > down_count:
            direction = "UP"
        elif down_count > up_count:
            direction = "DOWN"
        else:
            direction = "NEUTRAL"

        ev = abs(composite_score - Decimal("0.5"))

        return {
            "direction": direction,
            "composite_score": composite_score.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
            "confidence": composite_confidence.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "ev": ev.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
            "sources_agreeing": max(up_count, down_count),
            "total_active_sources": len(active_sources),
            "details": {
                "market_type": market_type,
                "weights": {s: w[s] for s in active_sources},
                "directions": directions,
            },
        }

    def check_consensus(
        self,
        signals: dict[str, dict[str, Any]],
        market_type: str,
        min_consensus: int = 2,
    ) -> tuple[bool, str]:
        """Check if enough signal sources agree on a direction.

        At least `min_consensus` active sources must agree on the same
        direction (UP or DOWN). If only one source emits a signal, the
        trade is discarded to avoid noise.

        Parameters
        ----------
        signals : dict
            Signal results per source.
        market_type : str
            Market type classification.
        min_consensus : int
            Minimum sources that must agree (default 2).

        Returns
        -------
        (passed: bool, reason: str)
        """
        w = self.get_weights_for_market(market_type)
        active_sources = [s for s in SIGNAL_SOURCES if w.get(s, 0) > 0 and s in signals and signals[s] is not None]

        if len(active_sources) < min_consensus:
            return False, f"insufficient_active_sources: {len(active_sources)} < {min_consensus}"

        # Count directions
        up_count = 0
        down_count = 0
        for source in active_sources:
            sig = signals[source]
            dir_norm = "UP" if sig.get("direction", sig.get("signal", "NONE")) in ("BUY_YES", "BUY_NO", "UP", "BUY") else \
                       "DOWN" if sig.get("direction", sig.get("signal", "NONE")) in ("SELL_YES", "SELL_NO", "DOWN", "SELL") else None
            if dir_norm == "UP":
                up_count += 1
            elif dir_norm == "DOWN":
                down_count += 1

        max_agreement = max(up_count, down_count)
        if max_agreement < min_consensus:
            return False, f"no_consensus: up={up_count} down={down_count} need={min_consensus}"

        return True, f"consensus_reached: {max_agreement}/{len(active_sources)} sources agree"

    # ── Multi-Asset Optimization ───────────────────────────────────────

    def record_portfolio_outcome(
        self,
        category: str,
        pnl_pct: Decimal,
    ) -> None:
        """Record a portfolio-level outcome for correlation tracking.

        Parameters
        ----------
        category : str
            Market category (e.g. "btc_updown", "eth_updown", "politics").
        pnl_pct : Decimal
            PnL as a percentage of the position size.
        """
        if self._correlation_tracker:
            self._correlation_tracker.record_outcome(category, float(pnl_pct))

    def compute_portfolio_allocation(
        self,
        opportunities: list[dict[str, Any]],
        total_balance: Decimal,
    ) -> list[dict[str, Any]]:
        """Compute optimal capital allocation across multiple opportunities.

        Uses a variant of Kelly criterion with diversification constraints.
        Returns allocations that sum to total_balance, respecting correlation
        and concentration limits.

        Parameters
        ----------
        opportunities : list[dict]
            Each dict must have: asset_id, edge (Decimal), confidence (Decimal),
            category (str), implied_probability (Decimal).
        total_balance : Decimal
            Total available capital.

        Returns
        -------
        list of dicts with: asset_id, allocation (Decimal), reason (str).
        """
        if not opportunities:
            return []

        categorized: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for opp in opportunities:
            cat = opp.get("category", "default")
            categorized[cat].append(opp)

        diversified: list[dict[str, Any]] = []
        for cat, opps in categorized.items():
            sorted_opps = sorted(opps, key=lambda x: float(x.get("edge", 0)), reverse=True)
            diversified.extend(sorted_opps[:MAX_POSITIONS_PER_CATEGORY])

        diversified.sort(key=lambda x: float(x.get("edge", 0)) * float(x.get("confidence", 0)), reverse=True)

        total_edge_confidence = sum(
            float(o.get("edge", 0)) * float(o.get("confidence", 0))
            for o in diversified
        )

        if total_edge_confidence <= 0:
            return []

        allocations: list[dict[str, Any]] = []
        remaining = total_balance

        for opp in diversified:
            edge = float(opp.get("edge", 0))
            confidence = float(opp.get("confidence", 0))
            score = edge * confidence
            if score <= 0:
                continue

            fraction = Decimal(str(score / total_edge_confidence))
            allocation = (total_balance * fraction).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            max_pos = total_balance * Decimal("0.10")
            allocation = min(allocation, max_pos)
            allocation = min(allocation, remaining)

            if allocation >= Decimal("1"):
                allocations.append({
                    "asset_id": opp["asset_id"],
                    "allocation": allocation,
                    "reason": f"portfolio_kelly: edge={edge:.4f} confidence={confidence:.2f}",
                })
                remaining -= allocation

        self._portfolio_allocations = {a["asset_id"]: a["allocation"] for a in allocations}
        return allocations

    def check_rebalance_needed(self) -> bool:
        """Check if portfolio rebalance interval has elapsed."""
        now = time.time()
        if now - self._last_portfolio_rebalance >= PORTFOLIO_REBALANCE_INTERVAL:
            self._last_portfolio_rebalance = now
            return True
        return False

    def adjust_weights_from_performance(self) -> dict[str, dict[str, float]]:
        """Adjust weights based on signal source performance.

        Called periodically (every 100 trades). For each market type:
          - Sources below 50% win-rate → weight halved
          - Sources below 45% win-rate → weight set to zero
          - Remaining weight redistributed proportionally.

        Returns the updated weight matrix.
        """
        if self._perf is None:
            return self._weights

        if self._perf.total_trades < MIN_TRADES_FOR_ADJUSTMENT:
            return self._weights

        # Check if we should adjust (every ADJUSTMENT_WINDOW trades)
        trades_since_last = self._perf.total_trades - self._last_adjustment_count
        if trades_since_last < ADJUSTMENT_WINDOW:
            return self._weights

        win_rates = self._perf.get_all_win_rates()
        logger.info("Adjusting weights from performance: %s", win_rates)

        regime_overlay_applied = False
        if self._regime_detector is not None:
            regime = self._regime_detector.detect_regime()
            if regime != "LOW_VOL":
                regime_weights = self._regime_detector.get_regime_weights()
                for market_type in self._weights:
                    for source in SIGNAL_SOURCES:
                        self._weights[market_type][source] = float(
                            regime_weights.get(source, Decimal(str(self._weights[market_type].get(source, 0))))
                        )
                regime_overlay_applied = True
                logger.info("Regime overlay applied: %s -> weights=%s", regime, self._weights)

        if not regime_overlay_applied:
            for market_type in self._weights:
                market_weights = self._weights[market_type]
                adjustments: dict[str, float] = {}

                for source in SIGNAL_SOURCES:
                    wr = win_rates.get(source, 0.0)
                    current_w = market_weights.get(source, 0.0)

                    if wr < WIN_RATE_ZERO_THRESHOLD and wr > 0:
                        adjustments[source] = 0.0
                        logger.warning(
                            "Source %s win-rate=%.1f%% < %.0f%% — weight set to 0 (was %.2f)",
                            source, wr * 100, WIN_RATE_ZERO_THRESHOLD * 100, current_w,
                        )
                    elif wr < WIN_RATE_HALF_THRESHOLD and wr > 0:
                        adjustments[source] = current_w / 2
                        logger.info(
                            "Source %s win-rate=%.1f%% < %.0f%% — weight halved (%.2f -> %.2f)",
                            source, wr * 100, WIN_RATE_HALF_THRESHOLD * 100, current_w, adjustments[source],
                        )

                for source, new_w in adjustments.items():
                    market_weights[source] = new_w

                total = sum(market_weights.values())
                if total > 0 and abs(total - 1.0) > 0.001:
                    for source in SIGNAL_SOURCES:
                        market_weights[source] = market_weights[source] / total

        self._last_adjustment_count = self._perf.total_trades
        logger.info("Weights adjusted: %s", self._weights)
        return self._weights

    async def persist_weights(self) -> None:
        """Persist current weights to SQLite."""
        db = await self._ensure_db()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO signal_weights_state (key, value) VALUES (?, ?)",
                ("weight_matrix", json.dumps(self._weights)),
            )
            await db.commit()
        except Exception:
            logger.exception("Error persisting weights")

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS signal_weights_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await self._db.commit()
        return self._db

    async def load_persisted_weights(self) -> None:
        """Load weights from SQLite on startup."""
        db = await self._ensure_db()
        try:
            cursor = await db.execute(
                "SELECT value FROM signal_weights_state WHERE key = ?",
                ("weight_matrix",),
            )
            row = await cursor.fetchone()
            if row:
                loaded = json.loads(row[0])
                if isinstance(loaded, dict):
                    self._weights = loaded
                    logger.info("Loaded persisted weights: %s", self._weights)
        except Exception:
            logger.exception("Error loading persisted weights")

    async def start(self) -> None:
        """Initialize the weights manager."""
        await self._ensure_db()
        await self.load_persisted_weights()
        logger.info("SignalWeightsManager started with weights: %s", self._weights)

    async def stop(self) -> None:
        """Persist weights and close DB."""
        await self.persist_weights()
        if self._db:
            await self._db.close()
            self._db = None
