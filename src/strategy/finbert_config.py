from decimal import Decimal
from typing import Any, Dict, List


FINBERT_CONFIG: Dict[str, Any] = {
    "model_name": "project-aps/finbert-finetune",
    "alternative_models": [
        "ProsusAI/finbert",
        "yiyanghkust/finbert-tone",
    ],
    "use_onnx": True,
    "use_quantization": False,
    "device": "cpu",
    "max_length": 512,
    "batch_size": 16,

    "news_api_key": None,
    "use_rss": True,
    "rss_feeds": [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "https://www.reuters.com/tools/rss",
    ],
    "max_articles_per_market": 10,
    "news_cache_ttl_seconds": 300,

    "sentiment_cache_size": 1000,
    "sentiment_cache_ttl_seconds": 600,

    "news_api_rate_limit": 100,
    "rss_rate_limit": 60,
    "sentiment_rate_limit": 100,

    "confidence_threshold": Decimal("0.6"),
    "min_edge": Decimal("0.03"),
    "sentiment_weight": Decimal("0.3"),

    "daily_api_cost_limit_usd": 10.0,
}

MODEL_LABEL_MAP: Dict[str, Dict[int, str]] = {
    "ProsusAI/finbert": {0: "positive", 1: "negative", 2: "neutral"},
    "yiyanghkust/finbert-tone": {0: "neutral", 1: "positive", 2: "negative"},
    "project-aps/finbert-finetune": {0: "positive", 1: "negative", 2: "neutral"},
}
