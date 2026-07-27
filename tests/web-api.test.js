"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { MarketDataError, calculateConfidence, calculateGbmProjection, calculateIntelligence, fetchMarkets, normalizeMarket } = require("../serverless/market-data");
const { parsePagination } = require("../api/markets");

const RAW_MARKET = {
  id: "42",
  conditionId: "0xabc",
  question: "Will Bitcoin reach 150k?",
  slug: "bitcoin-150k",
  outcomes: '["Yes", "No"]',
  outcomePrices: '["0.63", "0.37"]',
  volumeNum: 120000,
  volume24hr: 4200,
  liquidity: "25000.5",
  endDate: "2027-01-01T00:00:00Z",
  updatedAt: "2026-07-26T12:30:00Z",
  events: [{ slug: "bitcoin-price-in-2027" }]
};

test("normalizes Gamma string arrays, numbers, category, and URL", () => {
  const market = normalizeMarket(RAW_MARKET);
  assert.equal(market.probability, 0.63);
  assert.equal(market.category, "Crypto");
  assert.equal(market.liquidity, 25000.5);
  assert.equal(market.url, "https://polymarket.com/event/bitcoin-price-in-2027");
  assert.equal(market.updatedAt, "2026-07-26T12:30:00Z");
});

test("does not classify Ethiopia as crypto because it contains eth", () => {
  const market = normalizeMarket({
    question: "Will the Prime Minister of Ethiopia win the elections?",
    outcomes: '["Yes", "No"]',
    outcomePrices: '["0.5", "0.5"]'
  });
  assert.equal(market.category, "Politics");
});

test("uses Gamma sports metadata before keyword classification", () => {
  const market = normalizeMarket({
    question: "New York vs Philadelphia",
    sportsMarketType: "moneyline",
    outcomes: '["New York", "Philadelphia"]',
    outcomePrices: '["0.4", "0.6"]'
  });
  assert.equal(market.category, "Sports");
});

test("calculates an explainable confidence score from free public metrics", () => {
  const confidence = calculateConfidence({
    liquidityNum: 1000000,
    volume24hr: 250000,
    spread: 0.01,
    oneDayPriceChange: 0.01,
    competitive: 0.9
  });
  assert.equal(confidence.score, 96);
  assert.equal(confidence.level, "high");
  assert.equal(confidence.coverage, 100);
  assert.deepEqual(confidence.factors.map(factor => factor.key), [
    "liquidity", "activity", "spread", "stability", "competition"
  ]);
});

test("renormalizes confidence when optional market metrics are missing", () => {
  const confidence = calculateConfidence({ liquidityNum: 1000000 });
  assert.equal(confidence.score, 100);
  assert.equal(confidence.coverage, 25);
  assert.equal(confidence.factors.length, 1);
});

test("builds intelligence only from observed Gamma metrics", () => {
  const intelligence = calculateIntelligence({
    oneHourPriceChange: "0.01",
    oneDayPriceChange: "0.05",
    oneWeekPriceChange: "-0.03",
    volume24hr: 50000,
    liquidityNum: 25000
  }, 0.6);

  assert.deepEqual(intelligence.points, [
    { hoursAgo: 168, probability: 0.63 },
    { hoursAgo: 24, probability: 0.5499999999999999 },
    { hoursAgo: 1, probability: 0.59 },
    { hoursAgo: 0, probability: 0.6 }
  ]);
  assert.equal(intelligence.activityRatio, 2);
  assert.equal(intelligence.source, "gamma");
  assert.equal(Object.hasOwn(intelligence, "whaleShare"), false);
  assert.equal(Object.hasOwn(intelligence, "external"), false);
});

test("reports unavailable intelligence instead of inventing values", () => {
  const intelligence = calculateIntelligence({}, 0.42);
  assert.deepEqual(intelligence.points, [{ hoursAgo: 0, probability: 0.42 }]);
  assert.equal(intelligence.change24h, null);
  assert.equal(intelligence.activityRatio, null);
  assert.equal(intelligence.activityScore, null);
  assert.equal(intelligence.activityLevel, "unknown");
});

test("runs deterministic GBM paths in log-odds space", () => {
  const raw = {
    conditionId: "gbm-market",
    endDate: "2027-07-26T00:00:00Z",
    oneDayPriceChange: 0.03,
    oneWeekPriceChange: -0.02,
    spread: 0.01,
    bestBid: 0.59,
    bestAsk: 0.60
  };
  const now = Date.parse("2026-07-26T00:00:00Z");
  const first = calculateGbmProjection(raw, 0.6, now);
  const second = calculateGbmProjection(raw, 0.6, now);

  assert.deepEqual(first, second);
  assert.equal(first.status, "available");
  assert.equal(first.model, "gbm_log_odds");
  assert.equal(first.paths, 1000);
  assert.equal(first.horizonDays, 365);
  assert.equal(first.calibration, "gamma");
  assert.ok(first.p05 < first.median);
  assert.ok(first.median < first.p95);
  assert.ok(first.mean > 0 && first.mean < 1);
});

test("marks GBM assumptions and short-term exclusions explicitly", () => {
  const now = Date.parse("2026-07-26T00:00:00Z");
  const assumed = calculateGbmProjection({ id: "assumed", endDate: "2026-08-26T00:00:00Z" }, 0.5, now);
  const shortTerm = calculateGbmProjection({ id: "short", endDate: "2026-07-30T00:00:00Z" }, 0.5, now);

  assert.equal(assumed.status, "available");
  assert.equal(assumed.calibration, "assumed");
  assert.deepEqual(assumed.volatilitySources, ["default_assumption"]);
  assert.equal(shortTerm.status, "unavailable");
  assert.equal(shortTerm.reason, "short_term_market");
});

test("fetches without network and clamps the internal limit", async () => {
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return { ok: true, json: async () => [RAW_MARKET] };
  };
  const markets = await fetchMarkets(999, 300, fakeFetch);
  assert.equal(markets.length, 1);
  assert.match(calls[0].url, /limit=100/);
  assert.match(calls[0].url, /offset=300/);
  assert.equal(calls[0].options.signal.aborted, false);
});

test("wraps network errors in a safe domain error", async () => {
  const offlineFetch = async () => { throw new Error("private upstream detail"); };
  await assert.rejects(fetchMarkets(10, 0, offlineFetch), MarketDataError);
});

test("rejects a non-array Gamma payload", async () => {
  const badFetch = async () => ({ ok: true, json: async () => ({ bad: true }) });
  await assert.rejects(fetchMarkets(10, 0, badFetch), MarketDataError);
});

test("parses and validates public pagination", () => {
  assert.deepEqual(parsePagination("/api/markets?limit=25&offset=50"), { limit: 25, offset: 50 });
  for (const path of [
    "/api/markets?limit=nope",
    "/api/markets?limit=0",
    "/api/markets?limit=101",
    "/api/markets?offset=-1",
    "/api/markets?offset=nope"
  ]) {
    assert.throws(() => parsePagination(path), RangeError);
  }
});
