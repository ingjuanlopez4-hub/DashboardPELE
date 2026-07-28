"use strict";

const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const WEB_ROOT = path.join(ROOT, "web");
const CONTENT_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml"
};

function resolveHandlers() {
  const markets = require(path.join(ROOT, "api", "markets.js"));
  const projectionModule = require(path.join(ROOT, ".test-dist", "api", "projection.js"));
  return { markets, projection: projectionModule.default };
}

function safeStaticPath(pathname) {
  if (pathname === "/") return path.join(WEB_ROOT, "index.html");
  if (!pathname.startsWith("/assets/")) return null;
  const relative = pathname.slice(1);
  const candidate = path.resolve(WEB_ROOT, relative);
  return candidate.startsWith(`${WEB_ROOT}${path.sep}`) ? candidate : null;
}

function createWebServer(handlers = resolveHandlers()) {
  return http.createServer(async (request, response) => {
    const pathname = new URL(request.url || "/", "http://localhost").pathname;
    try {
      if (pathname === "/api/markets") return await handlers.markets(request, response);
      if (pathname === "/api/projection") return await handlers.projection(request, response);
      if (pathname === "/api/health") return require(path.join(ROOT, "api", "health.js"))(request, response);

      const filename = safeStaticPath(pathname);
      if (!filename || !["GET", "HEAD"].includes(request.method || "GET")) {
        response.writeHead(404, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
        return response.end(JSON.stringify({ error: { code: "not_found", message: "Route not found" } }));
      }
      const body = await fs.readFile(filename);
      response.writeHead(200, {
        "Content-Type": CONTENT_TYPES[path.extname(filename)] || "application/octet-stream",
        "Cache-Control": pathname.startsWith("/assets/") ? "public, max-age=3600" : "no-cache",
        "X-Content-Type-Options": "nosniff"
      });
      return response.end(request.method === "HEAD" ? undefined : body);
    } catch (error) {
      if (error?.code === "ENOENT") {
        response.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
        return response.end(JSON.stringify({ error: { code: "not_found", message: "Route not found" } }));
      }
      console.error("Local web server error", error);
      if (!response.headersSent) response.writeHead(500, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
      return response.end(JSON.stringify({ error: { code: "internal_error", message: "Internal server error" } }));
    }
  });
}

if (require.main === module) {
  const port = Number(process.env.PORT || 3000);
  createWebServer().listen(port, "127.0.0.1", () => {
    console.log(`PELE web surveillance running at http://127.0.0.1:${port}`);
  });
}

module.exports = { createWebServer, safeStaticPath };
