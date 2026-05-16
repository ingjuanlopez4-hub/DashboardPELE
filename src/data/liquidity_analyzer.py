import logging
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Any

getcontext().prec = 28

logger = logging.getLogger("liquidity_analyzer")

VOLUME_NORM = Decimal("500000")
LIQUIDITY_NORM = Decimal("250000")
PRICE_LOWER = Decimal("0.30")
PRICE_UPPER = Decimal("0.70")
TIME_HORIZON_DAYS = Decimal("30")
RATIO_MULTIPLIER = Decimal("5")

WEIGHT_VOLUME = Decimal("0.25")
WEIGHT_LIQUIDITY = Decimal("0.35")
WEIGHT_PRICE = Decimal("0.15")
WEIGHT_TIME = Decimal("0.10")
WEIGHT_RATIO = Decimal("0.15")


def compute_liquidity_score(market: dict[str, Any]) -> Decimal:
    volume = Decimal(str(market["volume"]))
    liquidity = Decimal(str(market["liquidity"]))
    mid_price = Decimal(str(market["outcome_prices"][0]))

    vol_score = min(Decimal("1"), volume / VOLUME_NORM)
    liq_score = min(Decimal("1"), liquidity / LIQUIDITY_NORM)
    price_score = (
        Decimal("1")
        if PRICE_LOWER <= mid_price <= PRICE_UPPER
        else Decimal("0.3")
    )

    end_date_str = market.get("end_date", "")
    days_left = Decimal("0")
    if end_date_str:
        try:
            end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            delta = (end - datetime.now(timezone.utc)).days
            days_left = Decimal(str(max(delta, 0)))
        except (ValueError, TypeError):
            days_left = Decimal("0")
    time_score = min(Decimal("1"), days_left / TIME_HORIZON_DAYS)

    ratio = liquidity / volume if volume > Decimal("0") else Decimal("0")
    ratio_score = min(Decimal("1"), ratio * RATIO_MULTIPLIER)

    score = (
        WEIGHT_VOLUME * vol_score
        + WEIGHT_LIQUIDITY * liq_score
        + WEIGHT_PRICE * price_score
        + WEIGHT_TIME * time_score
        + WEIGHT_RATIO * ratio_score
    )

    return score.quantize(Decimal("0.0001"))


class LiquidityAnalyzer:
    def score_markets(
        self, markets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        for m in markets:
            try:
                market_id = m.get("id") or m.get("condition_id")
                if not isinstance(m, dict) or not market_id:
                    logger.warning("Skipping invalid market entry: %s", m)
                    continue

                parsed = m
                if "volume" not in parsed or not isinstance(parsed.get("volume"), Decimal):
                    from src.data.gamma_client import GammaClient
                    parsed = GammaClient.parse_market_basic(m)

                score = compute_liquidity_score(parsed)
                parsed["liquidity_score"] = score
                scored.append(parsed)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping market %s: %s", m.get("id", "unknown"), exc)
                continue

        scored.sort(key=lambda x: float(str(x["liquidity_score"])), reverse=True)
        logger.info(
            "Scored %d markets (top score: %s, bottom: %s)",
            len(scored),
            scored[0]["liquidity_score"] if scored else "0",
            scored[-1]["liquidity_score"] if scored else "0",
        )
        return scored
