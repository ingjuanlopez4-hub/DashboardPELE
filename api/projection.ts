"use strict";

import { FinanceDataError, ProjectionParameters, fetchFinanceProjection } from "../serverless/finance-projection";

const SYMBOL_PATTERN = /^[A-Z0-9.^=-]{1,15}$/;

interface RequestLike { method?: string; url?: string }
interface ResponseLike {
  statusCode: number;
  setHeader(name: string, value: string): void;
  end(body: string): void;
}

export function parseProjectionParameters(requestUrl = "/api/projection"): ProjectionParameters {
  const params = new URL(requestUrl, "http://localhost").searchParams;
  const symbol = (params.get("symbol") || "").trim().toUpperCase();
  if (!SYMBOL_PATTERN.test(symbol)) throw new RangeError("symbol must contain 1-15 valid ticker characters");

  const integer = (name: string, fallback: number, minimum: number, maximum: number): number => {
    const raw = params.get(name) ?? String(fallback);
    if (!/^\d+$/.test(raw)) throw new RangeError(`${name} must be an integer`);
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new RangeError(`${name} must be between ${minimum} and ${maximum}`);
    return value;
  };
  const horizonDays = integer("horizonDays", 30, 1, 365);
  const paths = integer("paths", 5_000, 500, 10_000);
  const targetValue = params.get("targetPrice");
  let targetPrice: number | undefined;
  if (targetValue !== null && targetValue !== "") {
    targetPrice = Number(targetValue);
    if (!Number.isFinite(targetPrice) || targetPrice <= 0 || targetPrice > 10_000_000) throw new RangeError("targetPrice must be a positive number no greater than 10000000");
  }
  return { symbol, horizonDays, paths, targetPrice };
}

function send(response: ResponseLike, status: number, payload: unknown): void {
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Cache-Control", status === 200 ? "s-maxage=300, stale-while-revalidate=300" : "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  response.end(JSON.stringify(payload));
}

export default async function projectionHandler(request: RequestLike, response: ResponseLike): Promise<void> {
  if (request.method && request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return send(response, 405, { error: { code: "method_not_allowed", message: "Method not allowed" } });
  }
  try {
    const parameters = parseProjectionParameters(request.url);
    return send(response, 200, await fetchFinanceProjection(parameters));
  } catch (error) {
    if (error instanceof RangeError) return send(response, 400, { error: { code: "invalid_request", message: error.message } });
    if (error instanceof FinanceDataError) {
      const status = error.code === "symbol_not_found" ? 404 : 502;
      const message = status === 404 ? "No usable market history was found for this symbol" : "Market data is temporarily unavailable";
      return send(response, status, { error: { code: error.code, message } });
    }
    console.error("Unexpected projection handler error", error);
    return send(response, 500, { error: { code: "internal_error", message: "Internal server error" } });
  }
}
