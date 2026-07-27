"use strict";

const {
  DEFAULT_LIMIT,
  MAX_LIMIT,
  MarketDataError,
  fetchMarkets
} = require("../serverless/market-data");

function parsePagination(requestUrl = "/api/markets") {
  const params = new URL(requestUrl, "http://localhost").searchParams;
  const limitValue = params.get("limit") ?? String(DEFAULT_LIMIT);
  const offsetValue = params.get("offset") ?? "0";
  if (!/^\d+$/.test(limitValue)) throw new RangeError("limit must be an integer");
  if (!/^\d+$/.test(offsetValue)) throw new RangeError("offset must be a non-negative integer");
  const limit = Number(limitValue);
  const offset = Number(offsetValue);
  if (limit < 1 || limit > MAX_LIMIT) {
    throw new RangeError(`limit must be between 1 and ${MAX_LIMIT}`);
  }
  if (!Number.isSafeInteger(offset)) throw new RangeError("offset is too large");
  return { limit, offset };
}

function send(response, status, payload) {
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Cache-Control", "s-maxage=60, stale-while-revalidate=300");
  response.setHeader("X-Content-Type-Options", "nosniff");
  response.end(JSON.stringify(payload));
}

async function marketsHandler(request, response) {
  if (request.method && request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return send(response, 405, { error: { code: "method_not_allowed", message: "Method not allowed" } });
  }
  try {
    const { limit, offset } = parsePagination(request.url);
    const markets = await fetchMarkets(limit, offset);
    const hasMore = markets.length === limit;
    const dataAsOf = markets.map(market => market.updatedAt).filter(Boolean).sort().at(-1) || null;
    return send(response, 200, {
      markets,
      count: markets.length,
      source: "gamma",
      dataAsOf,
      offset,
      hasMore,
      nextOffset: hasMore ? offset + markets.length : null
    });
  } catch (error) {
    if (error instanceof RangeError) {
      return send(response, 400, { error: { code: "invalid_request", message: error.message } });
    }
    if (error instanceof MarketDataError) {
      return send(response, 502, {
        error: {
          code: "upstream_unavailable",
          message: "Live market data is temporarily unavailable"
        }
      });
    }
    console.error("Unexpected markets handler error", error);
    return send(response, 500, { error: { code: "internal_error", message: "Internal server error" } });
  }
}

module.exports = marketsHandler;
module.exports.parsePagination = parsePagination;
