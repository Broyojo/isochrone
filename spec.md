## Goal

Find a fair meeting point for a group of friends.

* Each friend provides a starting address and travel mode.
* The app prefers points every friend can reach within `max_minutes`.
* The web app defaults to `objective: "min_sum"` and grid search to minimize total group travel time.
* The web app also offers `objective: "min_max"` for minimizing the slowest friend's ETA.
* If no candidate fits the ETA budget, the API returns `reachable=false` and no meeting point.
* Candidate scoring must batch travel-time checks through Mapbox Matrix instead of making one Directions request per person per candidate.

## API

### `POST /api/meeting-point`

Request:

```json
{
  "addresses": ["addr1", "addr2"],
  "profiles": ["walking", "driving"],
  "max_minutes": 15,
  "objective": "min_max",
  "use_grid_search": true,
  "city_hint": "Atlanta, GA",
  "include_debug": false
}
```

Notes:

* `profile` can be used instead of `profiles` when everyone uses the same mode.
* `profiles`, when present, must match the length of `addresses`.
* Supported profiles are `walking`, `cycling`, `driving`, and `driving-traffic`.
* Address strings, city hints, and custom grid resolutions are bounded before any Mapbox request is made.
* Oversized meeting-point request bodies are rejected before validation or upstream calls, even when `Content-Length` is missing.
* Meeting-point searches have an overall timeout and return `504` if the work exceeds it.
* `include_debug` is opt-in and adds GeoJSON for the intersection and sampled points.
* The API may downsample candidate points when a profile mix would require too many Matrix batches.

Success response:

```json
{
  "reachable": true,
  "meeting_point": {"lat": 33.7765, "lng": -84.3898},
  "participants": [
    {
      "address": "addr1",
      "profile": "walking",
      "lat": 33.77,
      "lng": -84.39,
      "eta_minutes": 11.2
    }
  ],
  "objective": "min_max",
  "objective_value": 11.2,
  "max_minutes": 15
}
```

No feasible response:

```json
{
  "reachable": false,
  "reason": "no_common_reachable_region",
  "objective": "min_max",
  "max_minutes": 15,
  "participants": []
}
```

## Frontend

The first screen is the working app, not a landing page.

* The browser asks for the user's location on load.
* The map starts from the last saved browser location when available, then refreshes the current location in the background.
* The map centers on the browser location and marks it.
* The browser-reported location accuracy is visible in the search area indicator.
* Reverse geocoding fills a hidden city/area hint for better address matching.
* The examples menu loads curated setups and immediately runs the search so routes and markers appear without a second action.
* The friend setup autosaves to `localStorage`.
* Autosave persists the selected example, map viewport, browser location, and last matching result when available.
* Restoring a saved example uses its saved map viewport and does not let background geolocation recenter the map away from that example.
* Loading indicators are visible while examples, location, meeting-point computation, imports, saved results, or route drawing are in progress.
* Users can import app JSON or simple CSV/TXT lists.
* Users can export the current setup as JSON.
* Debug polygons and candidate points are not shown in the main UI.
* The map score overlay is opt-in; when enabled, the frontend requests candidate scores and interpolates them into a continuous green-to-red score surface.
* Grid candidates are sampled across the full shared reachable polygon. Large regions use a coarser grid rather than truncating to the first corner of the bounds.
* The browser receives only a public Mapbox token. Secret `sk.` tokens stay server-side.

## Operations

* `/health` is available for liveness checks.
* `/ready` returns `503` until `MAPBOX_TOKEN` and, by default, a public Mapbox token are configured.
* `/metrics` exposes Prometheus-style HTTP, meeting-point, and Mapbox upstream counters without user-supplied labels.
* Every response includes `x-request-id`; incoming `x-request-id` is echoed when provided.
* Responses include baseline browser security headers without blocking Mapbox rendering.
* Geocode caching is bounded in memory.
* The expensive meeting-point endpoint uses a per-process client rate limit and an in-flight search semaphore to protect upstream cost.
* Mapbox geocoding, isochrone, and Matrix calls use bounded timeouts and retry transient 429/5xx failures.
* Malformed Mapbox JSON, coordinate, geometry, and Matrix-duration payloads return `502` instead of internal errors.
* Mapbox network failures are surfaced without leaking token-bearing upstream URLs to clients or logs.
* Matrix request batching chooses chunk sizes by estimated total request count, then caps candidate density if the request budget would be exceeded.
* `config.js` uses no-store cache headers and never serializes a secret `sk.` token into the browser.
* Runtime configuration comes from environment variables:
  * `MAPBOX_TOKEN` is required for server-side Mapbox calls.
  * `MAPBOX_PUBLIC_TOKEN` is preferred for browser Mapbox GL.
  * `REQUIRE_MAPBOX_PUBLIC_TOKEN=false` is reserved for API-only deployments that do not serve the map UI.
  * `HOST`, `PORT`, and `RELOAD` configure `uv run python main.py`.
* `MAX_REQUEST_BODY_BYTES`, `MAX_IN_FLIGHT_SEARCHES`, `MEETING_POINT_TIMEOUT_SECONDS`, `MEETING_POINT_RATE_LIMIT`, and `MEETING_POINT_RATE_WINDOW_SECONDS` tune cost controls.
  * `TRUST_PROXY_HEADERS` should be enabled only behind a trusted proxy that controls `X-Forwarded-For`.
* The Dockerfile runs the FastAPI app with `uv`, binds to port 8000, and uses a non-root user.
* The Docker image healthcheck uses `/health`.
* `scripts/smoke.py` verifies a running deployment without making paid Mapbox API calls.
* GitHub Actions checks the lockfile, format, lint, type, tests, frontend syntax, app smoke, Docker build, Docker smoke, and Docker healthcheck.
* `docs/production.md` contains the release checklist, probe behavior, token guidance, and cost-control settings.

## Verification

Required gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
node --check static/app.js
uv lock --check
uv run python scripts/smoke.py --base-url http://127.0.0.1:8000 --expect-public-token --attempts 20 --retry-delay 0.5
```
