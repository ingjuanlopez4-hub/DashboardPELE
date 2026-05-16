# PELE — Predictive Event Liquidity Engine

**Bot de trading algorítmico para Polymarket** · Combina tres señales independientes (Wick‑Fishing, FinBERT, Monte Carlo) con ejecución segura, circuit breakers y generación automática de whitepapers.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](#)
[![Tests](https://img.shields.io/badge/tests-467%20passing-green.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#)

---

**PELE** is a production-grade algorithmic trading bot for [Polymarket](https://polymarket.com) prediction markets. It fuses three independent signal sources — order‑book manipulation detection, financial NLP sentiment, and Monte Carlo simulation — into a single trading decision, executes through the CLOB API with full lifecycle management, and can auto‑generate professional HTML whitepapers with interactive Plotly charts.

## Features

### Signal Pipeline (3 independent strategies, configurable weights)

| Signal | Source | Method |
|---|---|---|
| **Wick‑Fishing** | Order book snapshots (CLOB) | Detects large bid/ask placements that are suddenly removed — a common manipulation pattern. Produces an adjusted probability signal. |
| **FinBERT** | NewsAPI, RSS, Reddit | Financial sentiment analysis via `ProsusAI/finbert`. Cascading fallback: ONNX → PyTorch → neutral. Singleton model, cached per‑market. |
| **Monte Carlo** | Market data (implied vol, spread) | 10,000 Geometric Brownian Motion paths in log‑odds space. Returns expected value distribution and confidence bands. |

Signals are fused via configurable weights (`w_wick`, `w_sentiment`, `w_montecarlo`).

### Data Pipeline (Whitepaper)

1. **Market Discovery** — Fetches all active Polymarket markets via the Gamma API (with pagination, retries, rate limiting).
2. **Liquidity Scoring** — Weighted score from volume, order‑book depth, spread, bid/ask ratio, and time to resolution.
3. **Market Selection** — Configurable thresholds (min score, volume, liquidity, probability range, days to resolution).
4. **Backtest** — Runs the fused strategy on historical data across selected markets.
5. **Parameter Sweep** — Grid search over `min_edge`, `kelly_fraction`, `max_position_size`, and signal weights.
6. **Robustness Analysis** — Permutation test (10,000 shuffles), Monte Carlo equity bands (95/99%), out‑of‑sample test.
7. **Whitepaper Generation** — Professional HTML report with Plotly interactive charts (equity curves, drawdown, heatmaps, distributions).

### Live Trading Pipeline

- **WebSocket Ingestion** — Connects to the Polymarket CLOB WebSocket with automatic reconnection, zombie detection (no messages for 60s → reconnect), snapshot sync, event deduplication, and periodic book refresh.
- **Market Filter** — Only trades within opportunity windows (last N seconds of a candle), probability range [0.30, 0.70], min 24h volume > $5k, min 14 days to resolution, and dynamic edge calibration via MAE tracking.
- **Order Lifecycle** — Timeout‑safe placement (`asyncio.wait_for`), configurable retry budget (max 2), unconditional `cancel_all` on failure. Decorator‑based cycle timeout.
- **Order Guard** — `clean_start()` cancels all orders on boot. Periodic watchdog (every 30s) cancels stale orders (>120s old). Proactive cancel‑all on WebSocket disconnect.
- **Position Manager** — TP/SL monitoring, max position age enforcement (force‑close after N cycles), market‑category exposure limits.
- **Circuit Breakers** — Daily drawdown (5%), total drawdown (10% / 25% permanent kill), max exposure per market (10%) and total (50%), failure cooldown (5 failures → 1h pause), cash reserve (20%). Persisted in SQLite.
- **Monitoring** — Health HTTP endpoint (port 8080), Prometheus metrics, periodic reconciliation (balance vs on‑chain), Discord/Telegram alerts, structured JSON logging.

### Precision

All monetary values (prices, quantities, PnL, balances) use **`Decimal`** throughout, quantized to each market's tick size. SQLite stores Decimals as TEXT. This eliminates the floating‑point rounding errors documented in the official `py-clob-client` (see issue #142).

---

## Requirements

- Python 3.11+
- Dependencies in `requirements.txt`:
  - Core: `aiohttp`, `websockets`, `web3`, `eth-account`, `aiosqlite`, `numpy`, `scipy`, `python-dotenv`, `tenacity`, `cachetools`
  - Optional (FinBERT): `transformers`, `torch`, `optimum[onnxruntime]`, `onnxruntime`
  - Dev: `pytest`, `pytest-asyncio`
- For live trading: a Polygon wallet with USDC and Polymarket API credentials.

---

## Installation

```bash
git clone https://github.com/your-org/pele.git
cd pele
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
```

---

## Configuration

Edit `.env` (see `.env.example`):

```ini
PRIVATE_KEY=0x_your_private_key_hex_here
POLYMARKET_API_KEY=your_api_key
POLYMARKET_SECRET=your_base64_secret
POLYMARKET_PASSPHRASE=your_passphrase
DRY_RUN=true                          # Set to false for real trades
```

Strategy parameters and circuit‑breaker thresholds are tuned in `src/config/live_settings.py`:

| Parameter | Default | Description |
|---|---|---|
| `max_position_size_pct` | 3.0% | Per‑position balance cap |
| `max_positions` | 5 | Max concurrent positions |
| `kelly_fraction` | 0.25 | Fractional Kelly sizing |
| `min_edge_to_trade` | 5% | Minimum expected edge |
| `max_daily_loss_pct` | 5.0% | Daily loss limit |
| `max_drawdown_pct` | 10.0% | Drawdown from HWM |
| `max_total_drawdown_pct` | 25.0% | Permanent kill‑switch |
| `max_consecutive_failures` | 5 | Triggers cooldown |
| `cooldown_seconds` | 3600 | Cooldown duration |

---

## Usage

The CLI is available as `scripts/entry_point.py` (aliased as `pele`):

```bash
# Generate a full whitepaper (discovery + backtest + sweep + robustness + HTML report)
python scripts/entry_point.py whitepaper --top-n 50

# Quick backtest (no sweep, top 20 markets)
python scripts/entry_point.py backtest --top-n 20

# Start live trading in dry-run (default) — validates everything without real orders
python scripts/entry_point.py live

# Start live trading with real funds (only after dry-run validation!)
python scripts/entry_point.py live --dry-run false

# Pre-flight checklist — verifies API keys, wallet, balance, WebSocket, circuit breakers
python scripts/entry_point.py check

# Standalone whitepaper pipeline (more options)
python scripts/run_whitepaper_pipeline.py --top-n 50 --no-sweep --log-level DEBUG
```

### Example output

```
Markets discovered:  10100
Markets tracked:      10022
Markets selected:        50

BACKTEST COMPLETE in 148.3 seconds
  Net PnL:            +152.40 USDC
  Sharpe ratio:        1.2345
  Sortino ratio:       1.8765
  Max drawdown:        8.23%
  Win rate:            55.2%
  Total trades:        487
  Profit factor:       1.4523

Whitepaper generated: whitepaper_output/whitepaper_20260516.html
```

---

## Project Structure

```
pele/
├── scripts/
│   ├── entry_point.py              # Unified CLI (whitepaper / live / backtest / check)
│   ├── run_pipeline.py             # Pipeline orchestrator (discovery → backtest)
│   ├── run_whitepaper_pipeline.py  # Full whitepaper pipeline (includes sweep + robustness)
│   ├── run_live.py                 # Live trading entry point
│   ├── run_market_analysis.py      # Market analysis utility
│   └── live_production_checklist.py# 7‑step pre‑flight validation script
├── src/
│   ├── config/
│   │   └── live_settings.py        # All configurable parameters in one place
│   ├── data/
│   │   ├── database.py             # Async SQLite layer (690 lines, full schema)
│   │   ├── gamma_client.py         # Gamma API client (markets, pagination)
│   │   ├── market_discovery.py     # Discover + score all active markets
│   │   ├── liquidity_analyzer.py   # Weighted liquidity scoring
│   │   ├── market_tracker.py       # Track markets, store snapshots
│   │   └── market_selector.py      # Filter + rank markets by quality
│   ├── strategy/
│   │   ├── wick_fishing.py         # Order‑book manipulation detection
│   │   ├── finbert_sentiment.py    # Financial NLP (ONNX/PyTorch, singleton)
│   │   ├── finbert_config.py       # FinBERT model configuration
│   │   ├── finbert_utils.py        # Preprocessing, compound scoring
│   │   ├── monte_carlo.py          # GBM simulation on log-odds (10k paths)
│   │   └── news_fetcher.py         # NewsAPI + RSS + Reddit fetcher
│   ├── execution/
│   │   └── order_lifecycle.py      # Timeout‑safe orders, retry, cancel‑on‑fail
│   ├── risk/
│   │   └── circuit_breakers.py     # 10+ circuit breakers, persisted state
│   ├── live/
│   │   ├── data_resilience.py      # WS reconnection, zombie detection, dedup, snapshots
│   │   ├── market_filter.py        # Opportunity windows, prob/volume/time filters
│   │   ├── order_guard.py          # Clean start, watchdog, WS disconnect handler
│   │   ├── position_manager.py     # TP/SL tracking, position age enforcement
│   │   ├── performance_tracker.py  # MAE tracking, dynamic edge calibration
│   │   ├── preflight.py            # Pre‑flight environment validation
│   │   ├── alerting.py             # Discord/Telegram alerts with rate limiting
│   │   ├── async_manager.py        # aiohttp session + resource lifecycle
│   │   └── monitor.py              # Cron monitor, reconciliation, health HTTP endpoint
│   └── whitepaper/
│       ├── strategy_runner.py      # Backtest engine (trades over historical data)
│       ├── parameter_sweep.py      # Grid search over strategy parameters
│       ├── robustness_analyzer.py  # Permutation test, MC equity, OOS test
│       ├── whitepaper_generator.py # HTML + Plotly report generator
│       ├── whitepaper_data_collector.py  # Orchestrates data collection
│       └── universe_analyzer.py    # Market universe statistics
├── tests/                          # 467 tests (unit + integration)
│   ├── test_archivo.py
│   ├── test_ejecucion.py
│   ├── test_estrategia.py
│   ├── test_ingesta.py
│   ├── test_integration.py
│   ├── test_circuit_breakers.py
│   ├── test_finbert.py
│   ├── test_gamma_client.py
│   ├── test_live_improvements.py
│   ├── test_market_filter.py
│   ├── test_monitor.py
│   ├── test_news_fetcher.py
│   ├── test_order_lifecycle.py
│   ├── test_position_manager.py
│   ├── test_data_resilience.py
│   ├── test_whitepaper_pipeline.py
│   └── conftest.py
├── .env.example                    # Environment variable template
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## Testing

The project includes **467 tests** (unit + integration). All pass with zero warnings on Python 3.11+.

```bash
# Run full suite
python -m pytest tests/ -v

# Run specific module
python -m pytest tests/test_circuit_breakers.py -v

# Run with coverage report
python -m pytest tests/ --cov=src
```

Test categories:
- **Strategy**: Wick‑Fishing pattern detection, FinBERT sentiment, Monte Carlo simulation, news fetching
- **Execution**: Order lifecycle, timeouts, retries, stale order detection
- **Risk**: Circuit breaker state machine, drawdown limits, exposure caps, cooldown logic
- **Live**: WebSocket resilience, market filtering, position management, monitoring, alerting
- **Data**: Gamma client, database CRUD, market discovery, liquidity scoring, market selection
- **Whitepaper**: Backtest engine, parameter sweep, robustness analysis, HTML generation

---

## Lessons Learned

During development several critical issues were identified and resolved:

1. **Market Selector type mismatch** — Received `TrackedMarket` objects but expected dicts. Fixed by adding `_market_to_dict()` normalization with `to_dict()` fallback.

2. **FinBERT model reloading** — The model was re‑loaded from disk for every market, causing OOM and extreme latency. Fixed with a singleton pattern (`get_instance()`) and cascading fallback (ONNX → PyTorch → neutral).

3. **Pre‑flight check fragility** — Failed silently when env vars were missing or when imports were absent. Redesigned with granular validation, descriptive error messages, and a `PreflightResult` dataclass.

4. **Async resource leaks** — `aiohttp` sessions and `aiosqlite` connections were left open, causing `RuntimeError: Event loop is closed`. Fixed with `AsyncResourceManager` that centralises session lifecycle and cleanup task registration.

5. **Decimal precision in SQLite** — Float columns silently rounded monetary values. All monetary columns now store `TEXT` (Decimal strings) and convert on read/write. Quantization uses each market's `tick_size`.

6. **WebSocket zombie connections** — The connection could appear alive while no messages arrived. Fixed with a heartbeat monitor that triggers forced reconnect after 60s of silence.

7. **Stale orders after restart** — Orphaned orders from a previous run could execute unexpectedly. Fixed with `clean_start()` that cancels all open orders on boot.

---

## Live Trading Checklist

Before deploying with real funds:

1. Run `python scripts/entry_point.py check` — fixes env/config issues first.
2. Run `python scripts/live_production_checklist.py` — validates API, wallet, allowance, WS, dry‑run order construction.
3. Start with `DRY_RUN=true` (default) and observe logs for at least 24h.
4. Review circuit breaker history in `bot_state.db`.
5. Set `DRY_RUN=false` only after all checks pass and you are comfortable with the risk profile.

**Start small.** Even in production, keep initial capital low (e.g. $100 USDC) and monitor actively.

---

## Contributing

Contributions are welcome. Please open an issue or pull request.

- Ensure all 467 tests still pass: `python -m pytest tests/ -v`
- Follow existing code conventions (Decimal for money, async/await, type hints)
- Add tests for new functionality

---

## License

MIT © 2026 — See `LICENSE` for details.
