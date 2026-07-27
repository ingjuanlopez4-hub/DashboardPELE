import test from "node:test";
import assert from "node:assert/strict";
import { parseProjectionParameters } from "../api/projection";
import { fetchFinanceProjection, runGbm } from "../serverless/finance-projection";

test("validates and normalizes projection query parameters", () => {
  assert.deepEqual(parseProjectionParameters("/api/projection?symbol=btc-usd&horizonDays=60&paths=1000&targetPrice=90000"), {
    symbol: "BTC-USD", horizonDays: 60, paths: 1000, targetPrice: 90000
  });
  for (const url of [
    "/api/projection", "/api/projection?symbol=AAPL!", "/api/projection?symbol=AAPL&horizonDays=0",
    "/api/projection?symbol=AAPL&paths=200", "/api/projection?symbol=AAPL&targetPrice=-1"
  ]) assert.throws(() => parseProjectionParameters(url), RangeError);
});

test("GBM produces a deterministic bounded distribution", () => {
  const closes = Array.from({ length: 80 }, (_, index) => 100 * Math.exp(index * 0.0008 + Math.sin(index / 3) * 0.02));
  const parameters = { symbol: "TEST", horizonDays: 30, paths: 1000, targetPrice: 110 };
  const first = runGbm(108, closes, parameters, "stable-seed");
  const second = runGbm(108, closes, parameters, "stable-seed");
  assert.deepEqual(first, second);
  assert.equal(first.distribution.histogram.length, 20);
  assert.ok(first.distribution.p05 < first.distribution.median);
  assert.ok(first.distribution.median < first.distribution.p95);
  assert.ok(first.probabilities.gain >= 0 && first.probabilities.gain <= 1);
  assert.ok(Math.abs(first.distribution.histogram.reduce((sum, bin) => sum + bin.probability, 0) - 1) < 0.00001);
  const returns = closes.slice(1).map((close, index) => Math.log(close / closes[index]));
  const dailyMean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance = returns.reduce((sum, value) => sum + (value - dailyMean) ** 2, 0) / (returns.length - 1);
  assert.ok(Math.abs(first.calibration.annualDrift - (dailyMean * 252 + variance * 252 / 2)) < 0.0001);
});

test("maps Yahoo history and current price without network access", async () => {
  const timestamps = Array.from({ length: 30 }, (_, index) => 1_700_000_000 + index * 86_400);
  const closes = timestamps.map((_, index) => 100 + index + Math.sin(index));
  const fakeFetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ chart: { result: [{
      timestamp: timestamps,
      meta: { regularMarketPrice: 132.5, regularMarketTime: timestamps.at(-1), currency: "USD", exchangeName: "NMS" },
      indicators: { quote: [{ close: closes }] }
    }], error: null } })
  });
  const result = await fetchFinanceProjection({ symbol: "AAPL", horizonDays: 20, paths: 500 }, fakeFetch);
  assert.equal(result.source, "yahoo_finance");
  assert.equal(result.currentPrice, 132.5);
  assert.equal(result.history.length, 30);
  assert.equal(result.calibration.observations, 30);
});
