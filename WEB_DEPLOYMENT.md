# PELE web dashboard on Vercel

This deployment is intentionally separate from the trading runtime. Vercel hosts a
static dashboard and short-lived Node.js functions for public analysis only.

## Architecture

- `web/`: static HTML, CSS, and JavaScript. There is no build step.
- `api/markets.js`: paginated Node.js serverless proxy to the public Gamma API. It
  normalizes the payload and caches each page at Vercel's edge for 60 seconds.
- `serverless/market-data.js`: dependency-free Gamma client and response normalization.
- `api/health.js`: lightweight Node.js liveness endpoint.
- `api/projection.ts`: validated Yahoo Finance proxy and TypeScript GBM endpoint for
  symbol projections. Responses are edge-cached for five minutes.
- `serverless/finance-projection.ts`: credential-free Yahoo historical-price client,
  annualized calibration, seeded simulation, histogram and threshold probabilities.

No wallet key, Polymarket credential, `.env` value, SQLite file, order endpoint, or
trading module is imported by the web application. The serverless functions are
Node.js modules with no package dependencies, so Vercel does not install the heavy
Python trading requirements for these functions.

## Probability confidence model

Gamma is a free, public Polymarket API and requires no API key. For every market,
the server calculates a 0-100 market-confidence score from liquidity (25%), 24-hour
volume (25%), bid/ask spread (25%), 24-hour price stability (15%), and order-book
competitiveness (10%). Missing optional factors are excluded and the score reports
its data coverage. This measures the quality of the price-discovery signal; it is not
a guarantee that the predicted event will occur.

The API also returns an `intelligence` object derived only from Gamma's observed
one-hour, one-day and one-week price changes, current liquidity, 24-hour volume,
bid/ask and spread. Missing upstream metrics remain `null`; the server does not
fabricate history, holder concentration, external forecasts, or expected value.

For markets with more than seven days until expiry, the server runs 1,000 seeded
Geometric Brownian Motion paths in log-odds space. Volatility is calibrated from
Gamma's observed price changes and spread when available, otherwise the response
marks the 0.50 annualized volatility as an explicit assumption. The API returns the
P5, median, mean and P95 projection under `market.gbm`; these are model outputs, not
observed prices or guaranteed forecasts.

## Deploy

1. Import the repository in Vercel or install the CLI with `npm i -g vercel`.
2. Set the project root to the repository root. Framework preset: `Other`.
3. Do not add trading secrets to this Vercel project; none are required.
4. Run `vercel` for a preview and `vercel --prod` for production.
5. Verify `/`, `/api/health`, and `/api/markets?limit=20`.
6. Verify `/api/projection?symbol=AAPL&horizonDays=30&paths=5000`.

Local static preview:

```bash
python -m http.server 8000
# Open http://localhost:8000/web/ (API calls need `vercel dev` for full routing.)
```

For a production-equivalent local environment, install the Vercel CLI and run
`vercel dev` from the repository root.

## Validation

```bash
node --test tests/web-api.test.js
node --check serverless/market-data.js
node --check api/markets.js
node --check api/health.js
node --check web/assets/app.js
npm run test:web
python -m json.tool vercel.json >/dev/null
```

## Vercel limits and operations

Vercel functions are request-driven and ephemeral. They must not host PELE's live
trading loop, WebSocket connections, background workers, in-memory state, or SQLite
database. Cold starts and execution time limits apply, and the filesystem is not
durable. Keep live trading on an always-on process (VM, container service, or worker)
with persistent storage and independent monitoring.

The dashboard follows Gamma pagination in batches of 10 as the user requests more
markets. Each serverless invocation makes one
bounded Gamma request with a 10-second timeout, avoiding oversized Vercel responses.
If Gamma fails during pagination, the browser keeps and clearly labels any markets
already recovered. If the first page fails, it shows an explicit unavailable state
instead of substituting fictional markets.

The health endpoint proves only that the dashboard function can execute. It does not
report trading-bot health. Configure external uptime monitoring separately for both
services. Review current function duration, bandwidth, and invocation quotas in the
selected Vercel plan before increasing the sample size or refresh frequency.

The browser polls the last valid symbol projection every five minutes only while the
tab is visible. Yahoo Finance's chart endpoint requires no application credential,
but it is an external, unofficial integration and can change or rate-limit requests;
the dashboard preserves the last successful result and exposes failures explicitly.
