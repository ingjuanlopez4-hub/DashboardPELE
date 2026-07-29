"""
Optimization Settings — Performance tuning configuration for local execution.

Controls event loop selection, CPU affinity, parallelism, caching, SQLite
performance, and profiling. Designed for single-machine (VPS or physical)
deployment only — not for Kubernetes or microservices.

All monetary values remain Decimal in the trading layer; float is used ONLY
in internal simulation loops and cache TTL calculations.
"""

from decimal import Decimal
from typing import Any, Dict

LOCAL_OPTIMIZATION_CONFIG: dict[str, Any] = {
    # ── Event Loop ────────────────────────────────────────────────────
    "event_loop": {
        "use_uvloop": True,
        "fallback_to_asyncio": True,
    },

    # ── CPU Affinity ──────────────────────────────────────────────────
    "cpu_affinity": {
        "enabled": True,
        "cores": {
            "ingestion": 0,
            "strategy": 1,
            "execution": 2,
            "monitoring": 3,
        },
    },

    # ── Monte Carlo ───────────────────────────────────────────────────
    "monte_carlo": {
        "use_numpy_vectorization": True,
        "use_process_pool": True,
        "max_workers": 2,
        "n_simulations": 1000,
    },

    # ── FinBERT ───────────────────────────────────────────────────────
    "finbert": {
        "use_onnx": True,
        "use_quantization": False,  # INT8 disabled by default (stability)
        "intra_op_threads": 2,
        "inter_op_threads": 1,
        "warmup": True,
    },

    # ── WebSocket ─────────────────────────────────────────────────────
    "websocket": {
        "library": "picows",       # Cython-based, 1.5-2x faster
        "fallback": "websockets",  # Pure Python fallback
    },

    # ── HTTP ──────────────────────────────────────────────────────────
    "http": {
        "max_connections": 50,
        "max_per_host": 20,
        "dns_cache_ttl": 300,
        "total_timeout_s": 5.0,
        "connect_timeout_s": 2.0,
    },

    # ── SQLite ────────────────────────────────────────────────────────
    "sqlite": {
        "wal_mode": True,
        "synchronous": "NORMAL",
        "cache_size_mb": 64,
        "mmap_size_mb": 256,
        "temp_store": "MEMORY",
        "batch_commit_interval_ms": 100,
        "page_size": 4096,
        "auto_vacuum": "INCREMENTAL",
    },

    # ── Cache ─────────────────────────────────────────────────────────
    "cache": {
        "l1_ttl_ms": 100,
        "l2_ttl_seconds": 5.0,
        "sentiment_ttl_seconds": 600,
        "order_book_ttl_ms": 50,
        "price_ttl_ms": 100,
        "maxsize_l1": 100,
        "maxsize_l2": 1000,
        "maxsize_sentiment": 1000,
    },

    # ── Parallelism ───────────────────────────────────────────────────
    "parallelism": {
        "max_concurrent_markets": 10,
        "max_concurrent_signals": 4,
    },

    # ── Profiling ─────────────────────────────────────────────────────
    "profiling": {
        "enabled": False,
        "top_n_functions": 20,
        "output_file": "profile_output.txt",
    },

    # ── System Limits ─────────────────────────────────────────────────
    "system": {
        "max_open_files": 65536,
        "tcp_rmem": "4096 87380 134217728",
        "tcp_wmem": "4096 65536 134217728",
        "tcp_fastopen": 3,
        "tcp_tw_reuse": 1,
    },
}


def get_optimization_config() -> dict[str, Any]:
    """Return the complete optimization configuration as a single dict."""
    return LOCAL_OPTIMIZATION_CONFIG


def get_ws_library() -> str:
    """Return the preferred WebSocket library name."""
    cfg = LOCAL_OPTIMIZATION_CONFIG["websocket"]
    try:
        import picows  # noqa: F401
        return cfg.get("library", "picows")
    except ImportError:
        return cfg.get("fallback", "websockets")


def get_cpu_cores() -> dict[str, int]:
    """Return the CPU core assignment mapping."""
    return LOCAL_OPTIMIZATION_CONFIG.get("cpu_affinity", {}).get("cores", {})


def should_use_uvloop() -> bool:
    """Check if uvloop should be used (Linux/macOS only)."""
    import platform
    if platform.system() not in ("Linux", "Darwin"):
        return False
    return LOCAL_OPTIMIZATION_CONFIG["event_loop"]["use_uvloop"]
