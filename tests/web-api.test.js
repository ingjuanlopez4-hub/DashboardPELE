"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  MarketDataError,
  calculateConfidence,
  calculateGbmProjection,
  calculateIntelligence,
  calculateWalletConcentration,
  fetchMarkets,
  normalizeMarket,
  normalizeRegionalSources
} = require("../serverless/market-data");
const { parsePagination } = require("../api/markets");
const { createWebServer } = require("../scripts/web_server");
const { expectedReturn, simpleAverage, formatChange, classifyLifecycle, freshness } = require("../web/assets/market-math");

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

test("calculates relative expected return without assigning a currency", () => {
  assert.equal(expectedReturn(0.8, 0.5, 0.02), 0.56);
  assert.ok(Math.abs(expectedReturn(0.4, 0.5, 0.02) + 0.24) < Number.EPSILON);
  assert.equal(expectedReturn(0.8, 0, 0.02), null);
});

test("calculates an unweighted sample average", () => {
  assert.equal(simpleAverage([0.2, 0.5, 0.8]), 0.5);
  assert.equal(simpleAverage([]), null);
  assert.equal(simpleAverage([0.2, null, "bad", 0.8]), 0.5);
});

test("formats material change without positive or negative zero", () => {
  assert.equal(formatChange(-0.001), "Sin cambio material");
  assert.equal(formatChange(0), "Sin cambio material");
  assert.equal(formatChange(0.051), "+5 %");
  assert.equal(formatChange(null), "Sin dato");
  assert.equal(formatChange(undefined), "Sin dato");
});

test("classifies explicit lifecycle before inferring a passed deadline", () => {
  const now = Date.parse("2026-07-28T12:00:00Z");
  assert.deepEqual(classifyLifecycle({ lifecycle: { resolved: true }, endDate: "2027-01-01" }, now), { code: "resolved", label: "Resuelto" });
  assert.deepEqual(classifyLifecycle({ lifecycle: { active: true }, endDate: "2026-07-27" }, now), { code: "deadline_conflict", label: "Activo tras plazo" });
  assert.deepEqual(classifyLifecycle({ lifecycle: { active: true }, endDate: "2026-07-29" }, now), { code: "open", label: "Abierto" });
  assert.deepEqual(classifyLifecycle({ lifecycle: {}, endDate: "2026-07-29" }, now), { code: "unconfirmed", label: "Estado no confirmado" });
});

test("reports source freshness without treating clock skew as stale", () => {
  const now = Date.parse("2026-07-28T12:00:00Z");
  assert.equal(freshness("2026-07-28T11:30:00Z", now).label, "Hace 30 min");
  assert.equal(freshness("2026-07-26T12:00:00Z", now).code, "stale");
  assert.equal(freshness("2026-07-29T12:00:00Z", now).code, "invalid");
  assert.equal(freshness("2026-07-28T12:03:00Z", now).label, "Actualizado ahora");
  assert.equal(freshness(null, now).code, "unknown");
});

test("normalizes Gamma string arrays, numbers, category, and URL", () => {
  const market = normalizeMarket(RAW_MARKET);
  assert.equal(market.probability, 0.63);
  assert.equal(market.category, "Crypto");
  assert.equal(market.liquidity, 25000.5);
  assert.equal(market.url, "https://polymarket.com/event/bitcoin-price-in-2027");
  assert.equal(market.updatedAt, "2026-07-26T12:30:00Z");
  assert.deepEqual(market.lifecycle, { active: null, closed: null, resolved: null, acceptingOrders: null });
});

test("preserves explicit Gamma lifecycle fields", () => {
  const market = normalizeMarket({ ...RAW_MARKET, active: false, closed: true, resolved: false, acceptingOrders: false });
  assert.deepEqual(market.lifecycle, { active: false, closed: true, resolved: false, acceptingOrders: false });
});

test("preserves identifiers needed for public market structure analytics", () => {
  const market = normalizeMarket({
    ...RAW_MARKET,
    clobTokenIds: '["yes-token","no-token"]',
    negRisk: false,
    resolutionSource: "https://www.tse.jus.br/eleicoes/resultados"
  });
  assert.equal(market.conditionId, "0xabc");
  assert.deepEqual(market.clobTokenIds, ["yes-token", "no-token"]);
  assert.deepEqual(market.outcomes, ["Yes", "No"]);
  assert.equal(market.marketStructure.openInterest.status, "unavailable");
  assert.equal(market.regionalSources.items[0].publisher, "Tribunal Superior Eleitoral");
});

test("accepts only allowlisted regional resolution URLs", () => {
  const sources = normalizeRegionalSources({
    resolutionSource: "https://resultados.servel.cl/eleccion",
    events: [
      { resolutionSource: "https://servel.cl.evil.test/resultados" },
      { resolutionSource: "javascript:alert(1)" }
    ]
  });
  assert.equal(sources.status, "available");
  assert.equal(sources.items.length, 1);
  assert.equal(sources.items[0].country, "CL");
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

test("keeps absent metrics distinct from real zero values", () => {
  const absent = normalizeMarket({ question: "No observations", outcomes: '["Yes", "No"]', outcomePrices: "[]" });
  const zero = normalizeMarket({
    question: "Observed zero", outcomes: '["Yes", "No"]', outcomePrices: '["0", "1"]',
    volumeNum: 0, volume24hr: 0, liquidityNum: 0, spread: 0
  });
  assert.equal(absent.probability, null);
  assert.equal(absent.volume, null);
  assert.equal(absent.liquidity, null);
  assert.equal(absent.confidence.score, null);
  assert.equal(absent.confidence.level, "unknown");
  assert.equal(absent.gbm.reason, "missing_price");
  assert.equal(absent.signalDossier.status, "insufficient_data");
  assert.equal(zero.probability, 0);
  assert.equal(zero.volume, 0);
  assert.equal(zero.liquidity, 0);
  assert.equal(zero.spread, 0);
});

test("builds a verifiable signal dossier with provenance and explicit triggers", () => {
  const market = normalizeMarket({
    ...RAW_MARKET, oneDayPriceChange: 0.08, spread: 0.06, bestBid: 0.58, bestAsk: 0.64,
    competitive: 0.8
  });
  assert.equal(market.signalDossier.status, "attention");
  assert.deepEqual(market.signalDossier.triggers.map(trigger => trigger.code), ["price_move_24h", "wide_spread"]);
  assert.equal(market.signalDossier.provenance.marketId, "0xabc");
  assert.equal(market.signalDossier.provenance.source, "gamma");
  assert.ok(market.signalDossier.evidence.some(item => item.kind === "model"));
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

test("calculates top-wallet concentration per outcome against open interest", () => {
  const concentration = calculateWalletConcentration([
    { token: "yes-token", holders: [
      { proxyWallet: "0xA", amount: 20 },
      { proxyWallet: "0xa", amount: 5 },
      { proxyWallet: "0xB", amount: 15 },
      { proxyWallet: "0xC", amount: 10 }
    ] },
    { token: "no-token", holders: [{ proxyWallet: "0xD", amount: 30 }] }
  ], 100, { clobTokenIds: ["yes-token", "no-token"], outcomes: ["Yes", "No"], negRisk: false });
  assert.equal(concentration.status, "available");
  assert.equal(concentration.outcomes[0].sampleSize, 3);
  assert.equal(concentration.outcomes[0].top1Share, 0.25);
  assert.equal(concentration.outcomes[0].top5Share, 0.5);
  assert.equal(concentration.marketTop5Share, 0.5);
});

test("does not publish concentration with an invalid denominator or negative-risk market", () => {
  const market = { clobTokenIds: ["yes-token"], outcomes: ["Yes"], negRisk: false };
  assert.equal(calculateWalletConcentration([], 0, market).status, "unavailable");
  assert.equal(calculateWalletConcentration([], 100, { ...market, negRisk: true }).status, "unsupported_market_type");
  assert.equal(calculateWalletConcentration([
    { token: "yes-token", holders: [{ proxyWallet: "0xA", amount: 102 }] }
  ], 100, market).status, "inconsistent");
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

test("enriches markets from batched Polymarket Data API responses", async () => {
  const conditionId = `0x${"a".repeat(64)}`;
  const calls = [];
  const fakeFetch = async url => {
    const href = String(url);
    calls.push(href);
    if (href.startsWith("https://gamma-api.polymarket.com")) {
      return { ok: true, json: async () => [{ ...RAW_MARKET, conditionId, clobTokenIds: '["yes-token","no-token"]' }] };
    }
    if (href.startsWith("https://data-api.polymarket.com/oi")) {
      return { ok: true, json: async () => [{ market: conditionId, value: 100 }] };
    }
    return { ok: true, json: async () => [
      { token: "yes-token", holders: [{ proxyWallet: "0xA", amount: 40 }] },
      { token: "no-token", holders: [{ proxyWallet: "0xB", amount: 25 }] }
    ] };
  };
  const markets = await fetchMarkets(10, 0, fakeFetch);
  assert.equal(calls.length, 3);
  assert.equal(markets.sources.dataApi, "available");
  assert.equal(markets[0].marketStructure.openInterest.value, 100);
  assert.equal(markets[0].marketStructure.walletConcentration.marketTop5Share, 0.4);
  assert.ok(markets[0].signalDossier.evidence.some(item => item.key === "walletConcentration"));
});

test("keeps Gamma markets when supplemental analytics fail", async () => {
  const conditionId = `0x${"b".repeat(64)}`;
  const fakeFetch = async url => {
    if (String(url).startsWith("https://gamma-api.polymarket.com")) {
      return { ok: true, json: async () => [{ ...RAW_MARKET, conditionId, clobTokenIds: '["yes-token","no-token"]' }] };
    }
    throw new Error("analytics offline");
  };
  const markets = await fetchMarkets(10, 0, fakeFetch);
  assert.equal(markets.length, 1);
  assert.equal(markets.sources.dataApi, "unavailable");
  assert.equal(markets[0].marketStructure.openInterest.status, "unavailable");
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

test("local web server dispatches API routes instead of returning static 404s", async () => {
  const json = payload => (request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify(payload));
  };
  const server = createWebServer({
    markets: json({ markets: [] }),
    projection: json({ symbol: "TEST" })
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const { port } = server.address();
    const [home, markets, projection] = await Promise.all([
      fetch(`http://127.0.0.1:${port}/`),
      fetch(`http://127.0.0.1:${port}/api/markets?limit=10&offset=0`),
      fetch(`http://127.0.0.1:${port}/api/projection?symbol=AAPL`)
    ]);
    assert.equal(home.status, 200);
    assert.match(home.headers.get("content-type"), /text\/html/);
    assert.deepEqual(await markets.json(), { markets: [] });
    assert.deepEqual(await projection.json(), { symbol: "TEST" });
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});
