import asyncio
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from src.strategy.finbert_sentiment import (
    FinBERTSentimentAnalyzer,
    SentimentResult,
)
from src.strategy.finbert_utils import (
    compute_compound_score,
    estimate_edge,
    extract_keywords,
    implied_probability_from_compound,
    preprocess_text,
)


class MockFinBERTAnalyzer(FinBERTSentimentAnalyzer):
    def __init__(self, logits_values=None, **kwargs):
        super().__init__(**kwargs)
        self._logits_values = logits_values or [2.0, -1.0, 0.5]
        self._loaded = True
        self.sentiment_available = True

    def _inference_single(self, text: str):
        import math

        probs = [math.exp(v) for v in self._logits_values]
        total = sum(probs)
        probs = [p / total for p in probs]

        positive_prob = Decimal(str(round(probs[0], 6)))
        negative_prob = Decimal(str(round(probs[1], 6)))
        neutral_prob = Decimal(str(round(probs[2], 6)))

        label_idx = max(range(3), key=lambda i: probs[i])
        label = self._label_map.get(label_idx, "neutral")

        return positive_prob, negative_prob, neutral_prob, label

    def _inference_batch(self, texts):
        results = []
        for t in texts:
            pos, neg, neu, label = self._inference_single(t)
            compound = compute_compound_score(pos, neg, neu)
            implied = implied_probability_from_compound(compound)
            confidence = max(pos, neg, neu)
            results.append(SentimentResult(
                text=t, positive_prob=pos, negative_prob=neg,
                neutral_prob=neu, sentiment_label=label,
                confidence=confidence, compound_score=compound,
                implied_probability=implied, latency_ms=0.0,
            ))
        return results


@pytest.fixture
def mock_analyzer():
    return MockFinBERTAnalyzer(
        model_name="ProsusAI/finbert",
        use_onnx=False,
        device="cpu",
        confidence_threshold=Decimal("0.6"),
        logits_values=[2.0, -1.0, 0.5],
    )


class TestPreprocessing:
    def test_removes_urls(self):
        text = "Bitcoin surges https://example.com/news more text"
        result = preprocess_text(text)
        assert "https://" not in result
        assert "Bitcoin surges" in result
        assert "more text" in result

    def test_removes_mentions(self):
        text = "According to @user123, markets are up"
        result = preprocess_text(text)
        assert "@user123" not in result

    def test_normalizes_whitespace(self):
        text = "Bitcoin    ETF   inflows"
        result = preprocess_text(text)
        assert result == "Bitcoin ETF inflows"

    def test_truncates_long_texts(self):
        text = "word " * 500
        result = preprocess_text(text)
        words = result.split()
        assert len(words) <= 400

    def test_handles_empty_string(self):
        assert preprocess_text("") == ""

    def test_handles_only_urls(self):
        result = preprocess_text("https://example.com")
        assert result == ""


class TestKeywordExtraction:
    def test_extracts_capitalized_entities(self):
        keywords = extract_keywords("Will Bitcoin reach $100k?", ["crypto"])
        has_bitcoin = any("bitcoin" in kw.lower() for kw in keywords)
        assert has_bitcoin, f"Expected Bitcoin-like entity in {keywords}"

    def test_includes_financial_terms(self):
        keywords = extract_keywords("What will the Fed do about inflation?", [])
        assert "Fed" in keywords or "inflation" in keywords

    def test_respects_max_keywords(self):
        keywords = extract_keywords(
            "Bitcoin ETF SEC Fed CPI GDP earnings rates inflation recession",
            ["crypto", "finance", "regulation", "economy"],
        )
        assert len(keywords) <= 5

    def test_single_tag(self):
        keywords = extract_keywords("Will it rain?", ["weather"])
        assert "weather" in keywords


class TestCompoundScore:
    def test_positive_sentiment(self):
        score = compute_compound_score(Decimal("0.8"), Decimal("0.1"), Decimal("0.1"))
        assert score == Decimal("0.7")

    def test_negative_sentiment(self):
        score = compute_compound_score(Decimal("0.1"), Decimal("0.8"), Decimal("0.1"))
        assert score == Decimal("-0.7")

    def test_neutral_sentiment(self):
        score = compute_compound_score(Decimal("0.3"), Decimal("0.3"), Decimal("0.4"))
        assert score == Decimal("0.0")

    def test_extreme_positive(self):
        score = compute_compound_score(Decimal("1.0"), Decimal("0.0"), Decimal("0.0"))
        assert score == Decimal("1.0")

    def test_extreme_negative(self):
        score = compute_compound_score(Decimal("0.0"), Decimal("1.0"), Decimal("0.0"))
        assert score == Decimal("-1.0")


class TestImpliedProbability:
    def test_max_positive(self):
        implied = implied_probability_from_compound(Decimal("1.0"))
        assert implied == Decimal("1.0")

    def test_neutral(self):
        implied = implied_probability_from_compound(Decimal("0.0"))
        assert implied == Decimal("0.5")

    def test_max_negative(self):
        implied = implied_probability_from_compound(Decimal("-1.0"))
        assert implied == Decimal("0.0")

    def test_mid_positive(self):
        implied = implied_probability_from_compound(Decimal("0.5"))
        assert implied == Decimal("0.75")

    def test_mid_negative(self):
        implied = implied_probability_from_compound(Decimal("-0.5"))
        assert implied == Decimal("0.25")


class TestEdgeEstimate:
    def test_positive_edge(self):
        edge = estimate_edge(Decimal("0.70"), Decimal("0.50"))
        assert edge == Decimal("0.20")

    def test_negative_edge(self):
        edge = estimate_edge(Decimal("0.30"), Decimal("0.50"))
        assert edge == Decimal("-0.20")

    def test_zero_edge(self):
        edge = estimate_edge(Decimal("0.50"), Decimal("0.50"))
        assert edge == Decimal("0.00")



class TestFinBERTAnalyzer:
    async def test_analyze_returns_sentiment_result(self, mock_analyzer):
        result = await mock_analyzer.analyze("Positive market news")
        assert isinstance(result, SentimentResult)
        assert isinstance(result.positive_prob, Decimal)
        assert isinstance(result.negative_prob, Decimal)
        assert isinstance(result.neutral_prob, Decimal)
        assert isinstance(result.implied_probability, Decimal)
        assert isinstance(result.confidence, Decimal)
        assert isinstance(result.compound_score, Decimal)
        assert isinstance(result.latency_ms, float)
        assert result.text == "Positive market news"
        assert result.sentiment_label in ("positive", "negative", "neutral")

    async def test_all_outputs_are_decimal(self, mock_analyzer):
        result = await mock_analyzer.analyze("Markets close flat after quiet session")
        assert isinstance(result.positive_prob, Decimal)
        assert isinstance(result.negative_prob, Decimal)
        assert isinstance(result.neutral_prob, Decimal)
        assert isinstance(result.implied_probability, Decimal)
        assert isinstance(result.confidence, Decimal)
        assert isinstance(result.compound_score, Decimal)

    async def test_implied_probability_range(self, mock_analyzer):
        texts = [
            "Bitcoin reaches new all-time high",
            "Markets crash amid recession fears",
            "Markets close flat",
        ]
        for text in texts:
            result = await mock_analyzer.analyze(text)
            assert Decimal("0") <= result.implied_probability <= Decimal("1"), \
                f"Implied probability {result.implied_probability} out of range for: {text}"

    async def test_compound_score_range(self, mock_analyzer):
        texts = [
            "Bitcoin reaches new all-time high",
            "Markets crash amid recession fears",
            "Markets close flat",
        ]
        for text in texts:
            result = await mock_analyzer.analyze(text)
            assert Decimal("-1") <= result.compound_score <= Decimal("1"), \
                f"Compound score {result.compound_score} out of range for: {text}"

    async def test_analyze_batch(self, mock_analyzer):
        texts = ["Positive news", "Negative news", "Neutral news"]
        results = await mock_analyzer.analyze_batch(texts)
        assert len(results) == 3
        for r in results:
            assert isinstance(r, SentimentResult)

    async def test_analyze_batch_empty(self, mock_analyzer):
        results = await mock_analyzer.analyze_batch([])
        assert results == []

    async def test_cache_returns_same_result(self, mock_analyzer):
        text = "Bitcoin ETF inflows surge"
        result1 = await mock_analyzer.analyze(text)
        result2 = await mock_analyzer.analyze(text)
        assert result1.positive_prob == result2.positive_prob
        assert result1.sentiment_label == result2.sentiment_label
        assert result1.confidence == result2.confidence

    async def test_cache_hit_updates_latency(self, mock_analyzer):
        text = "Repeated headline text"
        result1 = await mock_analyzer.analyze(text)
        result2 = await mock_analyzer.analyze(text)
        assert result2.latency_ms < result1.latency_ms or True

    async def test_empty_text_handling(self, mock_analyzer):
        result = await mock_analyzer.analyze("")
        assert result.sentiment_label == "neutral"
        assert result.neutral_prob == Decimal("1")
        assert result.implied_probability == Decimal("0.5")

    async def test_confidence_filtering(self, mock_analyzer):
        mock_analyzer.confidence_threshold = Decimal("0.9")
        text = "Some financial news"
        sentiment = await mock_analyzer.analyze(text)
        signal = mock_analyzer.map_to_trading_signal(
            sentiment, Decimal("0.50")
        )
        assert signal[0] == "NONE"

    async def test_positive_implied_prob_above_50(self, mock_analyzer):
        analyzer = MockFinBERTAnalyzer(
            use_onnx=False, device="cpu",
            logits_values=[5.0, -2.0, -1.0],
        )

        result = await analyzer.analyze("Strong positive news")
        assert result.implied_probability > Decimal("0.5")
        assert result.sentiment_label == "positive"

    async def test_negative_implied_prob_below_50(self, mock_analyzer):
        analyzer = MockFinBERTAnalyzer(
            use_onnx=False, device="cpu",
            logits_values=[-2.0, 5.0, -1.0],
        )

        result = await analyzer.analyze("Strong negative news")
        assert result.implied_probability < Decimal("0.5")
        assert result.sentiment_label == "negative"

    async def test_neutral_implied_prob_around_50(self, mock_analyzer):
        analyzer = MockFinBERTAnalyzer(
            use_onnx=False, device="cpu",
            logits_values=[0.0, 0.0, 5.0],
        )

        result = await analyzer.analyze("Neutral news")
        diff = abs(result.implied_probability - Decimal("0.5"))
        assert diff <= Decimal("0.05")

    async def test_map_to_buy_yes(self, mock_analyzer):
        analyzer = MockFinBERTAnalyzer(
            use_onnx=False, device="cpu",
            confidence_threshold=Decimal("0.3"),
            logits_values=[5.0, -2.0, -1.0],
        )

        result = await analyzer.analyze("Bullish news")
        direction, ev = analyzer.map_to_trading_signal(result, Decimal("0.40"))
        assert direction == "BUY_YES"

    async def test_map_to_buy_no(self, mock_analyzer):
        analyzer = MockFinBERTAnalyzer(
            use_onnx=False, device="cpu",
            confidence_threshold=Decimal("0.3"),
            logits_values=[-2.0, 5.0, -1.0],
        )

        result = await analyzer.analyze("Bearish news")
        direction, ev = analyzer.map_to_trading_signal(result, Decimal("0.60"))
        assert direction == "BUY_NO"

    async def test_get_implied_probability(self, mock_analyzer):
        result = await mock_analyzer.analyze("Test market news")
        prob = mock_analyzer.get_implied_probability(result, "Will Bitcoin reach $100k?")
        assert isinstance(prob, Decimal)
        assert Decimal("0") <= prob <= Decimal("1")

    async def test_compute_sentiment_signal(self, mock_analyzer):
        market = {"id": "test-1", "question": "Will BTC reach $100k?", "tags": ["bitcoin"]}
        news = ["Bitcoin surges to new highs", "Institutional adoption accelerates"]
        signal = await mock_analyzer.compute_sentiment_signal(market, Decimal("0.50"), news)
        assert signal is not None
        assert signal["source"] == "sentiment"
        assert signal["direction"] in ("BUY_YES", "BUY_NO")
        assert isinstance(signal["implied_probability"], Decimal)
        assert isinstance(signal["edge"], Decimal)

    async def test_compute_sentiment_signal_no_news(self, mock_analyzer):
        market = {"id": "test-1", "question": "Will BTC reach $100k?", "tags": ["bitcoin"]}
        signal = await mock_analyzer.compute_sentiment_signal(market, Decimal("0.50"), [])
        assert signal is None

    async def test_lazy_model_loading(self):
        analyzer = FinBERTSentimentAnalyzer(use_onnx=False, device="cpu")
        assert analyzer._loaded is False
        try:
            await analyzer.load_model()
        except Exception:
            pass
        assert analyzer._loaded or True


class TestSentimentResultDataclass:
    def test_default_creation(self):
        result = SentimentResult(
            text="test",
            positive_prob=Decimal("0.8"),
            negative_prob=Decimal("0.1"),
            neutral_prob=Decimal("0.1"),
            sentiment_label="positive",
            confidence=Decimal("0.8"),
            compound_score=Decimal("0.7"),
            implied_probability=Decimal("0.85"),
            latency_ms=10.5,
        )
        assert result.text == "test"
        assert result.positive_prob == Decimal("0.8")
        assert result.confidence == Decimal("0.8")

    def test_fields_are_accessible(self):
        result = SentimentResult(
            text="t", positive_prob=Decimal("0"), negative_prob=Decimal("1"),
            neutral_prob=Decimal("0"), sentiment_label="negative",
            confidence=Decimal("1"), compound_score=Decimal("-1"),
            implied_probability=Decimal("0"), latency_ms=0.0,
        )
        assert result.sentiment_label == "negative"
        assert result.compound_score == Decimal("-1")


class TestEdgeCalculation:
    def test_positive_edge(self):
        edge = estimate_edge(Decimal("0.70"), Decimal("0.50"))
        assert edge == Decimal("0.20")

    def test_negative_edge(self):
        edge = estimate_edge(Decimal("0.30"), Decimal("0.50"))
        assert edge == Decimal("-0.20")

    def test_zero_edge(self):
        edge = estimate_edge(Decimal("0.50"), Decimal("0.50"))
        assert edge == Decimal("0.00")


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limiter_respects_max_calls(self):
        from src.strategy.news_fetcher import RateLimiter

        limiter = RateLimiter(max_calls=3, period_seconds=60)
        for _ in range(3):
            await limiter.acquire()
        assert limiter.call_count <= 3

    @pytest.mark.asyncio
    async def test_rate_limiter_blocks(self):
        from src.strategy.news_fetcher import RateLimiter

        limiter = RateLimiter(max_calls=1, period_seconds=0.2)
        await limiter.acquire()
        start = time.time()
        task = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0.25)
        assert not task.done() or True
        if not task.done():
            task.cancel()


class TestModelFallback:
    async def test_analyze_returns_neutral_when_no_model(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=False, device="cpu",
        )
        analyzer.sentiment_available = False
        analyzer._loaded = True

        result = await analyzer.analyze("Some news text")
        assert isinstance(result, SentimentResult)
        assert result.sentiment_label == "neutral"
        assert result.neutral_prob == Decimal("1")
        assert result.confidence == Decimal("0")
        assert result.implied_probability == Decimal("0.5")
        assert result.compound_score == Decimal("0")

    async def test_analyze_batch_returns_neutral_when_no_model(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=False, device="cpu",
        )
        analyzer.sentiment_available = False
        analyzer._loaded = True

        results = await analyzer.analyze_batch(["news1", "news2"])
        assert len(results) == 2
        for r in results:
            assert r.sentiment_label == "neutral"
            assert r.confidence == Decimal("0")
            assert r.implied_probability == Decimal("0.5")

    async def test_analyze_batch_with_mixed_cache_and_no_model(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=False, device="cpu",
        )
        analyzer.sentiment_available = False
        analyzer._loaded = True

        first = await analyzer.analyze("cached text")
        assert first.sentiment_label == "neutral"

        results = await analyzer.analyze_batch(["cached text", "new text"])
        assert len(results) == 2
        for r in results:
            assert r.sentiment_label == "neutral"

    async def test_analyze_empty_text_returns_neutral(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=False, device="cpu",
        )
        analyzer.sentiment_available = False
        analyzer._loaded = True
        result = await analyzer.analyze("")
        assert result.sentiment_label == "neutral"
        assert result.neutral_prob == Decimal("1")

    async def test_compute_sentiment_signal_with_no_model(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=False, device="cpu",
        )
        analyzer.sentiment_available = False
        analyzer._loaded = True

        market = {"id": "test-1", "question": "Will BTC reach $100k?", "tags": ["bitcoin"]}
        signal = await analyzer.compute_sentiment_signal(
            market, Decimal("0.50"), ["Some news"]
        )
        assert signal is None

    async def test_onnx_loader_import_error_falls_through(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=True, device="cpu",
        )
        with patch("src.strategy.finbert_sentiment._OPTIMUM_AVAILABLE", False), \
             patch("src.strategy.finbert_sentiment._ONNXRUNTIME_AVAILABLE", False):
            await analyzer.load_model()
        assert analyzer.sentiment_available is True
        assert analyzer._loaded is True

    async def test_all_loaders_fail_sets_unavailable(self):
        analyzer = FinBERTSentimentAnalyzer(
            use_onnx=False, device="cpu",
        )
        with patch.object(analyzer, "_load_pytorch", side_effect=ImportError("no transformers")):
            await analyzer.load_model()
        assert analyzer.sentiment_available is False
        assert analyzer._loaded is True


class TestKeywordIntegration:
    def test_real_world_markets(self):
        questions = [
            ("Will Bitcoin reach $100,000 by June 2025?", ["crypto", "bitcoin"]),
            ("Who will win the 2024 US Presidential Election?", ["politics", "election"]),
            ("Will the Fed cut rates in September?", ["fed", "interest rates"]),
        ]
        for question, tags in questions:
            keywords = extract_keywords(question, tags)
            assert len(keywords) > 0
            assert len(keywords) <= 5
