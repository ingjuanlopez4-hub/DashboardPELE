import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger("market_selector")

DEFAULT_MIN_SCORE = Decimal("0.1")
DEFAULT_MIN_VOLUME = Decimal("1000")
DEFAULT_MIN_LIQUIDITY = Decimal("500")
DEFAULT_MIN_DAYS = 0
DEFAULT_PRICE_LOWER = Decimal("0.10")
DEFAULT_PRICE_UPPER = Decimal("0.90")


@dataclass
class SelectionResult:
    selected: List[Dict[str, Any]]
    rejected: List[Dict[str, Any]]
    total_evaluated: int
    passed: int
    failed: int
    selection_explanations: Dict[str, str] = field(default_factory=dict)


def _market_to_dict(market: Any) -> Dict[str, Any]:
    if isinstance(market, dict):
        return market
    if hasattr(market, "liquidity_score") and hasattr(market, "market"):
        m = market.market
        outcome_prices = [str(t.price) for t in getattr(m, "tokens", []) if t.price > 0]
        try:
            return {
                "liquidity_score": Decimal(str(market.liquidity_score)),
                "volume": getattr(m, "volume_num", Decimal("0")),
                "liquidity": getattr(m, "liquidity_num", Decimal("0")),
                "outcome_prices": outcome_prices,
                "end_date": getattr(m, "end_date", ""),
                "enable_order_book": True,
                "_original": market,
                "question": getattr(m, "question", ""),
                "id": getattr(m, "id", str(id(market))),
                "category": getattr(m, "category", ""),
            }
        except Exception as e:
            logger.warning("Failed to normalize market %s: %s", getattr(m, "id", "?"), e)
            return {"liquidity_score": Decimal("0"), "_original": market}
    try:
        return dict(market)
    except (TypeError, ValueError):
        return {"liquidity_score": Decimal("0"), "_original": market}


class MarketSelector:
    def __init__(
        self,
        min_score: Decimal = DEFAULT_MIN_SCORE,
        min_volume: Decimal = DEFAULT_MIN_VOLUME,
        min_liquidity: Decimal = DEFAULT_MIN_LIQUIDITY,
        min_days_to_resolution: int = DEFAULT_MIN_DAYS,
        price_range: tuple[Decimal, Decimal] = (
            DEFAULT_PRICE_LOWER,
            DEFAULT_PRICE_UPPER,
        ),
    ) -> None:
        self._min_score = min_score
        self._min_volume = min_volume
        self._min_liquidity = min_liquidity
        self._min_days = min_days_to_resolution
        self._price_lower, self._price_upper = price_range

    def _passes_filters(self, market: dict[str, Any]) -> bool:
        score = market.get("liquidity_score", Decimal("0"))
        if not isinstance(score, Decimal):
            try:
                score = Decimal(str(score))
            except Exception:
                score = Decimal("0")
        if score < self._min_score:
            if len(logger.handlers) > 0:
                logger.log(logging.DEBUG, "Market %s score=%s < min=%s — rejected",
                           market.get("id", "?")[:8], score, self._min_score)
            return False

        volume = Decimal(str(market.get("volume", "0")))
        if volume < self._min_volume:
            return False

        liquidity = Decimal(str(market.get("liquidity", "0")))
        if liquidity < self._min_liquidity:
            return False

        outcome_prices = market.get("outcome_prices", [])
        if outcome_prices:
            raw = outcome_prices[0]
            try:
                yes_price = Decimal(str(raw)) if not isinstance(raw, Decimal) else raw
            except Exception:
                yes_price = Decimal("0.5")
            if not (self._price_lower <= yes_price <= self._price_upper):
                logger.debug("Market %s yes_price=%s outside [%s, %s] — rejected",
                             market.get("id", "?")[:8], yes_price,
                             self._price_lower, self._price_upper)
                return False

        end_date_str = market.get("end_date", "")
        if end_date_str:
            try:
                end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_left = (end - datetime.now(timezone.utc)).days
                if days_left < self._min_days:
                    return False
            except (ValueError, TypeError):
                logger.debug("Skipping end_date filter for market %s — invalid date: %s",
                             market.get("id", "?"), end_date_str)

        if not market.get("enable_order_book", True):
            return False

        return True

    def select_top_markets(
        self,
        scored_markets: list[Any],
        top_n: int = 50,
        min_score: Decimal | None = None,
        min_volume: Decimal | None = None,
        min_liquidity: Decimal | None = None,
        min_days_to_resolution: int | None = None,
        price_range: tuple[Decimal, Decimal] | None = None,
    ) -> SelectionResult:
        if min_score is not None:
            self._min_score = min_score
        if min_volume is not None:
            self._min_volume = min_volume
        if min_liquidity is not None:
            self._min_liquidity = min_liquidity
        if min_days_to_resolution is not None:
            self._min_days = min_days_to_resolution
        if price_range is not None:
            self._price_lower, self._price_upper = price_range

        normalized = [_market_to_dict(m) for m in scored_markets]
        results_list = [(m, self._passes_filters(m)) for m in normalized]
        passed = [m for m, ok in results_list if ok]
        failed = [m for m, ok in results_list if not ok]
        passed.sort(key=lambda x: float(str(x.get("liquidity_score", "0"))), reverse=True)
        top = passed[:top_n]
        not_top = passed[top_n:]

        def _restore(m: dict[str, Any]) -> Any:
            return m.get("_original", m)

        logger.info(
            "Selected %d / %d markets (filtered from %d scored)",
            len(top),
            top_n,
            len(scored_markets),
        )

        return SelectionResult(
            selected=[_restore(m) for m in top],
            rejected=[_restore(m) for m in failed + not_top],
            total_evaluated=len(scored_markets),
            passed=len(passed),
            failed=len(failed + not_top),
        )

    def select_with_result(
        self,
        scored_markets: list[Any],
        top_n: int = 50,
        min_score: Decimal | None = None,
        min_volume: Decimal | None = None,
        min_liquidity: Decimal | None = None,
        min_days_to_resolution: int | None = None,
        price_range: tuple[Decimal, Decimal] | None = None,
    ) -> SelectionResult:
        result = self.select_top_markets(
            scored_markets,
            top_n=top_n,
            min_score=min_score,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
            min_days_to_resolution=min_days_to_resolution,
            price_range=price_range,
        )
        for m in result.selected:
            d = _market_to_dict(m)
            mid = d.get("id", str(id(m)))
            result.selection_explanations[mid] = self.explain_selection(m)
        return result

    def explain_selection(self, market: Any) -> str:
        d = _market_to_dict(market)
        parts = [
            f"Market: {d.get('question', 'Unknown')}",
            f"Score: {d.get('liquidity_score', Decimal('0'))}",
            f"Volume: ${d.get('volume', Decimal('0'))}",
            f"Liquidity: ${d.get('liquidity', Decimal('0'))}",
        ]
        return " | ".join(parts)

    def select_by_category(
        self,
        scored_markets: list[Any],
        categories: list[str],
        top_n: int = 50,
    ) -> list[Any]:
        normalized = [_market_to_dict(m) for m in scored_markets]
        filtered = [m for m in normalized if m.get("category", "") in categories]
        filtered.sort(key=lambda x: float(str(x.get("liquidity_score", "0"))), reverse=True)
        top = filtered[:top_n]
        return [m.get("_original", m) for m in top]
