"use strict";

const GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets";
const DEFAULT_LIMIT = 60;
const MAX_LIMIT = 100;
const TIMEOUT_MS = 10000;

class MarketDataError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "MarketDataError";
  }
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function clamp(value, minimum = 0, maximum = 1) {
  return Math.min(Math.max(value, minimum), maximum);
}

function logarithmicScore(value, reference) {
  return clamp(Math.log10(1 + Math.max(value, 0)) / Math.log10(1 + reference));
}

function calculateIntelligence(raw, probability) {
  const changes = {
    week: finiteNumber(raw.oneWeekPriceChange),
    day: finiteNumber(raw.oneDayPriceChange),
    hour: finiteNumber(raw.oneHourPriceChange)
  };
  const points = [
    [changes.week, 168],
    [changes.day, 24],
    [changes.hour, 1]
  ].filter(([change]) => change !== null)
    .map(([change, hoursAgo]) => ({ hoursAgo, probability: clamp(probability - change) }));
  points.push({ hoursAgo: 0, probability });

  const volume24h = finiteNumber(raw.volume24hr);
  const liquidity = finiteNumber(raw.liquidityNum ?? raw.liquidity);
  const activityRatio = volume24h !== null && liquidity !== null && liquidity > 0
    ? volume24h / liquidity
    : null;
  const changeMagnitude = changes.day === null ? null : Math.abs(changes.day);
  const activityInputs = [
    activityRatio === null ? null : clamp(Math.log10(1 + activityRatio) / Math.log10(11)),
    changeMagnitude === null ? null : clamp(changeMagnitude / 0.2)
  ].filter(value => value !== null);
  const activityScore = activityInputs.length
    ? Math.round(activityInputs.reduce((sum, value) => sum + value, 0) / activityInputs.length * 100)
    : null;

  let explanation = "Gamma no publica todavía suficiente histórico para explicar un movimiento reciente.";
  if (changes.day !== null) {
    const direction = changes.day > 0 ? "subió" : changes.day < 0 ? "bajó" : "no cambió";
    const activity = activityRatio === null
      ? "sin una lectura comparable de actividad"
      : `con volumen de 24h equivalente a ${activityRatio.toFixed(1)} veces la liquidez`;
    explanation = `El precio de Sí ${direction} ${(Math.abs(changes.day) * 100).toFixed(1)} puntos porcentuales en 24h, ${activity}.`;
  }

  return {
    source: "gamma",
    points,
    change1h: changes.hour,
    change24h: changes.day,
    change7d: changes.week,
    activityRatio,
    activityScore,
    activityLevel: activityScore === null ? "unknown" : activityScore >= 70 ? "high" : activityScore >= 40 ? "medium" : "low",
    explanation
  };
}

function calculateConfidence(raw) {
  const liquidity = finiteNumber(raw.liquidityNum ?? raw.liquidity);
  const volume24h = finiteNumber(raw.volume24hr);
  const bestBid = finiteNumber(raw.bestBid);
  const bestAsk = finiteNumber(raw.bestAsk);
  const explicitSpread = finiteNumber(raw.spread);
  const spread = explicitSpread !== null
    ? Math.abs(explicitSpread)
    : bestBid !== null && bestAsk !== null && bestAsk >= bestBid
      ? bestAsk - bestBid
      : null;
  const priceChange = finiteNumber(raw.oneDayPriceChange);
  const competitive = finiteNumber(raw.competitive);

  const factors = [
    { key: "liquidity", label: "Liquidez", value: liquidity === null ? null : logarithmicScore(liquidity, 1000000), weight: 0.25 },
    { key: "activity", label: "Actividad 24h", value: volume24h === null ? null : logarithmicScore(volume24h, 250000), weight: 0.25 },
    { key: "spread", label: "Spread", value: spread === null ? null : clamp(1 - spread / 0.1), weight: 0.25 },
    { key: "stability", label: "Estabilidad 24h", value: priceChange === null ? null : clamp(1 - Math.abs(priceChange) / 0.15), weight: 0.15 },
    { key: "competition", label: "Competitividad", value: competitive === null ? null : clamp(competitive), weight: 0.10 }
  ];
  const available = factors.filter(factor => factor.value !== null);
  const totalWeight = available.reduce((sum, factor) => sum + factor.weight, 0);
  const score = totalWeight
    ? Math.round(available.reduce((sum, factor) => sum + factor.value * factor.weight, 0) / totalWeight * 100)
    : 0;

  return {
    score,
    level: score >= 75 ? "high" : score >= 50 ? "medium" : "low",
    coverage: Math.round(totalWeight * 100),
    factors: available.map(factor => ({
      key: factor.key,
      label: factor.label,
      score: Math.round(factor.value * 100)
    }))
  };
}

function jsonList(value) {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string") return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function category(raw) {
  const explicit = String(raw.category || "").trim();
  if (explicit) return explicit.replace(/\b\w/g, letter => letter.toUpperCase());
  if (raw.sportsMarketType) return "Sports";

  for (const tag of jsonList(raw.tags)) {
    const label = tag && typeof tag === "object" ? tag.label : tag;
    if (label) return String(label).trim().replace(/\b\w/g, letter => letter.toUpperCase());
  }

  const text = [raw.question, raw.slug, raw.description].map(value => String(value || "")).join(" ").toLowerCase();
  const groups = [
    ["Crypto", ["bitcoin", "ethereum", "crypto", "btc", "eth"]],
    ["Politics", ["election", "elections", "president", "prime minister", "congress", "senate", "trump"]],
    ["Sports", ["nba", "nfl", "mlb", "baseball", "soccer", "football", "championship"]],
    ["Economy", ["fed", "inflation", "gdp", "interest rate", "recession"]],
    ["Technology", ["ai", "openai", "apple", "google", "tesla"]],
    ["Culture", ["album", "movie", "gta", "oscar", "grammy"]]
  ];
  for (const [name, keywords] of groups) {
    if (keywords.some(keyword => {
      const escaped = keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
      return new RegExp(`(?:^|\\W)${escaped}(?:$|\\W)`, "i").test(text);
    })) return name;
  }
  return "Other";
}

function normalizeMarket(raw) {
  const outcomes = jsonList(raw.outcomes).map(String);
  const prices = jsonList(raw.outcomePrices).map(number);
  const matchedIndex = outcomes.findIndex(value => value.toLowerCase() === "yes");
  const yesIndex = matchedIndex >= 0 ? matchedIndex : 0;
  const probability = Math.min(prices[yesIndex] ?? number(raw.lastTradePrice), 1);
  const event = Array.isArray(raw.events) && raw.events[0] && typeof raw.events[0] === "object"
    ? raw.events[0]
    : {};
  const slug = String(event.slug || raw.slug || "").trim();

  return {
    id: String(raw.conditionId || raw.id || ""),
    question: String(raw.question || "Untitled market").trim(),
    category: category(raw),
    probability: Math.round(probability * 10000) / 10000,
    volume: number(raw.volumeNum ?? raw.volume),
    volume24h: number(raw.volume24hr),
    liquidity: number(raw.liquidityNum ?? raw.liquidity),
    spread: finiteNumber(raw.spread),
    bestBid: finiteNumber(raw.bestBid),
    bestAsk: finiteNumber(raw.bestAsk),
    priceChange24h: finiteNumber(raw.oneDayPriceChange),
    confidence: calculateConfidence(raw),
    intelligence: calculateIntelligence(raw, Math.round(probability * 10000) / 10000),
    updatedAt: String(raw.updatedAt || ""),
    endDate: String(raw.endDate || raw.endDateIso || ""),
    url: slug ? `https://polymarket.com/event/${encodeURIComponent(slug)}` : "https://polymarket.com"
  };
}

async function fetchMarkets(limit = DEFAULT_LIMIT, offset = 0, fetchImpl = globalThis.fetch) {
  const safeLimit = Math.min(Math.max(Math.trunc(Number(limit)), 1), MAX_LIMIT);
  const safeOffset = Math.max(Math.trunc(Number(offset)), 0);
  const url = new URL(GAMMA_MARKETS_URL);
  url.search = new URLSearchParams({
    active: "true",
    closed: "false",
    limit: String(safeLimit),
    offset: String(safeOffset),
    order: "volume24hr",
    ascending: "false"
  });

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetchImpl(url, {
      headers: { Accept: "application/json", "User-Agent": "PELE-dashboard/1.0" },
      signal: controller.signal
    });
    if (!response || !response.ok) {
      throw new MarketDataError("Gamma API returned an unsuccessful status");
    }
    const payload = await response.json();
    if (!Array.isArray(payload)) {
      throw new MarketDataError("Gamma API returned an unexpected response");
    }
    return payload.filter(item => item && typeof item === "object").map(normalizeMarket);
  } catch (error) {
    if (error instanceof MarketDataError) throw error;
    throw new MarketDataError("Gamma API is temporarily unavailable", { cause: error });
  } finally {
    clearTimeout(timeout);
  }
}

module.exports = {
  DEFAULT_LIMIT,
  MAX_LIMIT,
  MarketDataError,
  calculateConfidence,
  calculateIntelligence,
  fetchMarkets,
  normalizeMarket
};
