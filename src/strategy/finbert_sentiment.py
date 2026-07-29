import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from src.config.optimization_settings import LOCAL_OPTIMIZATION_CONFIG
from src.strategy.finbert_config import FINBERT_CONFIG, MODEL_LABEL_MAP
from src.strategy.finbert_utils import (
    compute_compound_score,
    implied_probability_from_compound,
    preprocess_text,
)

logger = logging.getLogger(__name__)

_TORCH_AVAILABLE = False
_OPTIMUM_AVAILABLE = False
_ONNXRUNTIME_AVAILABLE = False

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    pass

try:
    import optimum
    _OPTIMUM_AVAILABLE = True
except ImportError:
    pass

try:
    import onnxruntime
    _ONNXRUNTIME_AVAILABLE = True
except ImportError:
    pass


class FinBERTError(Exception):
    pass


class ModelLoadError(FinBERTError):
    pass


class InferenceError(FinBERTError):
    pass


@dataclass
class SentimentResult:
    text: str
    positive_prob: Decimal
    negative_prob: Decimal
    neutral_prob: Decimal
    sentiment_label: str
    confidence: Decimal
    compound_score: Decimal
    implied_probability: Decimal
    latency_ms: float


class FinBERTSentimentAnalyzer:
    _instance = None
    _instance_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls, **kwargs) -> "FinBERTSentimentAnalyzer":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
                await cls._instance.load_model()
            return cls._instance

    def __init__(
        self,
        model_name: str = FINBERT_CONFIG["model_name"],
        use_onnx: bool = FINBERT_CONFIG["use_onnx"],
        use_quantization: bool = FINBERT_CONFIG["use_quantization"],
        device: str = FINBERT_CONFIG["device"],
        max_length: int = FINBERT_CONFIG["max_length"],
        batch_size: int = FINBERT_CONFIG["batch_size"],
        cache_size: int = FINBERT_CONFIG["sentiment_cache_size"],
        cache_ttl: int = FINBERT_CONFIG["sentiment_cache_ttl_seconds"],
        confidence_threshold: Decimal = FINBERT_CONFIG["confidence_threshold"],
        enabled_for_markets: list[str] | None = None,
        update_interval_seconds: int = 300,
    ) -> None:
        self.model_name = model_name
        self.use_onnx = use_onnx
        self.use_quantization = use_quantization
        self.device = device
        self.max_length = max_length
        self.batch_size = batch_size
        self.confidence_threshold = confidence_threshold
        self.enabled_for_markets = enabled_for_markets or ["long_term"]
        self._update_interval_s = update_interval_seconds
        self._last_update_time: dict[str, float] = {}  # market_id -> last update time

        self._model = None
        self._tokenizer = None
        self._is_onnx = False
        self._label_map: Dict[int, str] = MODEL_LABEL_MAP.get(
            model_name, {0: "positive", 1: "negative", 2: "neutral"}
        )
        self._cache: TTLCache = TTLCache(maxsize=cache_size, ttl=cache_ttl)
        self._lock = asyncio.Lock()
        self._loaded = False
        self.sentiment_available: bool = False

    async def load_model(self) -> None:
        async with self._lock:
            if self._loaded:
                return

            if self.use_onnx and _OPTIMUM_AVAILABLE and _ONNXRUNTIME_AVAILABLE:
                try:
                    await self._load_onnx()
                    self._loaded = True
                    self.sentiment_available = True
                    self._is_onnx = True
                    logger.info("FinBERT model loaded via ONNX: %s", self.model_name)
                    return
                except ImportError:
                    logger.info("optimum.onnxruntime not installed, skipping ONNX")
                except Exception as e:
                    logger.warning("ONNX load failed: %s", e)

            if _TORCH_AVAILABLE:
                try:
                    await self._load_pytorch()
                    self._loaded = True
                    self.sentiment_available = True
                    self._is_onnx = False
                    logger.info("FinBERT model loaded via PyTorch: %s (device=%s)", self.model_name, self.device)
                    return
                except ImportError:
                    logger.info("torch not installed, cannot load FinBERT")
                except Exception as e:
                    logger.warning("PyTorch load failed: %s", e)

            self._loaded = True
            self.sentiment_available = False
            logger.warning("No sentiment model available for %s — will use neutral fallback", self.model_name)

    async def _load_pytorch(self) -> None:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._tokenizer = await asyncio.to_thread(
            AutoTokenizer.from_pretrained, self.model_name
        )
        self._model = await asyncio.to_thread(
            AutoModelForSequenceClassification.from_pretrained, self.model_name
        )
        self._model.eval()
        if self.device != "cpu":
            self._model = self._model.to(self.device)

    async def _load_onnx(self) -> None:
        from transformers import AutoTokenizer

        self._tokenizer = await asyncio.to_thread(
            AutoTokenizer.from_pretrained, self.model_name
        )

        try:
            import onnxruntime as ort
            from optimum.onnxruntime import ORTModelForSequenceClassification

            # Configure ONNX Runtime session options for maximum CPU performance
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            )
            mc_cfg = LOCAL_OPTIMIZATION_CONFIG.get("finbert", {})
            session_options.intra_op_num_threads = mc_cfg.get("intra_op_threads", 2)
            session_options.inter_op_num_threads = mc_cfg.get("inter_op_threads", 1)
            session_options.enable_mem_pattern = True
            session_options.enable_cpu_mem_arena = True
            session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

            logger.info(
                "ONNX session: intra_op_threads=%d inter_op_threads=%d "
                "graph_opt=%s mem_pattern=%s cpu_arena=%s",
                session_options.intra_op_num_threads,
                session_options.inter_op_num_threads,
                session_options.graph_optimization_level,
                session_options.enable_mem_pattern,
                session_options.enable_cpu_mem_arena,
            )

            try:
                self._model = await asyncio.to_thread(
                    ORTModelForSequenceClassification.from_pretrained,
                    self.model_name,
                    export=False,
                    provider="CPUExecutionProvider",
                    session_options=session_options,
                )
            except Exception:
                logger.info("ONNX export not cached, exporting model to ONNX...")
                self._model = await asyncio.to_thread(
                    ORTModelForSequenceClassification.from_pretrained,
                    self.model_name,
                    export=True,
                    provider="CPUExecutionProvider",
                    session_options=session_options,
                )

            if self.use_quantization:
                try:
                    from optimum.onnxruntime import ORTQuantizer
                    from optimum.onnxruntime.configuration import AutoQuantizationConfig

                    quantizer = ORTQuantizer.from_pretrained(self._model)
                    qconfig = AutoQuantizationConfig.avx512_vnni(
                        is_static=False, per_channel=True
                    )
                    self._model = await asyncio.to_thread(
                        quantizer.quantize, quantization_config=qconfig
                    )
                    logger.info("INT8 quantization applied to ONNX model")
                except Exception:
                    logger.warning("INT8 quantization failed, using FP32 ONNX")

        except Exception:
            logger.warning("ONNX Runtime load failed — falling back to PyTorch")
            raise

        # Warm-up: first inference is always slower due to JIT compilation
        await self._onnx_warmup()

    async def _onnx_warmup(self) -> None:
        """Execute warm-up inference to avoid cold-start latency.

        The first ONNX Runtime inference call is significantly slower
        due to graph optimization, memory allocation, and thread pool
        initialization. Running a dummy inference at load time ensures
        that subsequent real inferences see consistent low latency.
        """
        logger.info("ONNX warm-up: running dummy inference...")
        try:
            dummy = "The market condition is neutral with moderate volume."
            start = time.perf_counter()
            await asyncio.to_thread(self._inference_onnx_single, dummy)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("ONNX warm-up complete: %.1fms (first inference)", elapsed_ms)

            # Second warm-up to stabilize thread pool
            start = time.perf_counter()
            await asyncio.to_thread(self._inference_onnx_single, dummy)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("ONNX warm-up second call: %.1fms (steady state)", elapsed_ms)
        except Exception as exc:
            logger.warning("ONNX warm-up failed: %s (non-fatal)", exc)

    async def analyze(self, text: str) -> SentimentResult:
        cache_key = hashlib_md5(text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for text (md5=%s)", cache_key[:8])
            return cached

        cleaned = preprocess_text(text)
        if not cleaned:
            result = SentimentResult(
                text=text,
                positive_prob=Decimal("0"),
                negative_prob=Decimal("0"),
                neutral_prob=Decimal("1"),
                sentiment_label="neutral",
                confidence=Decimal("1"),
                compound_score=Decimal("0"),
                implied_probability=Decimal("0.5"),
                latency_ms=0.0,
            )
            self._cache[cache_key] = result
            return result

        if not self._loaded:
            await self.load_model()

        if not self.sentiment_available:
            result = SentimentResult(
                text=text,
                positive_prob=Decimal("0"),
                negative_prob=Decimal("0"),
                neutral_prob=Decimal("1"),
                sentiment_label="neutral",
                confidence=Decimal("0"),
                compound_score=Decimal("0"),
                implied_probability=Decimal("0.5"),
                latency_ms=0.0,
            )
            self._cache[cache_key] = result
            return result

        start = time.perf_counter()
        try:
            positive_prob, negative_prob, neutral_prob, label = await asyncio.to_thread(
                self._inference_single, cleaned
            )
        except Exception as e:
            logger.exception("Inference failed for text: %s", cleaned[:80])
            raise InferenceError(f"Inference failed: {e}") from e

        latency = (time.perf_counter() - start) * 1000

        compound = compute_compound_score(positive_prob, negative_prob, neutral_prob)
        implied = implied_probability_from_compound(compound)
        confidence = max(positive_prob, negative_prob, neutral_prob)

        result = SentimentResult(
            text=text,
            positive_prob=positive_prob,
            negative_prob=negative_prob,
            neutral_prob=neutral_prob,
            sentiment_label=label,
            confidence=confidence,
            compound_score=compound,
            implied_probability=implied,
            latency_ms=latency,
        )

        self._cache[cache_key] = result
        logger.debug(
            "Sentiment: label=%s confidence=%s compound=%s latency=%.1fms",
            label, confidence, compound, latency,
        )
        return result

    def _inference_single(self, text: str) -> Tuple[Decimal, Decimal, Decimal, str]:
        if self._is_onnx:
            return self._inference_onnx_single(text)

        if not _TORCH_AVAILABLE:
            raise InferenceError("PyTorch is not available")

        inputs = self._tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
        )
        if self.device != "cpu":
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

        import torch
        with torch.no_grad():
            outputs = self._model(**inputs)
            logits = outputs.logits
            probabilities = torch.softmax(logits, dim=-1).squeeze()

        probs = probabilities.tolist()
        if isinstance(probs, float):
            probs = [probs]

        positive_prob = Decimal(str(round(float(probs[0]), 6)))
        negative_prob = Decimal(str(round(float(probs[1]), 6)))
        neutral_prob = Decimal(str(round(float(probs[2]), 6)))

        label_idx = int(torch.argmax(probabilities).item())
        label = self._label_map.get(label_idx, "neutral")

        return positive_prob, negative_prob, neutral_prob, label

    def _inference_onnx_single(self, text: str) -> Tuple[Decimal, Decimal, Decimal, str]:
        import numpy as np

        encoded = self._tokenizer(
            text,
            return_tensors="np",
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
        )

        onnx_inputs = {
            "input_ids": encoded["input_ids"],
            "attention_mask": encoded["attention_mask"],
        }
        if "token_type_ids" in encoded:
            onnx_inputs["token_type_ids"] = encoded["token_type_ids"]

        onnx_outputs = self._model(**onnx_inputs)
        logits = onnx_outputs.logits if hasattr(onnx_outputs, "logits") else onnx_outputs[0]
        exp = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
        probs = exp / exp.sum(axis=-1, keepdims=True)
        probs = probs.squeeze()

        if probs.ndim == 0:
            probs = np.array([probs])

        positive_prob = Decimal(str(round(float(probs[0]), 6)))
        negative_prob = Decimal(str(round(float(probs[1]), 6)))
        neutral_prob = Decimal(str(round(float(probs[2]), 6)))

        label_idx = int(np.argmax(probs))
        label = self._label_map.get(label_idx, "neutral")

        return positive_prob, negative_prob, neutral_prob, label

    async def analyze_batch(self, texts: List[str]) -> List[SentimentResult]:
        if not texts:
            return []

        uncached_texts: List[str] = []
        uncached_indices: List[int] = []
        results: List[Optional[SentimentResult]] = [None] * len(texts)

        for i, text in enumerate(texts):
            cache_key = hashlib_md5(text)
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[i] = cached
            else:
                uncached_texts.append(text)
                uncached_indices.append(i)

        if uncached_texts:
            if not self._loaded:
                await self.load_model()

            if not self.sentiment_available:
                for i, text in enumerate(uncached_texts):
                    idx = uncached_indices[i]
                    results[idx] = SentimentResult(
                        text=text,
                        positive_prob=Decimal("0"),
                        negative_prob=Decimal("0"),
                        neutral_prob=Decimal("1"),
                        sentiment_label="neutral",
                        confidence=Decimal("0"),
                        compound_score=Decimal("0"),
                        implied_probability=Decimal("0.5"),
                        latency_ms=0.0,
                    )
                    self._cache[hashlib_md5(text)] = results[idx]
                return [r for r in results if r is not None]

            for batch_start in range(0, len(uncached_texts), self.batch_size):
                batch = uncached_texts[batch_start:batch_start + self.batch_size]
                try:
                    batch_results = await asyncio.to_thread(
                        self._inference_batch, batch
                    )
                    for j, result in enumerate(batch_results):
                        idx = uncached_indices[batch_start + j]
                        results[idx] = result
                        self._cache[hashlib_md5(batch[j])] = result
                except Exception as e:
                    logger.exception("Batch inference failed for %d texts", len(batch))
                    for j in range(len(batch)):
                        idx = uncached_indices[batch_start + j]
                        results[idx] = SentimentResult(
                            text=batch[j],
                            positive_prob=Decimal("0"),
                            negative_prob=Decimal("0"),
                            neutral_prob=Decimal("1"),
                            sentiment_label="neutral",
                            confidence=Decimal("1"),
                            compound_score=Decimal("0"),
                            implied_probability=Decimal("0.5"),
                            latency_ms=0.0,
                        )

        return [r for r in results if r is not None]

    def _inference_batch(self, texts: List[str]) -> List[SentimentResult]:
        if self._is_onnx:
            return self._inference_onnx_batch(texts)

        if not _TORCH_AVAILABLE:
            raise InferenceError("PyTorch is not available")

        import torch
        cleaned = [preprocess_text(t) for t in texts]
        cleaned = [t if t else "empty" for t in cleaned]

        inputs = self._tokenizer(
            cleaned,
            return_tensors="pt",
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
        )
        if self.device != "cpu":
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self._model(**inputs)
            logits = outputs.logits
            all_probs = torch.softmax(logits, dim=-1)

        all_probs_np = all_probs.cpu().numpy()
        results: List[SentimentResult] = []
        for i, text in enumerate(texts):
            probs = all_probs_np[i]
            positive_prob = Decimal(str(round(float(probs[0]), 6)))
            negative_prob = Decimal(str(round(float(probs[1]), 6)))
            neutral_prob = Decimal(str(round(float(probs[2]), 6)))

            label_idx = int(all_probs[i].argmax().item())
            label = self._label_map.get(label_idx, "neutral")

            compound = compute_compound_score(positive_prob, negative_prob, neutral_prob)
            implied = implied_probability_from_compound(compound)
            confidence = max(positive_prob, negative_prob, neutral_prob)

            results.append(
                SentimentResult(
                    text=text,
                    positive_prob=positive_prob,
                    negative_prob=negative_prob,
                    neutral_prob=neutral_prob,
                    sentiment_label=label,
                    confidence=confidence,
                    compound_score=compound,
                    implied_probability=implied,
                    latency_ms=0.0,
                )
            )
        return results

    def _inference_onnx_batch(self, texts: List[str]) -> List[SentimentResult]:
        import numpy as np

        cleaned = [preprocess_text(t) for t in texts]
        cleaned = [t if t else "empty" for t in cleaned]

        encoded = self._tokenizer(
            cleaned,
            return_tensors="np",
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
        )

        onnx_inputs = {
            "input_ids": encoded["input_ids"],
            "attention_mask": encoded["attention_mask"],
        }
        if "token_type_ids" in encoded:
            onnx_inputs["token_type_ids"] = encoded["token_type_ids"]

        onnx_outputs = self._model(**onnx_inputs)
        logits = onnx_outputs.logits if hasattr(onnx_outputs, "logits") else onnx_outputs[0]
        exp = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
        all_probs = exp / exp.sum(axis=-1, keepdims=True)

        results: List[SentimentResult] = []
        for i, text in enumerate(texts):
            probs = all_probs[i]
            positive_prob = Decimal(str(round(float(probs[0]), 6)))
            negative_prob = Decimal(str(round(float(probs[1]), 6)))
            neutral_prob = Decimal(str(round(float(probs[2]), 6)))

            label_idx = int(np.argmax(probs))
            label = self._label_map.get(label_idx, "neutral")

            compound = compute_compound_score(positive_prob, negative_prob, neutral_prob)
            implied = implied_probability_from_compound(compound)
            confidence = max(positive_prob, negative_prob, neutral_prob)

            results.append(
                SentimentResult(
                    text=text,
                    positive_prob=positive_prob,
                    negative_prob=negative_prob,
                    neutral_prob=neutral_prob,
                    sentiment_label=label,
                    confidence=confidence,
                    compound_score=compound,
                    implied_probability=implied,
                    latency_ms=0.0,
                )
            )
        return results

    def get_implied_probability(
        self, sentiment: SentimentResult, market_question: str
    ) -> Decimal:
        return sentiment.implied_probability

    def is_enabled_for_market(self, market_type: str) -> bool:
        """Check if FinBERT is enabled for this market type.

        Short-duration markets (5min, 15min) have FinBERT DISABLED
        because its 65-180ms latency is incompatible with <100ms execution.
        Only long-term markets use FinBERT.

        Parameters
        ----------
        market_type : str
            Market type: "crypto_5min", "crypto_15min", or "long_term".

        Returns
        -------
        bool
            True if FinBERT should process this market.
        """
        return market_type in self.enabled_for_markets

    def should_update(self, market_id: str) -> bool:
        """Check if enough time has passed since last update.

        FinBERT updates are throttled to once every `update_interval_seconds`
        (default 300s = 5 minutes) for long-term markets.
        """
        last = self._last_update_time.get(market_id, 0.0)
        return (time.time() - last) >= self._update_interval_s

    def _mark_updated(self, market_id: str) -> None:
        self._last_update_time[market_id] = time.time()

    def map_to_trading_signal(
        self, sentiment: SentimentResult, current_market_price: Decimal
    ) -> Tuple[str, Decimal]:
        if sentiment.confidence < self.confidence_threshold:
            return ("NONE", Decimal("0"))

        compound = sentiment.implied_probability - Decimal("0.5")
        edge = sentiment.implied_probability - current_market_price

        if edge > 0:
            return ("BUY_YES", edge)
        else:
            return ("BUY_NO", abs(edge))

    async def compute_sentiment_signal(
        self,
        market: Dict[str, Any],
        current_price: Decimal,
        news_texts: List[str],
        market_type: str = "long_term",
        market_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Compute sentiment signal with market-type gating.

        For short-duration markets (5min, 15min), FinBERT is entirely skipped
        to avoid the 65-180ms inference latency.

        For long-term markets, throttled to once per `update_interval_seconds`.
        """
        if not self.is_enabled_for_market(market_type):
            logger.debug("FinBERT disabled for market type %s", market_type)
            return None

        if market_id and not self.should_update(market_id):
            logger.debug("FinBERT throttled for %s — update interval not elapsed", market_id)
            return None

        if not news_texts:
            logger.debug("No news texts provided for market %s", market.get("id"))
            return None

        results = await self.analyze_batch(news_texts)

        if market_id:
            self._mark_updated(market_id)

        high_confidence = [
            r for r in results if r.confidence >= self.confidence_threshold
        ]
        if not high_confidence:
            return None

        total_weight = sum(r.confidence for r in high_confidence)
        implied_prob = sum(
            r.implied_probability * r.confidence for r in high_confidence
        ) / total_weight

        edge = implied_prob - current_price

        if abs(edge) >= FINBERT_CONFIG["min_edge"]:
            direction = "BUY_YES" if edge > 0 else "BUY_NO"
            return {
                "source": "sentiment",
                "direction": direction,
                "implied_probability": implied_prob,
                "edge": edge,
                "confidence": total_weight / Decimal(str(len(high_confidence))),
                "num_articles": len(high_confidence),
                "articles": [
                    (r.text, r.sentiment_label, r.confidence)
                    for r in high_confidence[:5]
                ],
            }

        return None


def hashlib_md5(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode()).hexdigest()
