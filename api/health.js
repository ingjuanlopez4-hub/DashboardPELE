"use strict";

module.exports = function healthHandler(request, response) {
  if (request.method && request.method !== "GET") {
    response.statusCode = 405;
    response.setHeader("Allow", "GET");
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    return response.end(JSON.stringify({ error: { code: "method_not_allowed", message: "Method not allowed" } }));
  }
  response.statusCode = 200;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  return response.end(JSON.stringify({
    status: "ok",
    service: "pele-dashboard",
    time: new Date().toISOString()
  }));
};
