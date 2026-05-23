# Meeting Point Finder

FastAPI + Mapbox app for finding a fair meeting point for friends. Each friend can use a different travel mode, and the app chooses a balanced spot where everyone fits inside the selected per-person ETA budget.

Candidate scoring uses the Mapbox Matrix API in batches, so a multi-person search checks many candidate points with a small number of upstream requests instead of one Directions request per friend per candidate.

## Prerequisites
- `uv`
- Mapbox token in `MAPBOX_TOKEN` for server-side geocoding, isochrones, and Matrix ETA scoring
- Browser-safe Mapbox public token in `MAPBOX_PUBLIC_TOKEN` for Mapbox GL JS

## Setup
```bash
uv sync
cp .env.example .env
```

Export the token before running, or source it from your shell environment.

## Run
```bash
MAPBOX_TOKEN=sk.your-token-here MAPBOX_PUBLIC_TOKEN=pk.your-public-token-here uv run uvicorn main:app --reload --port 8000
```

The app is served at `http://127.0.0.1:8000`.

For a deployed service, run without reload and bind to the platform port:

```bash
HOST=0.0.0.0 PORT=8000 uv run python main.py
```

Or build the container:

```bash
docker build -t meeting-point-finder .
docker run --env-file .env -p 8000:8000 meeting-point-finder
```

## Checks
```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
node --check static/app.js
uv lock --check
```

## Production

See [docs/production.md](docs/production.md) for deployment settings, probes, cost controls, and the release checklist.

Smoke test a running app without making paid Mapbox API calls:

```bash
uv run python scripts/smoke.py \
  --base-url http://127.0.0.1:8000 \
  --expect-public-token \
  --attempts 20 \
  --retry-delay 0.5
```

## Example Request
```bash
curl -X POST http://localhost:8000/api/meeting-point \
  -H "Content-Type: application/json" \
  -d '{
    "addresses": ["123 Peachtree St NE", "555 Marietta St NW"],
    "profiles": ["walking", "driving"],
    "city_hint": "Atlanta, GA",
    "max_minutes": 15,
    "objective": "min_max",
    "use_grid_search": true
  }'
```

The same import/export shape is available in `examples/bay-area-driving.json`.

## Behavior
- `max_minutes` is a strict per-friend ETA budget.
- Supported modes are `walking`, `cycling`, `driving`, and `driving-traffic`.
- Send `profile` for one mode for everyone, or `profiles` aligned with `addresses`.
- The default goal is `min_sum`, shown as "Lowest total time" in the UI. `min_max`, shown as "Fairest split", is available when minimizing the slowest friend's ETA matters more.
- Candidate ETA scoring is batched by travel mode with the Matrix API. The search optimizes Matrix chunk sizes and caps request count to keep normal use deployable and rate-limit friendly.
- Grid search samples across the full shared reachable area, then adapts the sample density down for large regions instead of scanning only one corner.
- Mapbox geocoding, isochrone, and Matrix calls use bounded timeouts and small retries for transient upstream failures.
- Malformed Mapbox JSON, coordinate, geometry, and Matrix-duration responses are converted to clean `502` responses.
- Mapbox upstream network failures are reported without echoing token-bearing URLs into client errors or logs.
- Responses include an `x-request-id` header. Pass one in to correlate client reports with server logs, or the server generates one.
- Geocoding results are cached in a bounded in-memory LRU cache to reduce repeat Mapbox calls without unbounded memory growth.
- The map Scores toggle requests candidate score data for the current goal and interpolates it into a continuous green-to-red score surface over the shared reachable polygon.
- The web UI asks for browser location, centers the map there, reverse-geocodes the area, and uses that area as a hidden geocoding hint.
- The last browser location is saved locally, so refreshes start from the last known area immediately and then update in the background.
- Browser-reported location accuracy is shown beside the area.
- The Examples menu loads curated setups from `static/examples.json` and immediately computes the meeting point, markers, and routes.
- Each example includes a map view, so loading or restoring an example starts in the right area before the route results finish.
- The web UI autosaves the latest setup in browser local storage.
- Autosave includes the selected example, map viewport, browser location, and last matching result so refreshes can resume where you left off.
- Loading indicators appear on the map and primary action while the app loads examples, asks for location, computes a meeting point, restores a result, imports a setup, or draws routes.
- Import accepts exported app JSON or simple CSV/TXT lists. CSV headers like `address`, `location`, `place`, `name`, `title`, `profile`, and `mode` are recognized.
- Export downloads a reusable `meeting-point-setup.json`.
- Debug GeoJSON is omitted by default. Send `include_debug: true` to include the reachable intersection and sampled candidate points.
- Address strings, city hints, and custom grid resolutions have bounded validation so a bad request cannot trigger unbounded upstream or geometry work.
- The expensive meeting-point endpoint has an enforced streaming request body limit, per-client rate limit, total timeout, and in-process concurrency cap to keep Mapbox spend and latency bounded.
- `/health` is a liveness probe. `/ready` is a readiness probe that returns `503` until `MAPBOX_TOKEN` and, by default, a public Mapbox token are configured.
- `/metrics` exposes low-cardinality Prometheus-style counters for HTTP traffic, meeting-point outcomes, and Mapbox upstream attempts/retries.
- Browser responses include baseline security headers for content sniffing, framing, referrers, and geolocation policy.
- `config.js` is served with `Cache-Control: no-store` and never exposes a secret `sk.` Mapbox token. It serves `MAPBOX_PUBLIC_TOKEN`, or `MAPBOX_TOKEN` only if that token is a public `pk.` token. Set `REQUIRE_MAPBOX_PUBLIC_TOKEN=false` only for API-only deployments that do not serve the map UI.
- `.github/workflows/ci.yml` runs the core checks, app smoke, Docker build, Docker smoke, and Docker healthcheck verification on push and pull request.
- The Docker image uses the frozen `uv.lock`, binds to `0.0.0.0:8000`, runs the app as a non-root user, and includes a `/health` healthcheck.
