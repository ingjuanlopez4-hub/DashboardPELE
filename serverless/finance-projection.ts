"use strict";

const YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart";
const TRADING_DAYS_PER_YEAR = 252;
const REQUEST_TIMEOUT_MS = 8_000;

export interface ProjectionParameters {
  symbol: string;
  horizonDays: number;
  paths: number;
  targetPrice?: number;
}

export interface PricePoint {
  date: string;
  close: number;
}

export interface ProjectionResult {
  symbol: string;
  source: "yahoo_finance";
  currency: string | null;
  exchange: string | null;
  currentPrice: number;
  dataAsOf: string;
  history: PricePoint[];
  parameters: { horizonDays: number; paths: number; targetPrice: number };
  calibration: { observations: number; annualDrift: number; annualVolatility: number };
  distribution: {
    mean: number;
    p05: number;
    p25: number;
    median: number;
    p75: number;
    p95: number;
    histogram: Array<{ from: number; to: number; probability: number }>;
  };
  probabilities: { gain: number; aboveTarget: number };
}

export class FinanceDataError extends Error {
  constructor(message: string, public readonly code: "symbol_not_found" | "upstream_unavailable" = "upstream_unavailable", options?: ErrorOptions) {
    super(message, options);
    this.name = "FinanceDataError";
  }
}

function finite(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function round(value: number, digits = 4): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function percentile(sorted: number[], probability: number): number {
  const position = (sorted.length - 1) * probability;
  const lower = Math.floor(position);
  const fraction = position - lower;
  return sorted[lower + 1] === undefined
    ? sorted[lower]
    : sorted[lower] + fraction * (sorted[lower + 1] - sorted[lower]);
}

function seededRandom(seedValue: string): () => number {
  let seed = 2166136261;
  for (const character of seedValue) {
    seed ^= character.charCodeAt(0);
    seed = Math.imul(seed, 16777619);
  }
  return () => {
    seed += 0x6d2b79f5;
    let value = seed;
    value = Math.imul(value ^ value >>> 15, value | 1);
    value ^= value + Math.imul(value ^ value >>> 7, value | 61);
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
}

function histogram(values: number[], bins = 20): Array<{ from: number; to: number; probability: number }> {
  const minimum = values[0];
  const maximum = values.at(-1) ?? minimum;
  const width = Math.max((maximum - minimum) / bins, Math.max(minimum, 1) * 1e-6);
  const counts = Array.from({ length: bins }, () => 0);
  for (const value of values) counts[Math.min(Math.floor((value - minimum) / width), bins - 1)] += 1;
  return counts.map((count, index) => ({
    from: round(minimum + index * width, 2),
    to: round(index === bins - 1 ? maximum : minimum + (index + 1) * width, 2),
    probability: round(count / values.length, 6)
  }));
}

export function runGbm(currentPrice: number, closes: number[], parameters: ProjectionParameters, seed: string): Omit<ProjectionResult, "symbol" | "source" | "currency" | "exchange" | "currentPrice" | "dataAsOf" | "history"> {
  if (closes.length < 20) throw new FinanceDataError("Not enough historical observations", "symbol_not_found");
  const returns: number[] = [];
  for (let index = 1; index < closes.length; index += 1) {
    const value = Math.log(closes[index] / closes[index - 1]);
    if (Number.isFinite(value)) returns.push(value);
  }
  if (returns.length < 19) throw new FinanceDataError("Not enough valid historical observations", "symbol_not_found");

  const dailyMean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance = returns.reduce((sum, value) => sum + (value - dailyMean) ** 2, 0) / Math.max(returns.length - 1, 1);
  const annualVariance = variance * TRADING_DAYS_PER_YEAR;
  const annualVolatility = Math.sqrt(annualVariance);
  const annualDrift = dailyMean * TRADING_DAYS_PER_YEAR + 0.5 * annualVariance;
  if (!Number.isFinite(annualVolatility) || annualVolatility <= 0) {
    throw new FinanceDataError("Historical prices do not contain usable volatility", "symbol_not_found");
  }

  const horizonYears = parameters.horizonDays / TRADING_DAYS_PER_YEAR;
  const targetPrice = parameters.targetPrice ?? currentPrice;
  const random = seededRandom(seed);
  const outcomes: number[] = [];
  let total = 0;
  let gains = 0;
  let aboveTarget = 0;
  for (let index = 0; index < parameters.paths; index += 1) {
    const first = Math.max(random(), Number.EPSILON);
    const normal = Math.sqrt(-2 * Math.log(first)) * Math.cos(2 * Math.PI * random());
    const terminal = currentPrice * Math.exp(
      (annualDrift - 0.5 * annualVolatility ** 2) * horizonYears
      + annualVolatility * Math.sqrt(horizonYears) * normal
    );
    outcomes.push(terminal);
    total += terminal;
    if (terminal > currentPrice) gains += 1;
    if (terminal >= targetPrice) aboveTarget += 1;
  }
  outcomes.sort((left, right) => left - right);

  return {
    parameters: { horizonDays: parameters.horizonDays, paths: parameters.paths, targetPrice: round(targetPrice, 4) },
    calibration: { observations: closes.length, annualDrift: round(annualDrift), annualVolatility: round(annualVolatility) },
    distribution: {
      mean: round(total / parameters.paths, 2),
      p05: round(percentile(outcomes, 0.05), 2),
      p25: round(percentile(outcomes, 0.25), 2),
      median: round(percentile(outcomes, 0.5), 2),
      p75: round(percentile(outcomes, 0.75), 2),
      p95: round(percentile(outcomes, 0.95), 2),
      histogram: histogram(outcomes)
    },
    probabilities: { gain: round(gains / parameters.paths, 6), aboveTarget: round(aboveTarget / parameters.paths, 6) }
  };
}

type FetchLike = (input: string | URL, init?: RequestInit) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export async function fetchFinanceProjection(parameters: ProjectionParameters, fetchImpl: FetchLike = globalThis.fetch): Promise<ProjectionResult> {
  const url = new URL(`${YAHOO_CHART_URL}/${encodeURIComponent(parameters.symbol)}`);
  url.search = new URLSearchParams({ interval: "1d", range: "1y", events: "history" }).toString();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetchImpl(url, {
      headers: { Accept: "application/json", "User-Agent": "PELE-dashboard/1.0" },
      signal: controller.signal
    });
    if (!response.ok) {
      throw new FinanceDataError("Yahoo Finance did not return this symbol", response.status === 404 ? "symbol_not_found" : "upstream_unavailable");
    }
    const payload = await response.json() as any;
    const chart = payload?.chart;
    if (chart?.error) throw new FinanceDataError("Yahoo Finance rejected this symbol", "symbol_not_found");
    const result = chart?.result?.[0];
    const timestamps: unknown[] = Array.isArray(result?.timestamp) ? result.timestamp : [];
    const rawCloses: unknown[] = Array.isArray(result?.indicators?.quote?.[0]?.close) ? result.indicators.quote[0].close : [];
    const history: PricePoint[] = [];
    for (let index = 0; index < Math.min(timestamps.length, rawCloses.length); index += 1) {
      const timestamp = Number(timestamps[index]);
      const close = finite(rawCloses[index]);
      if (Number.isFinite(timestamp) && close !== null) history.push({ date: new Date(timestamp * 1000).toISOString(), close: round(close, 4) });
    }
    if (history.length < 20) throw new FinanceDataError("Yahoo Finance has insufficient history for this symbol", "symbol_not_found");
    const currentPrice = finite(result?.meta?.regularMarketPrice) ?? history.at(-1)!.close;
    const marketTimestamp = Number(result?.meta?.regularMarketTime);
    const dataAsOf = Number.isFinite(marketTimestamp)
      ? new Date(marketTimestamp * 1000).toISOString()
      : history.at(-1)!.date;
    const model = runGbm(currentPrice, history.map(point => point.close), parameters, `${parameters.symbol}:${dataAsOf}:${parameters.horizonDays}:${parameters.paths}:${parameters.targetPrice ?? "current"}`);
    return {
      symbol: parameters.symbol,
      source: "yahoo_finance",
      currency: typeof result?.meta?.currency === "string" ? result.meta.currency : null,
      exchange: typeof result?.meta?.exchangeName === "string" ? result.meta.exchangeName : null,
      currentPrice: round(currentPrice, 4),
      dataAsOf,
      history,
      ...model
    };
  } catch (error) {
    if (error instanceof FinanceDataError) throw error;
    throw new FinanceDataError("Yahoo Finance is temporarily unavailable", "upstream_unavailable", { cause: error });
  } finally {
    clearTimeout(timeout);
  }
}
