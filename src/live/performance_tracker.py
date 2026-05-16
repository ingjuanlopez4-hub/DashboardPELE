"""
PerformanceTracker — Tracks model prediction accuracy and dynamically adjusts
the minimum edge threshold to prevent trading when the model is performing poorly.

Uses Mean Absolute Error (MAE) of probability estimates vs actual outcomes.
When MAE exceeds a threshold, the min_edge is automatically increased
to require stronger signals before trading.

Formula: adjusted_min_edge = base_min_edge + MAE * adjustment_factor
Cap: max_min_edge prevents runaway adjustment.

State is persisted in SQLite for survival across restarts.
"""

import json
import logging
import time
from collections import deque
from decimal import Decimal
from typing import Any

import aiosqlite

logger = logging.getLogger("performance_tracker")

DB_PATH_DEFAULT = "bot_state.db"

# Defaults
BASE_MIN_EDGE = Decimal("0.05")
MAE_ADJUSTMENT_FACTOR = Decimal("1.5")
MAX_MIN_EDGE = Decimal("0.15")
MAE_WINDOW_SIZE = 100  # Number of resolved predictions to track


class PerformanceTracker:
    """Tracks prediction accuracy and dynamically adjusts min_edge.

    Parameters
    ----------
    db_path : str
        Path to SQLite database for persistence.
    base_min_edge : Decimal
        Base minimum edge (default 0.05 = 5%).
    mae_adjustment_factor : Decimal
        Multiplier applied to MAE when adjusting min_edge (default 1.5).
    max_min_edge : Decimal
        Cap on adjusted min_edge (default 0.15 = 15%).
    window_size : int
        Number of recent predictions to track for MAE calculation (default 100).
    """

    def __init__(
        self,
        db_path: str = DB_PATH_DEFAULT,
        base_min_edge: Decimal = BASE_MIN_EDGE,
        mae_adjustment_factor: Decimal = MAE_ADJUSTMENT_FACTOR,
        max_min_edge: Decimal = MAX_MIN_EDGE,
        window_size: int = MAE_WINDOW_SIZE,
    ) -> None:
        self._db_path = db_path
        self._base_min_edge = base_min_edge
        self._mae_factor = mae_adjustment_factor
        self._max_min_edge = max_min_edge
        self._window_size = window_size

        self._predictions: deque[dict[str, Any]] = deque(maxlen=window_size)
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS performance_tracker (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS prediction_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT,
                    predicted_prob TEXT,
                    actual_outcome INTEGER,
                    timestamp TEXT,
                    error_abs TEXT
                )
            """)
            await self._db.commit()
        return self._db

    async def _load_predictions(self) -> None:
        """Load recent predictions from SQLite on startup."""
        db = await self._ensure_db()
        try:
            cursor = await db.execute(
                "SELECT asset_id, predicted_prob, actual_outcome, timestamp, error_abs "
                "FROM prediction_log ORDER BY id DESC LIMIT ?",
                (self._window_size,),
            )
            rows = await cursor.fetchall()
            for row in reversed(rows):
                self._predictions.append({
                    "asset_id": row[0],
                    "predicted_prob": Decimal(row[1]),
                    "actual_outcome": bool(row[2]),
                    "timestamp": row[3],
                    "error_abs": Decimal(row[4]),
                })
        except Exception:
            logger.exception("Error loading predictions")

    async def record_prediction(
        self,
        asset_id: str,
        predicted_probability: Decimal,
        actual_outcome: bool,
    ) -> None:
        """Record a prediction and its actual outcome after market resolution.

        Parameters
        ----------
        asset_id : str
            The asset/token ID.
        predicted_probability : Decimal
            The bot's predicted probability (0-1).
        actual_outcome : bool
            True if YES outcome won, False otherwise.
        """
        actual_decimal = Decimal("1") if actual_outcome else Decimal("0")
        error_abs = abs(predicted_probability - actual_decimal)

        record = {
            "asset_id": asset_id,
            "predicted_prob": str(predicted_probability),
            "actual_outcome": int(actual_outcome),
            "timestamp": str(time.time()),
            "error_abs": str(error_abs),
        }
        self._predictions.append(record)

        # Persist to SQLite
        db = await self._ensure_db()
        try:
            await db.execute(
                "INSERT INTO prediction_log "
                "(asset_id, predicted_prob, actual_outcome, timestamp, error_abs) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    asset_id,
                    str(predicted_probability),
                    int(actual_outcome),
                    record["timestamp"],
                    str(error_abs),
                ),
            )
            await db.commit()
        except Exception:
            logger.exception("Error persisting prediction")

        current_mae = self.mae
        logger.info(
            "Prediction recorded: asset=%s predicted=%s actual=%s error_abs=%s (MAE=%.4f)",
            asset_id, str(predicted_probability), actual_outcome,
            str(error_abs), float(current_mae) if current_mae > 0 else 0,
        )

    @property
    def mae(self) -> Decimal:
        """Mean Absolute Error of recent predictions.

        Returns 0 if no predictions recorded.
        """
        if not self._predictions:
            return Decimal("0")
        total_error = sum(
            Decimal(p["error_abs"]) for p in self._predictions
        )
        return total_error / Decimal(str(len(self._predictions)))

    @property
    def adjusted_min_edge(self) -> Decimal:
        """Calculate the dynamically adjusted minimum edge.

        Formula: min_edge = base_min_edge + MAE * adjustment_factor
        Capped at max_min_edge.
        """
        current_mae = self.mae
        adjustment = current_mae * self._mae_factor
        adjusted = self._base_min_edge + adjustment
        capped = min(adjusted, self._max_min_edge)
        return capped.quantize(Decimal("0.0001"))

    @property
    def prediction_count(self) -> int:
        return len(self._predictions)

    async def start(self) -> None:
        """Initialize tracker and load history from DB."""
        await self._ensure_db()
        await self._load_predictions()
        logger.info(
            "PerformanceTracker started: %d predictions loaded, MAE=%.4f, adjusted_min_edge=%s",
            len(self._predictions), float(self.mae), str(self.adjusted_min_edge),
        )

    async def stop(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None
        logger.info("PerformanceTracker stopped")

    def get_stats(self) -> dict[str, Any]:
        """Return performance statistics for monitoring."""
        return {
            "mae": str(self.mae),
            "adjusted_min_edge": str(self.adjusted_min_edge),
            "base_min_edge": str(self._base_min_edge),
            "max_min_edge": str(self._max_min_edge),
            "prediction_count": self.prediction_count,
            "window_size": self._window_size,
        }
