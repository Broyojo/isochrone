# Production Runbook

This app can spend real Mapbox quota, so deploy it with explicit tokens, probes, and cost controls.

## Required Configuration

- `MAPBOX_TOKEN`: server-side token for geocoding, isochrones, and Matrix calls.
- `MAPBOX_PUBLIC_TOKEN`: browser-safe `pk.` token for Mapbox GL JS.
- `REQUIRE_MAPBOX_PUBLIC_TOKEN=true`: keep readiness strict for the web app. Set to `false` only for API-only deployments that do not serve the map UI.
- `HOST=0.0.0.0` and `PORT=8000` for containers or most platform runtimes.

Use a restricted public token for `MAPBOX_PUBLIC_TOKEN`. The app refuses to expose `sk.` tokens to the browser, but
Mapbox token restrictions should still be configured in Mapbox.
Upstream network errors are intentionally reported without token-bearing Mapbox URLs in client errors or logs.
Malformed Mapbox JSON, coordinate, geometry, and Matrix-duration payloads are reported as `502`.

## Cost Controls

- `MAX_REQUEST_BODY_BYTES`: rejects oversized JSON bodies while streaming the request, even if `Content-Length` is missing.
- `MAX_IN_FLIGHT_SEARCHES`: caps concurrent expensive searches per process.
- `MEETING_POINT_TIMEOUT_SECONDS`: caps total wall-clock time for a meeting-point search.
- `MEETING_POINT_RATE_LIMIT`: maximum meeting-point searches per client window.
- `MEETING_POINT_RATE_WINDOW_SECONDS`: rate-limit window size.
- `TRUST_PROXY_HEADERS`: set to `true` only behind a trusted proxy that owns `X-Forwarded-For`.

For multi-instance deployments, use a platform or edge rate limit too. The built-in limiter is per process.

## Probes

- `/health`: liveness probe. It should return `200`.
- `/ready`: readiness probe. It returns `503` until `MAPBOX_TOKEN` and, by default, a public Mapbox token are configured.
- `/metrics`: Prometheus-style counters for HTTP traffic, search outcomes, and Mapbox upstream attempts/retries.

The Docker image also has a `HEALTHCHECK` against `/health`.

## Build And Run

```bash
docker build -t meeting-point-finder .
docker run --rm --env-file .env -p 8000:8000 meeting-point-finder
```

Run the deployment smoke test without making paid Mapbox API calls:

```bash
uv run python scripts/smoke.py \
  --base-url http://127.0.0.1:8000 \
  --expect-public-token \
  --attempts 20 \
  --retry-delay 0.5
```

If checking a partially configured environment before secrets are attached:

```bash
uv run python scripts/smoke.py \
  --base-url http://127.0.0.1:8000 \
  --allow-not-ready \
  --attempts 20 \
  --retry-delay 0.5
```

## Release Checklist

- `uv lock --check`
- `uv run ruff format --check .`
- `uv run ruff check .`
- `uv run ty check`
- `uv run pytest`
- `node --check static/app.js`
- Docker image builds from a clean checkout.
- Container responds on `/health`, `/ready`, `/config.js`, and `/metrics`.
- `scripts/smoke.py` passes against the deployed URL.
- CI runs the app smoke, Docker build, Docker smoke, and Docker healthcheck verification.
- Mapbox public token is restricted for browser use.
- Platform logs preserve `x-request-id` for request correlation.
