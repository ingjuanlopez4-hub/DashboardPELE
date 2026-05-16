import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger("universe_analyzer")


class UniverseAnalyzer:
    def analyze(self, markets: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(markets)
        if total == 0:
            return {"total_markets": 0, "error": "no markets"}

        volumes = [Decimal(str(m.get("volume", "0"))) for m in markets]
        liquidities = [Decimal(str(m.get("liquidity", "0"))) for m in markets]
        scores = [float(m.get("liquidity_score", 0)) for m in markets]
        categories = [str(m.get("category", "unknown")) for m in markets]
        prices_yes = [
            Decimal(str(m["outcome_prices"][0]))
            for m in markets
            if m.get("outcome_prices")
        ]

        end_dates = [
            m.get("end_date", "")
            for m in markets
            if m.get("end_date")
        ]

        days_left_list = []
        for ed in end_dates:
            try:
                end = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                days_left_list.append((end - datetime.now(timezone.utc)).days)
            except (ValueError, TypeError):
                pass

        cat_counts = Counter(categories)
        selected_count = len(
            [m for m in markets if m.get("liquidity_score", 0) >= 0.4]
        )

        top_by_score = sorted(
            markets, key=lambda x: x.get("liquidity_score", 0), reverse=True
        )[:10]
        top_by_volume = sorted(
            markets, key=lambda x: Decimal(str(x.get("volume", "0"))), reverse=True
        )[:20]

        category_stats = self._category_stats(markets)

        return {
            "total_markets": total,
            "total_volume": sum(volumes),
            "total_liquidity": sum(liquidities),
            "avg_volume": sum(volumes) / total if total else Decimal("0"),
            "avg_liquidity": sum(liquidities) / total if total else Decimal("0"),
            "median_score": sorted(scores)[total // 2] if scores else 0,
            "avg_score": sum(scores) / len(scores) if scores else 0,
            "selected_count": selected_count,
            "category_counts": dict(cat_counts),
            "category_stats": category_stats,
            "top_10_by_score": [
                {
                    "id": m.get("id", ""),
                    "question": m.get("question", "")[:80],
                    "score": m.get("liquidity_score", 0),
                    "volume": str(Decimal(str(m.get("volume", "0")))),
                    "liquidity": str(Decimal(str(m.get("liquidity", "0")))),
                    "category": m.get("category", ""),
                    "yes_price": str(m["outcome_prices"][0])
                    if m.get("outcome_prices")
                    else "",
                }
                for m in top_by_score
            ],
            "top_20_by_volume": [
                {
                    "id": m.get("id", ""),
                    "question": m.get("question", "")[:60],
                    "volume": str(Decimal(str(m.get("volume", "0")))),
                    "category": m.get("category", ""),
                    "score": m.get("liquidity_score", 0),
                }
                for m in top_by_volume
            ],
            "avg_yes_price": (
                sum(prices_yes) / len(prices_yes) if prices_yes else Decimal("0")
            ),
            "avg_days_left": (
                sum(days_left_list) / len(days_left_list) if days_left_list else 0
            ),
            "score_distribution": self._histogram(scores, bins=10),
            "volume_distribution": self._histogram(
                [float(v) for v in volumes], bins=10
            ),
        }

    def _category_stats(
        self, markets: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in markets:
            cat = str(m.get("category", "unknown"))
            by_cat[cat].append(m)

        stats: dict[str, dict[str, Any]] = {}
        for cat, members in by_cat.items():
            vols = [Decimal(str(m.get("volume", "0"))) for m in members]
            liqs = [Decimal(str(m.get("liquidity", "0"))) for m in members]
            scores = [float(m.get("liquidity_score", 0)) for m in members]
            stats[cat] = {
                "count": len(members),
                "avg_volume": str(sum(vols) / len(members)),
                "avg_liquidity": str(sum(liqs) / len(members)),
                "avg_score": sum(scores) / len(scores) if scores else 0,
                "total_volume": str(sum(vols)),
            }
        return stats

    @staticmethod
    def _histogram(values: list[float], bins: int = 10) -> list[dict[str, Any]]:
        if not values:
            return []
        mn, mx = min(values), max(values)
        if mx == mn:
            return [{"bin_start": mn, "bin_end": mx, "count": len(values)}]
        width = (mx - mn) / bins
        result = []
        for i in range(bins):
            lo = mn + i * width
            hi = lo + width
            cnt = sum(1 for v in values if lo <= v < hi or (i == bins - 1 and v == mx))
            result.append({"bin_start": round(lo, 4), "bin_end": round(hi, 4), "count": cnt})
        return result
