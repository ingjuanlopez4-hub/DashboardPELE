"""
Live Mode Configuration — Conservative defaults for Polymarket production trading.

All monetary values use Decimal for precision (see py-clob-client issue #142).

MIGRATION GUIDE (to activate live mode):
  1. Set env vars: POLYMARKET_API_KEY, POLYMARKET_SECRET, POLYMARKET_PASSPHRASE, PRIVATE_KEY.
  2. Set DRY_RUN=false (default: true).
  3. Review RISK params below — start with CONSERVATIVE defaults.
  4. New params added for v2:
     - max_total_drawdown_pct (25%): permanent kill-switch
     - max_exposure_per_market_pct (10%): single-market cap
     - max_total_exposure_pct (50%): portfolio-wide cap
     - failure_window_seconds (1800): window for failure counting
     - max_consecutive_failures (5): triggers cooldown
     - cooldown_seconds (3600): trading pause after failures
     - zombie_timeout_seconds (60): WS zombie connection timeout
     - ws_disconnect_grace_seconds (10): grace before cancel_all
     - min_prob (0.30) / max_prob (0.70): probability filter range
     - min_volume_24h (5000): minimum 24h volume in USDC
     - min_hours_to_resolution (336): min 14 days to resolution
     - base_min_edge (0.05): dynamic edge floor
     - max_position_age_cycles (3): force-liquidate after N cycles
     - monitor_interval_seconds (60): cron monitor frequency
"""

from decimal import Decimal
from typing import Any

# ── Risk Parameters (CONSERVATIVE) ─────────────────────────────────────

RISK: dict[str, Any] = {
    # Position sizing
    "max_position_size_pct": Decimal("3.0"),       # Max 3% of balance per position
    "max_positions": 5,                             # Max concurrent open positions
    "kelly_fraction": Decimal("0.25"),              # Quarter-Kelly
    "min_edge_to_trade": Decimal("0.05"),           # 5% minimum expected edge

    # Loss limits
    "max_daily_loss_pct": Decimal("5.0"),           # Stop trading after 5% daily loss
    "max_drawdown_pct": Decimal("10.0"),            # Max drawdown from high-water mark
    "max_total_drawdown_pct": Decimal("25.0"),      # Permanent kill-switch (manual unblock only)

    # Cash management
    "cash_reserve_pct": Decimal("20.0"),            # Keep 20% of balance as free cash

    # Order lifecycle
    "op_timeout_s": 5.0,                            # Per-operation timeout (seconds)
    "cycle_timeout_s": 15.0,                        # Full cycle timeout (seconds)
    "max_retries": 2,                               # Max retries per order
    "stale_order_max_age_s": 120,                   # Cancel orders older than 2 min
    "stale_check_interval_s": 60,                   # Check every 60 seconds

    # Market filters
    "min_volume": 1000,                             # Minimum market volume in USDC
    "min_liquidity": Decimal("500"),                # Minimum liquidity in order book
    "exclude_prob_below": Decimal("0.05"),          # Exclude <5% probability
    "exclude_prob_above": Decimal("0.95"),          # Exclude >95% probability

    # ── NEW v2 parameters ──────────────────────────────────────────────
    # Exposure controls
    "max_exposure_per_market_pct": Decimal("10.0"), # Max 10% of balance in one market
    "max_total_exposure_pct": Decimal("50.0"),      # Max 50% of balance total at risk

    # Failure cooldown
    "failure_window_seconds": 1800,                 # Window for failure counting (30 min)
    "max_consecutive_failures": 5,                  # Triggers cooldown
    "cooldown_seconds": 3600,                       # Trading pause after failures (1 hr)

    # WebSocket resilience
    "zombie_timeout_seconds": 60,                   # Force reconnect if no message for 60s
    "ws_disconnect_grace_seconds": 10,              # Grace period before cancel_all on WS drop

    # Probability filter (avoid adverse selection in extremes)
    "min_prob": Decimal("0.30"),                    # Min probability to trade
    "max_prob": Decimal("0.70"),                    # Max probability to trade

    # Volume & time filters
    "min_volume_24h": Decimal("5000"),              # Min 24h volume in USDC
    "min_hours_to_resolution": 336,                 # Min 14 days to market resolution

    # Dynamic edge calibration
    "base_min_edge": Decimal("0.02"),               # Base min edge (reduced from 0.05)
    "mae_adjustment_factor": Decimal("1.5"),        # MAE multiplier for edge adjustment
    "max_min_edge": Decimal("0.15"),                # Cap on adjusted min edge

    # Position age limit
    "max_position_age_cycles": 3,                   # Force-liquidate after N candle cycles
    "cycle_duration_minutes": 15,                   # Candle duration in minutes (for age calc)

    # Take-profit / Stop-loss
    "take_profit_pct": Decimal("50.0"),             # TP at 50% gain
    "stop_loss_pct": Decimal("30.0"),               # SL at 30% loss

    # Cron monitor
    "monitor_interval_seconds": 60,                 # Health check frequency
    "balance_discrepancy_threshold_usdc": Decimal("1.0"),  # Alert if balance differs > 1 USDC

    # ── NEW v3 parameters (rentabilidad) ───────────────────────────────
    # Strategy
    "min_edge": Decimal("0.02"),                    # Reduced from 0.05 to 0.02
    "max_position_size_pct": Decimal("3.0"),       # Max 3% per trade (was in RISK already)
    "min_consensus": 2,                             # At least 2 modules must agree
    "kelly_fraction": Decimal("0.25"),              # Keep quarter-Kelly

    # External signal
    "external_signal": {
        "enabled": True,
        "binance_streams": ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt"],
        "min_distance_pct": Decimal("0.15"),
        "cache_ttl_ms": 100,
    },

    # Maker-first execution
    "maker_first": {
        "enabled": True,
        "maker_timeout_seconds": 15,
        "cross_spread_only_if_edge_above": Decimal("0.03"),
    },

    # Risk reinforced
    "risk_v3": {
        "daily_loss_pct": Decimal("5.0"),
        "monthly_loss_pct": Decimal("15.0"),
        "total_drawdown_pct": Decimal("25.0"),
        "per_trade_max_pct": Decimal("3.0"),
        "min_liquidity_usd": Decimal("5000"),
        "max_slippage_pct": Decimal("3.0"),
        "stop_loss_per_position_pct": Decimal("20.0"),
        "trailing_profit_lock": {"gain_pct": Decimal("15.0"), "retrace_pct": Decimal("10.0")},
        "correlated_exposure_max_pct": Decimal("25.0"),
    },

    # FinBERT config
    "finbert": {
        "enabled_for_markets": ["long_term"],
        "batch_size": 16,
        "cache_ttl_seconds": 600,
        "update_interval_seconds": 300,
    },

    # Monte Carlo config
    "montecarlo": {
        "enabled_for_markets": ["long_term"],
        "n_simulations": 1000,
        "update_interval_minutes": 60,
        "use_float_internally": True,
    },

    # ── NEW v4 parameters ──────────────────────────────────────────────
    # Slippage model
    "slippage": {
        "maker_slippage_bps": Decimal("0"),
        "taker_slippage_bps": Decimal("5"),
        "depth_tiers": [
            {"level": 1, "depth_usdc": Decimal("1000"), "slippage_bps": Decimal("1")},
            {"level": 2, "depth_usdc": Decimal("2500"), "slippage_bps": Decimal("3")},
            {"level": 3, "depth_usdc": Decimal("5000"), "slippage_bps": Decimal("5")},
            {"level": 4, "depth_usdc": Decimal("10000"), "slippage_bps": Decimal("10")},
            {"level": 5, "depth_usdc": Decimal("25000"), "slippage_bps": Decimal("20")},
        ],
        "fallback_slippage_bps": Decimal("15"),
        "max_slippage_bps": Decimal("50"),
    },

    # Regime detector
    "regime_detector": {
        "enabled": True,
        "vol_window": 50,
        "liq_window": 50,
        "trend_window": 20,
        "vol_threshold_high": Decimal("0.03"),
        "vol_threshold_chaotic": Decimal("0.06"),
        "liq_threshold_low": Decimal("2000"),
        "trend_threshold": Decimal("0.015"),
        "recalibration_cooldown_seconds": 3600,
    },

    # On-chain signals
    "onchain": {
        "enabled": True,
        "polygon_rpc": "https://polygon-rpc.com",
        "whale_threshold_usdc": Decimal("100000"),
        "gas_spike_threshold_gwei": Decimal("150"),
        "poll_interval_seconds": 5.0,
        "min_confidence": Decimal("0.3"),
    },

    # Live FinBERT pipeline
    "live_sentiment": {
        "enabled": True,
        "update_interval_seconds": 300,
        "min_articles_for_real_sentiment": 1,
        "use_keyword_fallback": True,
        "use_category_baseline": True,
    },

    # Multi-asset optimization
    "multi_asset": {
        "enabled": True,
        "max_correlation_threshold": 0.70,
        "correlation_window": 50,
        "max_positions_per_category": 3,
        "portfolio_rebalance_interval": 3600,
    },

    # Infrastructure
    "infrastructure": {
        "vps_location": "eu-west",
        "polygon_rpc": "https://rpc.ankr.com/polygon",
        "websocket_reconnect_backoff": [1, 2, 4, 8, 16],
    },
}

# ── Opportunity Windows (Endcycle Sniper pattern) ──────────────────────

OPPORTUNITY_WINDOWS: dict[str, dict[str, Any]] = {
    "15m": {
        "window_before_end_s": 90,                  # Evaluate only last 90 seconds
        "min_edge_post_fee": Decimal("0.03"),       # 3% min edge after fees
        "max_sizing_pct": Decimal("20.0"),          # Up to 20% of balance at end
    },
    "5m": {
        "window_before_end_s": 30,                  # Evaluate only last 30 seconds
        "min_edge_post_fee": Decimal("0.04"),       # 4% min edge after fees
        "max_sizing_pct": Decimal("10.0"),          # Up to 10% of balance
    },
    "default": {
        "window_before_end_s": 60,
        "min_edge_post_fee": Decimal("0.05"),
        "max_sizing_pct": Decimal("5.0"),
    },
}

# ── Dynamic Fee Formula (Polymarket 2026) ──────────────────────────────

# Fee = C * 0.25 * (p * (1-p))^2
# Max fee ~1.56% at p=0.5
FEE_CONSTANT_C = Decimal("1.0")

def dynamic_taker_fee(probability: Decimal) -> Decimal:
    """Calculate the dynamic taker fee for a given probability.

    Formula: fee = C * 0.25 * (p * (1-p))^2
    where C = 1.0 (Polymarket 2026 parameter).
    """
    p = min(max(probability, Decimal("0")), Decimal("1"))
    product = p * (Decimal("1") - p)
    fee = FEE_CONSTANT_C * Decimal("0.25") * (product * product)
    return fee.quantize(Decimal("0.0001"))

def estimate_post_fee_edge(
    probability: Decimal,
    current_price: Decimal,
    size: Decimal,
    slippage_bps: Decimal | None = None,
) -> Decimal:
    """Estimate edge after accounting for dynamic taker fee, spread, and slippage.

    Uses the slippage model when slippage_bps is provided, otherwise falls back
    to a 1-tick (1%) estimate for backward compatibility.

    Returns the post-fee edge as a Decimal. If negative, do not trade.
    """
    raw_edge = abs(probability - current_price)
    fee_rate = dynamic_taker_fee(current_price)
    if slippage_bps is not None:
        slippage_dec = slippage_bps / Decimal("10000")
    else:
        slippage_dec = Decimal("0.01")
    post_fee = raw_edge - fee_rate - slippage_dec
    return post_fee.quantize(Decimal("0.0001"))

# ── Sizing by Confidence Score ─────────────────────────────────────────

SIZING_TABLE: list[dict[str, Any]] = [
    {"min_score": Decimal("80"), "max_score": Decimal("100"), "balance_pct": Decimal("20.0")},
    {"min_score": Decimal("60"), "max_score": Decimal("79"), "balance_pct": Decimal("10.0")},
    {"min_score": Decimal("40"), "max_score": Decimal("59"), "balance_pct": Decimal("5.0")},
    {"min_score": Decimal("20"), "max_score": Decimal("39"), "balance_pct": Decimal("2.0")},
    {"min_score": Decimal("0"), "max_score": Decimal("19"), "balance_pct": Decimal("0.0")},
]

def position_size_from_score(score: Decimal, balance: Decimal) -> Decimal:
    """Determine position size based on confidence score and balance.

    Score 80-100 -> 20% of balance
    Score 60-79  -> 10% of balance
    Score 40-59  ->  5% of balance
    Score 20-39  ->  2% of balance
    Score 0-19   ->  0% (do not trade)
    """
    for tier in SIZING_TABLE:
        if tier["min_score"] <= score <= tier["max_score"]:
            pct = tier["balance_pct"]
            size = balance * (pct / Decimal("100"))
            return size.quantize(Decimal("0.01"))
    return Decimal("0")

# ── API & Cost Management ─────────────────────────────────────────────

COST_LIMITS: dict[str, Any] = {
    "daily_ai_cost_limit": Decimal("10.0"),         # Max $10/day on LLM inference
    "daily_ai_cost_spent": Decimal("0.0"),          # Reset daily
}

# ── Exposure Limits by Category ────────────────────────────────────────

EXPOSURE_LIMITS: dict[str, dict[str, Decimal]] = {
    "btc_updown": {
        "max_total_exposure_pct": Decimal("30.0"),
        "max_position_pct": Decimal("5.0"),
    },
    "eth_updown": {
        "max_total_exposure_pct": Decimal("25.0"),
        "max_position_pct": Decimal("4.0"),
    },
    "politics": {
        "max_total_exposure_pct": Decimal("15.0"),
        "max_position_pct": Decimal("3.0"),
    },
    "sports": {
        "max_total_exposure_pct": Decimal("20.0"),
        "max_position_pct": Decimal("3.0"),
    },
    "default": {
        "max_total_exposure_pct": Decimal("20.0"),
        "max_position_pct": Decimal("3.0"),
    },
}

# ── Alerting ────────────────────────────────────────────────────────────

ALERTING: dict[str, Any] = {
    "discord_webhook_url": "",                      # Set via env or config file
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "alert_on_critical": True,
    "alert_on_warning": False,
}

# ── Prometheus / Observability ─────────────────────────────────────────

MONITORING: dict[str, Any] = {
    "health_port": 8080,
    "metrics_enabled": True,
    "structured_logs_json": True,
    "alert_if_idle_minutes": 5,
}

# ── Compute full config ────────────────────────────────────────────────

def get_live_config() -> dict[str, Any]:
    """Return the complete live mode configuration as a single dict."""
    return {
        "risk": RISK,
        "opportunity_windows": OPPORTUNITY_WINDOWS,
        "cost_limits": COST_LIMITS,
        "exposure_limits": EXPOSURE_LIMITS,
        "monitoring": MONITORING,
        "sizing_table": SIZING_TABLE,
        "alerting": ALERTING,
    }
