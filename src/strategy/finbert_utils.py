import re
from decimal import Decimal
from typing import List


def preprocess_text(text: str) -> str:
    text = re.sub(r"http\S+|www\S+|https\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"[^\x00-\x7F]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) > 400:
        text = " ".join(words[:400])
    return text


def extract_keywords(question: str, tags: List[str]) -> List[str]:
    keywords = set(tags)
    capitalized = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", question)
    keywords.update(capitalized)
    financial_terms = [
        "ETF", "SEC", "Fed", "CPI", "GDP", "earnings", "rates",
        "inflation", "recession", "bull", "bear", "rally", "crash",
    ]
    for term in financial_terms:
        if term.lower() in question.lower():
            keywords.add(term)
    return list(keywords)[:5]


def compute_compound_score(
    positive: Decimal, negative: Decimal, neutral: Decimal
) -> Decimal:
    return positive - negative


def estimate_edge(
    implied_prob: Decimal, market_price: Decimal
) -> Decimal:
    return implied_prob - market_price


def implied_probability_from_compound(
    compound: Decimal,
) -> Decimal:
    return Decimal("0.5") + (compound * Decimal("0.5"))
