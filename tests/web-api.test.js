"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { MarketDataError, calculateConfidence, fetchMarkets, normalizeMarket } = require("../serverless/market-data");
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
  events: [{ slug: "bitcoin-price-in-2027" }]
};

test("normalizes Gamma string arrays, numbers, category, and URL", () => {
  const market = normalizeMarket(RAW_MARKET);
  assert.equal(market.probability, 0.63);
  assert.equal(market.category, "Crypto");
  assert.equal(market.liquidity, 25000.5);
  assert.equal(market.url, "https://polymarket.com/event/bitcoin-price-in-2027");
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
