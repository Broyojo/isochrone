# Isochrone meeting point MVP

FastAPI service that takes a list of addresses and returns a fair meeting point based on Mapbox isochrones and directions.

## Prerequisites
- `uv` installed (already used for dependency management).
- Mapbox access token in `MAPBOX_TOKEN`.

## Setup
```bash
uv sync  # ensure the env matches uv.lock
```

## Run the dev server
```bash
MAPBOX_TOKEN=sk.your-token-here uv run uvicorn main:app --reload --port 8000
```

## Run tests
```bash
uv run pytest
```

## Example request
```bash
curl -X POST http://localhost:8000/api/meeting-point \
  -H "Content-Type: application/json" \
  -d '{
    "addresses": ["123 Peachtree St NE", "555 Marietta St NW"],
    "city_hint": "Atlanta, GA",
    "max_minutes": 15,
    "objective": "min_sum",
    "profile": "walking"
  }'
```

## Behavior
- Addresses are deduplicated (case-insensitive) and capped at 10.
- Profile currently supports `walking` and `driving`.
- If the intersection of isochrones is empty, the service retries once with `max_minutes + 5` (up to 60). If still empty, `reachable=false` is returned.
- Meeting point is the centroid of the largest reachable region; per-participant ETAs come from Mapbox Directions.
- Debug payload includes the intersection polygon as GeoJSON; grid search is not implemented in this MVP.
