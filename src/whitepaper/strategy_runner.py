"""
StrategyBacktestRunner — Executes the Wick-Fishing + FinBERT + Monte Carlo
strategy over selected markets, generating a complete backtest with
simulated trades, PnL, equity curve, and performance metrics.

Signal generation:
  1. Wick-Fishing: detects order book manipulation patterns (large bids/asks
     that suddenly disappear, creating price wicks).
  2. FinBERT: real sentiment analysis from news articles (fetched per market).
  3. Monte Carlo: simulates 10,000 price trajectories to compute EV.

All monetary and probability values use Decimal.
"""

import asyncio
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

import numpy as np

from src.data.database import OrderBookSnapshot, PolymarketDatabase, TradeRecord
from src.data.market_tracker import TrackedMarket
from src.strategy.finbert_sentiment import FinBERTSentimentAnalyzer
from src.strategy.news_fetcher import NewsFetcher

logger = logging.getLogger("strategy_runner")

# ── Strategy defaults ───────────────────────────────────────────────────

DEFAULT_PARAMS: dict[str, Any] = {
    "min_edge": Decimal("0.05"),
    "kelly_fraction": Decimal("0.25"),
    "max_position_size_pct": Decimal("3.0"),
    "w_wick": Decimal("0.40"),
    "w_sentiment": Decimal("0.30"),
    "w_montecarlo": Decimal("0.30"),
    "base_fee_pct": Decimal("0.2"),
    "initial_balance": Decimal("10000"),
}


@dataclass
class BacktestResults:
    market_id: str = ""
    market_question: str = ""
    initial_balance: Decimal = Decimal("10000")
    final_balance: Decimal = Decimal("10000")
    net_pnl: Decimal = Decimal("0")
    total_return_pct: Decimal = Decimal("0")
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_drawdown_pct: Decimal = Decimal("0")
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[Decimal] = field(default_factory=list)
    drawdown_curve: list[Decimal] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)
    timestamps: list[str] = field(default_factory=list)
    trade_pnls: list[Decimal] = field(default_factory=list)
    historical_points: int = 0
    data_source: str = "synthetic"
    params_used: dict[str, Any] = field(default_factory=dict)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "initial_balance": str(self.initial_balance),
            "final_balance": str(self.final_balance),
            "net_pnl": str(self.net_pnl),
            "total_return_pct": str(self.total_return_pct),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "historical_points": self.historical_points,
            "data_source": self.data_source,
        }


class StrategyBacktestRunner:
    """Runs the Wick-Fishing + FinBERT + Monte Carlo strategy backtest.

    Parameters
    ----------
    db : PolymarketDatabase
        Database instance for persisting trades.
    params : dict, optional
        Strategy parameter overrides.
    sentiment_analyzer : FinBERTSentimentAnalyzer, optional
        FinBERT sentiment analyzer. If omitted, simulated sentiment is used.
    news_fetcher : NewsFetcher, optional
        News fetcher for fetching articles per market. If omitted, no real news is fetched.
    sentiment_concurrency : int
        Maximum number of markets to process news+sentiment for in parallel.
    """

    def __init__(
        self,
        db: PolymarketDatabase,
        params: Optional[dict[str, Any]] = None,
        sentiment_analyzer: Optional[FinBERTSentimentAnalyzer] = None,
        news_fetcher: Optional[NewsFetcher] = None,
        sentiment_concurrency: int = 5,
    ) -> None:
        self._db = db
        self._params = {**DEFAULT_PARAMS, **(params or {})}
        self._rng = random.Random(42)
        self._sentiment_analyzer = sentiment_analyzer
        self._news_fetcher = news_fetcher
        self._sentiment_concurrency = sentiment_concurrency
        self._sentiment_cache: dict[str, Optional[dict[str, Any]]] = {}

    async def run_backtest(
        self,
        markets: list[TrackedMarket],
        start_date: str = "",
        end_date: str = "",
    ) -> BacktestResults:
        """Run backtest over the given markets.

        Replays persisted token prices chronologically. Markets without enough
        history retain a clearly identified synthetic fallback.

        Parameters
        ----------
        markets : list[TrackedMarket]
            Markets to backtest over.
        start_date : str
            ISO start date (optional, for filtering).
        end_date : str
            ISO end date (optional, for filtering).

        Returns
        -------
        BacktestResults
            Aggregated backtest results across all markets.
        """
        logger.info("Running backtest on %d markets with params: %s", len(markets), self._params)

        # ── Phase 1: Pre-fetch news and analyze sentiment in parallel ──
        await self._prefetch_sentiment(markets)

        # ── Phase 2: Sequential backtest over each market ──
        aggregated = BacktestResults(
            market_id="__aggregated__",
            market_question="Aggregated Backtest",
            initial_balance=self._params["initial_balance"],
            params_used=dict(self._params),
        )
        current_balance = self._params["initial_balance"]
        all_trades: list[TradeRecord] = []
        equity_curve: list[Decimal] = [current_balance]
        timestamps: list[str] = [datetime.now(timezone.utc).isoformat()]

        for tm in markets:
            result = await self._run_single_market(
                tm, current_balance, start_date=start_date, end_date=end_date
            )
            all_trades.extend(result.trades)
            aggregated.trade_pnls.extend(result.trade_pnls)
            aggregated.historical_points += result.historical_points
            if result.data_source == "historical":
                aggregated.data_source = "historical"

            for pnl, timestamp in zip(result.trade_pnls, result.timestamps[1:]):
                current_balance += pnl
                equity_curve.append(current_balance)
                timestamps.append(timestamp)

            if result.trades:
                aggregated.winning_trades += result.winning_trades
                aggregated.losing_trades += result.losing_trades

        aggregated.trades = all_trades
        aggregated.total_trades = len(all_trades)
        aggregated.final_balance = current_balance
        aggregated.net_pnl = current_balance - aggregated.initial_balance
        aggregated.total_return_pct = (aggregated.net_pnl / aggregated.initial_balance * Decimal("100")).quantize(Decimal("0.01"))
        aggregated.equity_curve = equity_curve
        aggregated.timestamps = timestamps

        if len(all_trades) > 0:
            wins = sum(1 for pnl in aggregated.trade_pnls if pnl > 0)
            losses = sum(1 for pnl in aggregated.trade_pnls if pnl <= 0)
            aggregated.win_rate = wins / len(all_trades) if len(all_trades) > 0 else 0.0

            gross_profit = sum(pnl for pnl in aggregated.trade_pnls if pnl > 0)
            gross_loss = -sum(pnl for pnl in aggregated.trade_pnls if pnl < 0)
            aggregated.profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf")

            returns = []
            eq = [float(x) for x in equity_curve]
            for i in range(1, len(eq)):
                if eq[i - 1] > 0:
                    returns.append((eq[i] - eq[i - 1]) / eq[i - 1])
            aggregated.daily_returns = returns

            if len(returns) > 1:
                mean_r = np.mean(returns)
                std_r = np.std(returns, ddof=1)
                aggregated.sharpe_ratio = float(mean_r / std_r * math.sqrt(252)) if std_r > 1e-10 else 0.0

                neg_returns = [r for r in returns if r < 0]
                if neg_returns:
                    downside_std = np.std(neg_returns, ddof=1)
                    aggregated.sortino_ratio = float(mean_r / downside_std * math.sqrt(252)) if downside_std > 1e-10 else 0.0

            peak = Decimal("0")
            dd_sum = Decimal("0")
            for eq_val in equity_curve:
                if eq_val > peak:
                    peak = eq_val
                dd = (peak - eq_val) / peak * Decimal("100") if peak > 0 else Decimal("0")
                dd_sum = max(dd_sum, dd)
            aggregated.max_drawdown_pct = dd_sum.quantize(Decimal("0.01"))

            if aggregated.max_drawdown_pct > 0:
                annualized_return = float(aggregated.total_return_pct) / 100.0
                max_dd = float(aggregated.max_drawdown_pct) / 100.0
                aggregated.calmar_ratio = annualized_return / max_dd if max_dd > 0 else 0.0

        await self._db.insert_trades_bulk(all_trades)
        logger.info("Backtest complete: PnL=%s, Sharpe=%.2f, WinRate=%.1f%%",
                     aggregated.net_pnl, aggregated.sharpe_ratio, aggregated.win_rate * 100)
        return aggregated

    async def _prefetch_sentiment(self, markets: list[TrackedMarket]) -> None:
        """Fetch news and compute sentiment for all markets in parallel.

        Uses asyncio.gather with a concurrency limiter to avoid saturating
        news APIs. Results are stored in self._sentiment_cache.
        """
        news_fetcher = self._news_fetcher
        analyzer = self._sentiment_analyzer
        if not news_fetcher or not analyzer:
            logger.info("Sentiment analyzer or news fetcher not configured — skipping real sentiment")
            return

        logger.info("Prefetching sentiment for %d markets (concurrency=%d)...",
                     len(markets), self._sentiment_concurrency)
        sem = asyncio.Semaphore(self._sentiment_concurrency)

        async def _process(tm: TrackedMarket) -> tuple[str, Optional[dict[str, Any]]]:
            async with sem:
                if not getattr(analyzer, "sentiment_available", True):
                    logger.debug("Sentiment analyzer unavailable for market %s — using neutral", tm.market.id)
                    return tm.market.id, None

                news_texts = await news_fetcher.fetch_for_market(
                    market_question=tm.market.question,
                    market_tags=tm.market.tags,
                    market_category=tm.market.category,
                )

                if not news_texts:
                    logger.debug("No news for market %s — using neutral sentiment", tm.market.id)
                    return tm.market.id, None

                try:
                    results = await analyzer.analyze_batch(news_texts)
                except Exception as e:
                    logger.error("Sentiment analysis failed for market %s: %s", tm.market.id, e)
                    return tm.market.id, None

                logger.info(
                    "Market %s: fetched %d articles, analyzed %d sentiment results",
                    tm.market.id, len(news_texts), len(results),
                )

                high_conf = [
                    r for r in results
                    if r.confidence >= analyzer.confidence_threshold
                ]

                if not high_conf:
                    logger.info(
                        "Market %s: no high-confidence sentiment results — using neutral",
                        tm.market.id,
                    )
                    return tm.market.id, None

                total_weight = sum(r.confidence for r in high_conf)
                implied_prob = sum(
                    r.implied_probability * r.confidence for r in high_conf
                ) / total_weight
                avg_confidence = total_weight / Decimal(str(len(high_conf)))

                logger.info(
                    "Market %s: %d high-confidence articles, aggregate implied_prob=%s, confidence=%s",
                    tm.market.id, len(high_conf), implied_prob, avg_confidence,
                )

                data: dict[str, Any] = {
                    "implied_probability": implied_prob,
                    "confidence": avg_confidence,
                    "num_articles": len(high_conf),
                }
                return tm.market.id, data

        tasks = [_process(tm) for tm in markets]
        gathered: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)

        for i, tm in enumerate(markets):
            mid = tm.market.id
            if isinstance(gathered[i], BaseException):
                logger.error("Sentiment processing failed for market %s: %s", mid, gathered[i])
                self._sentiment_cache[mid] = None
            else:
                _, data = gathered[i]
                self._sentiment_cache[mid] = data

        cached_count = sum(1 for v in self._sentiment_cache.values() if v is not None)
        logger.info("Sentiment prefetch complete: %d/%d markets have real sentiment data",
                     cached_count, len(markets))

    async def _run_single_market(
        self,
        tm: TrackedMarket,
        balance: Decimal,
        start_date: str = "",
        end_date: str = "",
    ) -> BacktestResults:
        """Replay one market without using future prices to create signals."""
        result = BacktestResults(
            market_id=tm.market.id,
            market_question=tm.market.question,
            initial_balance=balance,
            params_used=dict(self._params),
        )

        snap = next(iter(tm.snapshots.values())) if tm.snapshots else None
        if snap is None or snap.mid_price <= 0:
            snap = self._make_fallback_snapshot(tm)
            if snap is None:
                logger.debug("Skipping market %s — no price data available", tm.market.question[:40])
                return result
            logger.info("Using fallback snapshot for market %s (mid=%s)", tm.market.question[:40], snap.mid_price)

        token_id = self._history_token_id(tm, snap)
        history = await self._db.get_token_price_history(
            token_id,
            limit=10_000,
            start_date=start_date,
            end_date=end_date,
        ) if token_id else []

        if len(history) >= 2:
            price_points = [
                (
                    Decimal(str(row["mid_price"])),
                    str(row["snapshot_time"]),
                    row,
                )
                for row in history
            ]
            result.data_source = "historical"
            result.historical_points = len(price_points)
        else:
            n_steps = self._rng.randint(20, 100)
            price = snap.mid_price
            price_points = [
                (price, datetime.now(timezone.utc).isoformat(), None)
            ]
            for _ in range(n_steps):
                price = self._simulate_price_step(
                    price, tm.market.tick_size, snap.spread_pct, tm
                )
                price_points.append(
                    (price, datetime.now(timezone.utc).isoformat(), None)
                )

        mid = price_points[0][0]
        tick = tm.market.tick_size
        balance_step = balance
        equity_curve = [balance_step]
        timestamps = [price_points[0][1]]
        rolling_history: list[Decimal] = [mid]

        for (price, timestamp, row), (next_price, _, _) in zip(
            price_points[:-1], price_points[1:]
        ):
            signal = self._generate_signal(
                price,
                mid,
                tm,
                price_history=list(rolling_history),
                snapshot=self._snapshot_from_history_row(row) if row else None,
                include_sentiment=result.data_source != "historical",
            )

            if signal is not None:
                threshold = self._params["min_edge"]
                ev = signal.get("ev", Decimal("0"))
                if abs(ev) >= threshold:
                    trade = self._execute_trade(tm, signal, balance_step, price, tick)
                    if trade is not None:
                        trade.timestamp = timestamp
                        result.trades.append(trade)
                        pnl = self._trade_pnl(trade, price, next_price)
                        result.trade_pnls.append(pnl)
                        balance_step += pnl

                        if pnl > 0:
                            result.winning_trades += 1
                        else:
                            result.losing_trades += 1

                        equity_curve.append(balance_step)
                        timestamps.append(trade.timestamp)
            rolling_history.append(price)

        result.total_trades = len(result.trades)
        result.final_balance = balance_step
        result.net_pnl = balance_step - result.initial_balance
        result.total_return_pct = (result.net_pnl / result.initial_balance * Decimal("100")).quantize(Decimal("0.01"))
        result.equity_curve = equity_curve
        result.timestamps = timestamps

        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades
            gross_profit = sum(pnl for pnl in result.trade_pnls if pnl > 0)
            gross_loss = -sum(pnl for pnl in result.trade_pnls if pnl < 0)
            result.profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        return result

    @staticmethod
    def _history_token_id(tm: TrackedMarket, snap: OrderBookSnapshot) -> str:
        for token in tm.tokens:
            if token.outcome.upper() == "YES" and token.token_id:
                return token.token_id
        return snap.token_id or (tm.tokens[0].token_id if tm.tokens else "")

    @staticmethod
    def _snapshot_from_history_row(row: dict[str, Any]) -> OrderBookSnapshot:
        def decimal_value(key: str) -> Decimal:
            return Decimal(str(row.get(key) or "0"))

        return OrderBookSnapshot(
            market_id=str(row["market_id"]),
            token_id=str(row["token_id"]),
            best_bid=decimal_value("best_bid"),
            best_ask=decimal_value("best_ask"),
            mid_price=decimal_value("mid_price"),
            spread_pct=decimal_value("spread_pct"),
            depth_2pct=decimal_value("depth_2pct"),
            bid_depth_5=decimal_value("bid_depth_5"),
            ask_depth_5=decimal_value("ask_depth_5"),
        )

    def _trade_pnl(
        self, trade: TradeRecord, price: Decimal, next_price: Decimal
    ) -> Decimal:
        entry = Decimal("1") - price if trade.side == "BUY_NO" else price
        exit_price = (
            Decimal("1") - next_price
            if trade.side == "BUY_NO"
            else next_price
        )
        if entry <= 0:
            return Decimal("0")
        gross = trade.usdc_amount * ((exit_price / entry) - Decimal("1"))
        fee = trade.usdc_amount * self._params["base_fee_pct"] / Decimal("100")
        return (gross - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def _make_fallback_snapshot(self, tm: TrackedMarket) -> Optional[OrderBookSnapshot]:
        yes_price = None
        for token in tm.tokens:
            if token.outcome.upper() == "YES" and token.price > 0:
                yes_price = token.price
                break
        if yes_price is None:
            for token in tm.tokens:
                if token.price > 0:
                    yes_price = token.price
                    break
        if yes_price is None:
            return None

        spread = Decimal("0.02")
        half_spread = spread / Decimal("2")
        return OrderBookSnapshot(
            market_id=tm.market.id,
            token_id=tm.tokens[0].token_id if tm.tokens else "",
            best_bid=round(yes_price * (Decimal("1") - half_spread), 6),
            best_ask=round(yes_price * (Decimal("1") + half_spread), 6),
            mid_price=yes_price,
            spread_pct=spread,
            depth_2pct=yes_price * Decimal("1000"),
            bid_depth_5=yes_price * Decimal("500"),
            ask_depth_5=yes_price * Decimal("500"),
        )

    def _simulate_price_step(
        self, price: Decimal, tick: Decimal, spread: Decimal, tm: TrackedMarket
    ) -> Decimal:
        """Simulate a single price movement step with mean-reverting
        random walk and occasional wick-fishing manipulation."""
        mu = 0.0
        sigma = 0.02

        snap = next(iter(tm.snapshots.values())) if tm.snapshots else None
        if snap:
            wick_prob = 0.05
            if self._rng.random() < wick_prob:
                sigma *= 3
                if self._rng.random() < 0.5:
                    sigma *= -1

        ret = self._rng.gauss(mu, sigma)
        new_price = price * (Decimal("1") + Decimal(str(round(ret, 6))))
        new_price = new_price.quantize(tick, rounding=ROUND_HALF_EVEN)
        return max(tick, min(Decimal("1"), new_price))

    def _generate_signal(
        self,
        current_price: Decimal,
        mid_price: Decimal,
        tm: TrackedMarket,
        price_history: Optional[list[Decimal]] = None,
        snapshot: Optional[OrderBookSnapshot] = None,
        include_sentiment: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Generate a trading signal using the three strategy components.

        The sentiment weight is dynamically adjusted by the confidence of the
        sentiment signal. When sentiment confidence is low (no news, neutral),
        its effective weight drops so it does not bias the decision.
        Returns None if no signal, or a dict with signal details.
        """
        snap = snapshot or (next(iter(tm.snapshots.values())) if tm.snapshots else None)

        wick_signal = self._wick_fishing_signal(snap, current_price)
        sentiment_signal = self._sentiment_signal(tm) if include_sentiment else {
            "ev": Decimal("0"),
            "probability": Decimal("0.5"),
            "signal": "neutral",
            "confidence": Decimal("0"),
        }
        mc_signal = self._monte_carlo_signal(
            current_price, mid_price, tm, price_history=price_history
        )

        w_wick = self._params["w_wick"]
        w_sent = self._params["w_sentiment"]
        w_mc = self._params["w_montecarlo"]

        # Adjust sentiment weight by its confidence — low confidence → low influence
        sent_confidence = sentiment_signal.get("confidence", Decimal("0.5"))
        w_sent_effective = w_sent * sent_confidence
        w_wick_effective = w_wick
        w_mc_effective = w_mc

        combined_ev = (
            w_wick_effective * Decimal(str(wick_signal.get("ev", 0)))
            + w_sent_effective * Decimal(str(sentiment_signal.get("ev", 0)))
            + w_mc_effective * Decimal(str(mc_signal.get("ev", 0)))
        )

        total_w = w_wick_effective + w_sent_effective + w_mc_effective
        if total_w > 0:
            combined_ev /= total_w

        combined_prob = (
            w_wick_effective * Decimal(str(wick_signal.get("probability", 0.5)))
            + w_sent_effective * Decimal(str(sentiment_signal.get("probability", 0.5)))
            + w_mc_effective * Decimal(str(mc_signal.get("probability", 0.5)))
        ) / total_w if total_w > 0 else Decimal("0.5")

        if abs(combined_ev) < self._params["min_edge"]:
            return None

        side = "BUY_YES" if combined_ev > 0 else "BUY_NO"

        return {
            "side": side,
            "ev": combined_ev,
            "probability": combined_prob,
            "win_rate": abs(float(combined_ev)) * 100,
            "signal_source": "combined",
            "wick_details": wick_signal,
            "sentiment_details": sentiment_signal,
            "mc_details": mc_signal,
        }

    def _wick_fishing_signal(
        self, snap: Any, current_price: Decimal
    ) -> dict[str, Any]:
        """Detect wick-fishing patterns in the order book.

        A wick-fishing pattern is characterized by a large bid or ask that
        suddenly disappears, creating a sharp price movement. We detect
        this by looking for large imbalance between bid and ask depth.
        """
        if snap is None:
            return {"ev": 0.0, "probability": 0.5, "signal": "none"}

        bid_depth = float(snap.bid_depth_5)
        ask_depth = float(snap.ask_depth_5)
        total_depth = bid_depth + ask_depth

        if total_depth < 0.01:
            return {"ev": 0.0, "probability": 0.5, "signal": "none"}

        imbalance = (bid_depth - ask_depth) / total_depth
        threshold = 0.3

        if abs(imbalance) > threshold:
            ev = imbalance * 0.5
            prob = 0.5 + abs(imbalance) * 0.15
            direction = "bullish" if imbalance > 0 else "bearish"
            return {
                "ev": round(ev, 4),
                "probability": round(min(prob, 0.95), 4),
                "signal": f"wick_{direction}",
                "imbalance": round(imbalance, 4),
            }

        return {"ev": 0.0, "probability": 0.5, "signal": "none"}

    def _sentiment_signal(self, tm: TrackedMarket) -> dict[str, Any]:
        """Compute sentiment signal from pre-fetched FinBERT analysis.

        Uses the cached sentiment data obtained during the prefetch phase.
        If no real data is available (no news, low confidence, or analyzer
        not configured), returns a neutral signal with low confidence so
        that the sentiment component does not bias the combined decision.
        """
        data = self._sentiment_cache.get(tm.market.id)

        if data is not None:
            implied_prob = data["implied_probability"]
            confidence = data["confidence"]
            ev = implied_prob - Decimal("0.5")
            direction = "positive" if ev > 0 else "negative" if ev < 0 else "neutral"

            logger.debug(
                "Sentiment signal for %s: prob=%s confidence=%s articles=%d",
                tm.market.id, implied_prob, confidence, data["num_articles"],
            )

            return {
                "ev": ev,
                "probability": implied_prob,
                "signal": direction,
                "sentiment_score": implied_prob,
                "confidence": confidence,
                "num_articles": data["num_articles"],
            }

        # Neutral signal with low confidence — won't bias the combined signal
        return {
            "ev": Decimal("0"),
            "probability": Decimal("0.5"),
            "signal": "neutral",
            "sentiment_score": Decimal("0.5"),
            "confidence": Decimal("0.1"),
            "num_articles": 0,
        }

    def _monte_carlo_signal(
        self,
        current_price: Decimal,
        initial_mid_price: Decimal,
        tm: TrackedMarket,
        price_history: Optional[list[Decimal]] = None,
    ) -> dict[str, Any]:
        """Estimate EV by comparing current price to initial fair value.

        When the current simulated price deviates from the initial mid price
        (fair value), the difference represents a mean-reversion edge.
        Monte Carlo simulations estimate the probability of reversion.
        """
        n_simulations = 10000
        if price_history and len(price_history) >= 10:
            prices = np.asarray([float(p) for p in price_history if p > 0])
            returns = np.diff(np.log(prices))
            sigma = float(np.std(returns)) if len(returns) >= 5 else 0.05
            sigma = min(max(sigma, 0.001), 0.50)
        else:
            sigma = 0.05
        n_steps = 10
        dt = 1.0 / n_steps

        mid = float(current_price)
        fair = float(initial_mid_price)

        rng = np.random.default_rng(self._rng.randrange(2**32))
        shocks = rng.standard_normal((n_simulations, n_steps))
        log_returns = (-0.5 * sigma * sigma * dt) + sigma * math.sqrt(dt) * shocks
        final_prices = mid * np.exp(np.sum(log_returns, axis=1))
        final_prices = np.clip(final_prices, 0.001, 0.999)

        prob_up = float(np.mean(final_prices > mid))
        expected_price = float(np.mean(final_prices))
        mc_ev = (fair - expected_price) / fair if fair > 0 else 0

        mc_probability = Decimal(str(round(prob_up, 4)))
        mc_ev_dec = Decimal(str(round(mc_ev, 4)))

        return {
            "ev": mc_ev_dec,
            "probability": mc_probability,
            "signal": "up" if mc_ev_dec > 0 else "down",
            "n_simulations": n_simulations,
            "expected_price": round(expected_price, 4),
            "history_points": len(price_history or []),
            "sigma": round(sigma, 6),
        }

    def _execute_trade(
        self,
        tm: TrackedMarket,
        signal: dict[str, Any],
        balance: Decimal,
        price: Decimal,
        tick: Decimal,
    ) -> Optional[TradeRecord]:
        """Execute a single simulated trade, applying Kelly sizing and fees."""
        ev = abs(signal.get("ev", Decimal("0")))
        prob = signal.get("probability", Decimal("0.5"))
        side = signal["side"]

        kelly_fraction = self._params["kelly_fraction"]
        max_pos_pct = self._params["max_position_size_pct"]
        base_fee = self._params["base_fee_pct"]

        p = float(prob)
        b = float(ev) / max(float(ev), 0.001)
        kelly_pct = max(0, (p * b - (1 - p)) / b) if b > 0 else 0
        position_pct = min(float(max_pos_pct), kelly_pct * float(kelly_fraction) * 100)
        position_size = balance * Decimal(str(round(position_pct / 100, 6)))
        position_size = position_size.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

        if position_size < Decimal("1"):
            return None

        fee = position_size * base_fee / Decimal("100")
        net_size = position_size - fee
        executed_price = price.quantize(tick, rounding=ROUND_HALF_EVEN)

        snap = next(iter(tm.snapshots.values())) if tm.snapshots else None
        token_id = ""
        for token in tm.tokens:
            if side == "BUY_YES" and token.outcome.upper() == "YES":
                token_id = token.token_id
                break
            if side == "BUY_NO" and token.outcome.upper() == "NO":
                token_id = token.token_id
                break

        trade = TradeRecord(
            market_id=tm.market.id,
            token_id=token_id or "unknown",
            side=side,
            price=executed_price,
            size=net_size,
            usdc_amount=position_size,
            fee_pct=base_fee,
            signal_source=signal.get("signal_source", "combined"),
            probability=prob,
            ev=ev,
            win_rate=Decimal(str(signal.get("win_rate", 50))),
        )
        return trade

    async def run_backtest_aggregated(
        self,
        results: list[BacktestResults],
    ) -> BacktestResults:
        """Aggregate multiple per-market backtest results into one."""
        if not results:
            return BacktestResults()

        agg = BacktestResults(
            initial_balance=results[0].initial_balance,
            params_used=dict(self._params),
        )

        all_trades = []
        equity = [agg.initial_balance]
        timestamps = [results[0].timestamps[0] if results[0].timestamps else datetime.now(timezone.utc).isoformat()]

        for r in results:
            all_trades.extend(r.trades)
            agg.winning_trades += r.winning_trades
            agg.losing_trades += r.losing_trades
            for i in range(len(r.equity_curve)):
                if i < len(r.equity_curve) - 1:
                    if len(equity) <= i + 1:
                        equity.append(equity[-1])
                    equity[i + 1] += r.equity_curve[i + 1]

        agg.trades = all_trades
        agg.total_trades = len(all_trades)
        agg.final_balance = sum(r.final_balance for r in results)
        agg.net_pnl = agg.final_balance - agg.initial_balance

        if agg.total_trades > 0:
            agg.win_rate = agg.winning_trades / agg.total_trades

        return agg
