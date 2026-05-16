import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.gamma_client import GammaClient, GammaClientError, RateLimitError
from src.data.liquidity_analyzer import LiquidityAnalyzer, compute_liquidity_score
from src.data.market_selector import MarketSelector
from src.whitepaper.universe_analyzer import UniverseAnalyzer
from src.whitepaper.whitepaper_generator import MarketUniverseReportGenerator


SAMPLE_MARKET = {
    "id": "0xabc123",
    "condition_id": "0xabc123",
    "question": "Will Bitcoin close above $100k on May 31?",
    "slug": "will-bitcoin-close-above-100k-may-31",
    "category": "crypto",
    "tags": ["bitcoin", "price"],
    "volume": Decimal("1500000.50"),
    "liquidity": Decimal("250000.75"),
    "active": True,
    "closed": False,
    "archived": False,
    "enable_order_book": True,
    "tick_size": Decimal("0.01"),
    "neg_risk": False,
    "end_date": "2026-05-31T23:59:59Z",
    "outcomes": ["Yes", "No"],
    "outcome_prices": [Decimal("0.48"), Decimal("0.52")],
    "clob_token_ids": ["0xTokenYes", "0xTokenNo"],
    "events": [],
    "liquidity_score": 0.65,
}

SAMPLE_RAW_API_RESPONSE = {
    "id": "0xabc123",
    "conditionId": "0xabc123",
    "question": "Will Bitcoin close above $100k on May 31?",
    "slug": "will-bitcoin-close-above-100k-may-31",
    "category": "crypto",
    "tags": ["bitcoin", "price"],
    "volume": 1500000.50,
    "liquidity": 250000.75,
    "active": True,
    "closed": False,
    "archived": False,
    "enableOrderBook": True,
    "tickSize": "0.01",
    "negRisk": False,
    "endDate": "2027-06-30T23:59:59Z",
    "outcomes": ["Yes", "No"],
    "outcomePrices": [0.48, 0.52],
    "clobTokenIds": ["0xTokenYes", "0xTokenNo"],
    "events": [],
}


# ── GammaClient Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gamma_client_parse_market():
    parsed = GammaClient.parse_market_basic(SAMPLE_RAW_API_RESPONSE)
    assert parsed["condition_id"] == "0xabc123"
    assert parsed["question"] == "Will Bitcoin close above $100k on May 31?"
    assert parsed["volume"] == Decimal("1500000.50")
    assert parsed["liquidity"] == Decimal("250000.75")
    assert parsed["enable_order_book"] is True
    assert parsed["outcome_prices"] == [Decimal("0.48"), Decimal("0.52")]
    assert parsed["category"] == "crypto"
    assert parsed["tick_size"] == Decimal("0.01")


@pytest.mark.asyncio
async def test_gamma_client_discover_pagination():
    mock_session = AsyncMock(spec_set=["get"])
    page1 = MagicMock()
    page1.status = 200
    page1.__aenter__.return_value = page1
    page1.json = AsyncMock(return_value=[SAMPLE_RAW_API_RESPONSE] * 100)

    page2 = MagicMock()
    page2.status = 200
    page2.__aenter__.return_value = page2
    page2.json = AsyncMock(return_value=[SAMPLE_RAW_API_RESPONSE] * 50)

    page3 = MagicMock()
    page3.status = 200
    page3.__aenter__.return_value = page3
    page3.json = AsyncMock(return_value=[])

    mock_session.get.side_effect = [page1, page2, page3]

    client = GammaClient(mock_session)
    markets = await client.discover_all_active_markets()

    assert len(markets) == 150
    assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_gamma_client_rate_limit_retry():
    mock_session = AsyncMock(spec_set=["get"])

    rate_limited = MagicMock()
    rate_limited.status = 429
    rate_limited.__aenter__.return_value = rate_limited

    ok_response = MagicMock()
    ok_response.status = 200
    ok_response.__aenter__.return_value = ok_response
    ok_response.json = AsyncMock(return_value=[])

    mock_session.get.side_effect = [rate_limited, ok_response]

    client = GammaClient(mock_session, max_concurrent=10)
    with patch("asyncio.sleep", AsyncMock()):
        markets = await client.discover_all_active_markets()

    assert markets == []


@pytest.mark.asyncio
async def test_gamma_client_http_error():
    mock_session = AsyncMock(spec_set=["get"])
    error_resp = MagicMock()
    error_resp.status = 500
    error_resp.__aenter__.return_value = error_resp
    error_resp.text = AsyncMock(return_value="Internal Server Error")

    mock_session.get.return_value = error_resp

    client = GammaClient(mock_session, max_concurrent=10)
    markets = await client.discover_all_active_markets()

    assert markets == []


# ── LiquidityAnalyzer Tests ────────────────────────────────────────


class TestComputeLiquidityScore:
    def test_high_quality_market(self):
        market = dict(SAMPLE_MARKET)
        score = compute_liquidity_score(market)
        assert 0.4 <= score <= 1.0

    def test_low_volume_market(self):
        market = dict(SAMPLE_MARKET)
        market["volume"] = Decimal("1000")
        market["liquidity"] = Decimal("500")
        score = compute_liquidity_score(market)
        assert score <= 0.5

    def test_price_out_of_range(self):
        market = dict(SAMPLE_MARKET)
        market["outcome_prices"] = [Decimal("0.85"), Decimal("0.15")]
        score = compute_liquidity_score(market)
        score_extreme = compute_liquidity_score(market)
        market2 = dict(SAMPLE_MARKET)
        score_normal = compute_liquidity_score(market2)
        assert score_extreme < score_normal

    def test_zero_volume_does_not_divide_by_zero(self):
        market = dict(SAMPLE_MARKET)
        market["volume"] = Decimal("0")
        score = compute_liquidity_score(market)
        assert isinstance(score, Decimal)

    def test_all_scores_between_zero_and_one(self):
        variations = [
            {"volume": Decimal("1000000"), "liquidity": Decimal("500000"), "outcome_prices": [Decimal("0.50"), Decimal("0.50")]},
            {"volume": Decimal("100"), "liquidity": Decimal("50"), "outcome_prices": [Decimal("0.15"), Decimal("0.85")]},
            {"volume": Decimal("50000"), "liquidity": Decimal("25000"), "outcome_prices": [Decimal("0.30"), Decimal("0.70")]},
        ]
        for v in variations:
            market = dict(SAMPLE_MARKET)
            market.update(v)
            score = compute_liquidity_score(market)
            assert 0.0 <= score <= 1.0, f"Score {score} out of range for {v}"


class TestLiquidityAnalyzer:
    def test_score_markets_sorts_descending(self):
        m1 = dict(SAMPLE_MARKET)
        m2 = dict(SAMPLE_MARKET)
        m2["volume"] = Decimal("100")
        m2["liquidity"] = Decimal("50")
        m2["outcome_prices"] = [Decimal("0.85"), Decimal("0.15")]

        analyzer = LiquidityAnalyzer()
        scored = analyzer.score_markets([m2, m1])
        assert len(scored) == 2
        assert scored[0]["liquidity_score"] >= scored[1]["liquidity_score"]

    def test_handles_missing_fields(self):
        analyzer = LiquidityAnalyzer()
        scored = analyzer.score_markets([{"bad_key": "bad_value"}])
        assert len(scored) == 0


# ── MarketSelector Tests ───────────────────────────────────────────


class TestMarketSelector:
    def test_select_top_markets(self):
        markets = []
        for i in range(10):
            m = dict(SAMPLE_MARKET)
            m["condition_id"] = f"0x{i}"
            m["volume"] = Decimal(str(100000 + i * 50000))
            m["liquidity"] = Decimal(str(50000 + i * 25000))
            m["liquidity_score"] = 0.4 + i * 0.05
            markets.append(m)

        selector = MarketSelector()
        sel_result = selector.select_top_markets(markets, top_n=5)
        selected = sel_result.selected
        assert len(selected) == 5
        assert selected[0]["liquidity_score"] >= selected[-1]["liquidity_score"]

    def test_filters_low_volume(self):
        m = dict(SAMPLE_MARKET)
        m["volume"] = Decimal("1000")
        m["liquidity"] = Decimal("100000")
        m["liquidity_score"] = 0.6

        selector = MarketSelector(min_volume=Decimal("50000"))
        sel_result = selector.select_top_markets([m], top_n=10)
        assert len(sel_result.selected) == 0

    def test_filters_low_liquidity(self):
        m = dict(SAMPLE_MARKET)
        m["volume"] = Decimal("100000")
        m["liquidity"] = Decimal("100")
        m["liquidity_score"] = 0.6

        selector = MarketSelector(min_liquidity=Decimal("25000"))
        sel_result = selector.select_top_markets([m], top_n=10)
        assert len(sel_result.selected) == 0

    def test_filters_out_of_range_price(self):
        m = dict(SAMPLE_MARKET)
        m["outcome_prices"] = [Decimal("0.95"), Decimal("0.05")]
        m["liquidity_score"] = 0.6

        selector = MarketSelector()
        sel_result = selector.select_top_markets([m], top_n=10)
        assert len(sel_result.selected) == 0

    def test_filters_disabled_order_book(self):
        m = dict(SAMPLE_MARKET)
        m["enable_order_book"] = False
        m["liquidity_score"] = 0.6

        selector = MarketSelector()
        sel_result = selector.select_top_markets([m], top_n=10)
        assert len(sel_result.selected) == 0

    def test_filters_low_score(self):
        m = dict(SAMPLE_MARKET)
        m["liquidity_score"] = 0.1

        selector = MarketSelector(min_score=0.4)
        sel_result = selector.select_top_markets([m], top_n=10)
        assert len(sel_result.selected) == 0


# ── UniverseAnalyzer Tests ─────────────────────────────────────────


class TestUniverseAnalyzer:
    def test_analyze_empty(self):
        analyzer = UniverseAnalyzer()
        result = analyzer.analyze([])
        assert result["total_markets"] == 0
        assert "error" in result

    def test_analyze_single_market(self):
        analyzer = UniverseAnalyzer()
        result = analyzer.analyze([SAMPLE_MARKET])
        assert result["total_markets"] == 1
        assert result["avg_score"] > 0
        assert result["selected_count"] >= 0

    def test_category_stats(self):
        m1 = dict(SAMPLE_MARKET)
        m2 = dict(SAMPLE_MARKET)
        m2["condition_id"] = "0x2"
        m2["category"] = "politics"

        analyzer = UniverseAnalyzer()
        stats = analyzer._category_stats([m1, m2])
        assert "crypto" in stats
        assert "politics" in stats

    def test_histogram(self):
        result = UniverseAnalyzer._histogram([1.0, 2.0, 3.0, 4.0, 5.0], bins=5)
        assert len(result) == 5
        total = sum(b["count"] for b in result)
        assert total == 5


# ── WhitepaperGenerator Tests ──────────────────────────────────────


class TestMarketUniverseReportGenerator:
    def test_generate_creates_html_file(self, tmp_path):
        markets = [dict(SAMPLE_MARKET)]
        selected = [dict(SAMPLE_MARKET)]

        generator = MarketUniverseReportGenerator()
        output_path = generator.generate(
            all_markets=markets,
            selected_markets=selected,
            output_dir=str(tmp_path),
        )

        import os
        assert os.path.exists(output_path)
        with open(output_path) as f:
            content = f.read()
        assert "Polymarket Market Universe Analysis" in content
        assert "Will Bitcoin close above $100k" in content
        assert "Plotly" in content or "plotly" in content

    def test_generate_with_no_selected(self, tmp_path):
        generator = MarketUniverseReportGenerator()
        output_path = generator.generate(
            all_markets=[],
            selected_markets=[],
            output_dir=str(tmp_path),
        )
        import os
        assert os.path.exists(output_path)


# ── Integration Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_dry_run(tmp_path):
    """End-to-end test using sample data."""
    m1 = dict(SAMPLE_MARKET)
    m2 = {
        **dict(SAMPLE_MARKET),
        "condition_id": "0x2",
        "volume": Decimal("50000000"),
        "liquidity": Decimal("10000000"),
        "outcome_prices": [Decimal("0.55"), Decimal("0.45")],
        "category": "politics",
    }
    m3 = {
        **dict(SAMPLE_MARKET),
        "condition_id": "0x3",
        "volume": Decimal("1000"),
        "liquidity": Decimal("500"),
        "outcome_prices": [Decimal("0.85"), Decimal("0.15")],
    }
    markets = [m1, m2, m3]

    analyzer = LiquidityAnalyzer()
    scored = analyzer.score_markets(markets)
    assert len(scored) == 3

    selector = MarketSelector()
    sel_result = selector.select_top_markets(scored, top_n=10)
    selected = sel_result.selected
    assert 1 <= len(selected) <= 3

    generator = MarketUniverseReportGenerator()
    output_path = generator.generate(
        all_markets=scored,
        selected_markets=selected,
        output_dir=str(tmp_path / "whitepaper"),
    )

    import os
    assert os.path.exists(output_path)
    with open(output_path) as f:
        html = f.read()
    assert "Polymarket" in html
    assert len(scored) >= len(selected)
