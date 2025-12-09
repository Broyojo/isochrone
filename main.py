from __future__ import annotations

import asyncio
import os
import urllib.parse
from contextlib import asynccontextmanager
from functools import reduce
from typing import Dict, List, Optional, Sequence, Tuple

import httpx
from fastapi import Body, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape, mapping

# Configuration constants
MAX_PARTICIPANTS = 10
DEFAULT_MAX_MINUTES = 15
MAX_MAX_MINUTES = 60
SUPPORTED_OBJECTIVES = {"min_sum", "min_max"}
SUPPORTED_PROFILES = {"walking"}

# Simple in-memory cache for geocoding results
_geocode_cache: Dict[str, Tuple[float, float]] = {}


def _mapbox_token() -> str:
    token = os.getenv("MAPBOX_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MAPBOX_TOKEN environment variable is required",
        )
    return token


class MeetingPointRequest(BaseModel):
    addresses: List[str] = Field(..., min_length=1)
    profile: str = Field("walking")
    max_minutes: int = Field(DEFAULT_MAX_MINUTES, ge=5, le=MAX_MAX_MINUTES)
    objective: str = Field("min_sum")
    grid_resolution_m: Optional[float] = Field(None, gt=0)
    city_hint: Optional[str] = None

    @field_validator("addresses")
    @classmethod
    def validate_addresses(cls, value: List[str]) -> List[str]:
        cleaned = [addr.strip() for addr in value if addr.strip()]
        if not cleaned:
            raise ValueError("addresses cannot be empty")
        if len(cleaned) > MAX_PARTICIPANTS:
            raise ValueError(f"maximum {MAX_PARTICIPANTS} addresses are supported")
        return cleaned

    @field_validator("profile")
    @classmethod
    def normalize_profile(cls, value: str) -> str:
        profile = value.lower()
        if profile.startswith("mapbox/"):
            profile = profile.split("/", 1)[1]
        if profile not in SUPPORTED_PROFILES:
            raise ValueError(f"profile must be one of: {', '.join(sorted(SUPPORTED_PROFILES))}")
        return profile

    @field_validator("objective")
    @classmethod
    def normalize_objective(cls, value: str) -> str:
        objective = value.lower()
        if objective not in SUPPORTED_OBJECTIVES:
            raise ValueError(f"objective must be one of: {', '.join(sorted(SUPPORTED_OBJECTIVES))}")
        return objective


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(timeout=20)
    app.state.http_client = client
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="Isochrone Meeting Point API",
    version="0.1.0",
    description="Compute a fair meeting point reachable within a walking time budget.",
    lifespan=lifespan,
)


def _deduplicate_addresses(addresses: Sequence[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for addr in addresses:
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(addr)
    return deduped


async def _geocode_address(
    client: httpx.AsyncClient, address: str, city_hint: Optional[str], token: str
) -> Tuple[float, float]:
    """Return (lng, lat) for a single address using Mapbox Geocoding."""
    cache_key = f"{address}|{city_hint}" if city_hint else address
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    query = f"{address}, {city_hint}" if city_hint else address
    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{urllib.parse.quote(query)}.json"
    params = {"access_token": token, "limit": 1, "autocomplete": False}

    try:
        resp = await client.get(url, params=params)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Geocoding failed for '{address}': {exc}",
        ) from exc

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Mapbox token")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Mapbox rate limit exceeded")
    if resp.status_code >= 500:
        raise HTTPException(status_code=502, detail="Mapbox geocoding service error")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"Geocoding error for '{address}'")

    data = resp.json()
    features = data.get("features") or []
    if not features:
        raise HTTPException(status_code=400, detail=f"Address not found: '{address}'")

    coords = features[0]["geometry"]["coordinates"]
    lng, lat = float(coords[0]), float(coords[1])
    _geocode_cache[cache_key] = (lng, lat)
    return lng, lat


async def _fetch_isochrone(
    client: httpx.AsyncClient,
    coordinate: Tuple[float, float],
    minutes: int,
    profile: str,
    token: str,
):
    lng, lat = coordinate
    url = f"https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
    params = {"contours_minutes": minutes, "polygons": "true", "access_token": token}

    try:
        resp = await client.get(url, params=params)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Isochrone request failed for coordinate {coordinate}: {exc}",
        ) from exc

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Mapbox token")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Mapbox rate limit exceeded")
    if resp.status_code >= 500:
        raise HTTPException(status_code=502, detail="Mapbox isochrone service error")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="Isochrone request rejected by Mapbox")

    data = resp.json()
    features = data.get("features") or []
    if not features:
        raise HTTPException(status_code=502, detail="Isochrone response contained no geometry")

    geom = features[0].get("geometry")
    polygon = shape(geom)
    if polygon.is_empty:
        raise HTTPException(status_code=502, detail="Isochrone geometry was empty")
    return polygon


def _intersection(polygons: Sequence[Polygon | MultiPolygon]):
    if not polygons:
        raise HTTPException(status_code=500, detail="No polygons to intersect")
    return reduce(lambda acc, poly: acc.intersection(poly), polygons[1:], polygons[0])


def _largest_component(geometry: Polygon | MultiPolygon) -> Polygon:
    if isinstance(geometry, MultiPolygon):
        return max(geometry.geoms, key=lambda g: g.area)
    return geometry


def _polygonal_region(geometry):
    """Extract the largest polygonal area from any geometry."""
    if geometry.is_empty:
        return geometry
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return _largest_component(geometry)
    if isinstance(geometry, GeometryCollection):
        polys = [geom for geom in geometry.geoms if isinstance(geom, (Polygon, MultiPolygon))]
        if not polys:
            return Polygon()
        merged = polys[0]
        for poly in polys[1:]:
            merged = merged.union(poly)
        return _largest_component(merged)
    return Polygon()


async def _travel_time_seconds(
    client: httpx.AsyncClient,
    origin: Tuple[float, float],
    destination: Tuple[float, float],
    profile: str,
    token: str,
) -> float:
    """Call Mapbox Directions API and return duration in seconds."""
    url = (
        f"https://api.mapbox.com/directions/v5/mapbox/"
        f"{profile}/{origin[0]},{origin[1]};{destination[0]},{destination[1]}"
    )
    params = {
        "access_token": token,
        "overview": "false",
        "alternatives": "false",
        "annotations": "duration",
        "geometries": "geojson",
    }

    try:
        resp = await client.get(url, params=params)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Directions request failed from {origin} to {destination}: {exc}",
        ) from exc

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Mapbox token")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Mapbox rate limit exceeded")
    if resp.status_code >= 500:
        raise HTTPException(status_code=502, detail="Mapbox directions service error")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="Directions request rejected by Mapbox")

    data = resp.json()
    routes = data.get("routes") or []
    if not routes or routes[0].get("duration") is None:
        raise HTTPException(status_code=502, detail="Directions response missing duration")
    return float(routes[0]["duration"])


def _objective_value(values: List[float], objective: str) -> float:
    if objective == "min_sum":
        return float(sum(values))
    return float(max(values))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/meeting-point")
async def meeting_point(payload: MeetingPointRequest = Body(...)):
    token = _mapbox_token()
    addresses = _deduplicate_addresses(payload.addresses)
    if not addresses:
        raise HTTPException(status_code=400, detail="No addresses supplied after deduplication")

    client: httpx.AsyncClient = app.state.http_client

    # Geocode all addresses concurrently
    geocode_tasks = [
        _geocode_address(client, address, payload.city_hint, token) for address in addresses
    ]
    coords = await asyncio.gather(*geocode_tasks)

    async def compute_with_minutes(minutes: int):
        iso_tasks = [
            _fetch_isochrone(client, coord, minutes, payload.profile, token) for coord in coords
        ]
        shapes = await asyncio.gather(*iso_tasks)
        geom = _intersection(shapes)
        return geom

    effective_max_minutes = payload.max_minutes
    intersection_geom = await compute_with_minutes(effective_max_minutes)

    if intersection_geom.is_empty and payload.max_minutes < MAX_MAX_MINUTES:
        bumped = min(MAX_MAX_MINUTES, payload.max_minutes + 5)
        intersection_geom = await compute_with_minutes(bumped)
        effective_max_minutes = bumped

    if intersection_geom.is_empty:
        return JSONResponse(
            status_code=200,
            content={
                "reachable": False,
                "reason": "no_common_reachable_region",
                "max_minutes": effective_max_minutes,
            },
        )

    region = _polygonal_region(intersection_geom)

    if region.is_empty:
        return JSONResponse(
            status_code=200,
            content={
                "reachable": False,
                "reason": "no_common_reachable_region",
                "max_minutes": effective_max_minutes,
            },
        )
    centroid = region.centroid
    meeting_point = {"lat": centroid.y, "lng": centroid.x}

    # Compute travel times to the centroid
    travel_tasks = [
        _travel_time_seconds(client, coord, (centroid.x, centroid.y), payload.profile, token)
        for coord in coords
    ]
    durations_seconds = await asyncio.gather(*travel_tasks)
    durations_minutes = [round(d / 60, 1) for d in durations_seconds]

    participants = []
    for address, coord, eta in zip(addresses, coords, durations_minutes):
        participants.append(
            {
                "address": address,
                "lat": coord[1],
                "lng": coord[0],
                "eta_minutes": eta,
            }
        )

    objective_value = round(_objective_value(durations_minutes, payload.objective), 2)
    reachable = all(d <= effective_max_minutes + 0.5 for d in durations_minutes)

    response_body = {
        "meeting_point": meeting_point,
        "participants": participants,
        "objective": payload.objective,
        "objective_value": objective_value,
        "max_minutes": effective_max_minutes,
        "reachable": reachable,
        "debug": {
            "intersection_polygons_geojson": mapping(region),
            "candidate_points_geojson": None,
        },
    }

    if not reachable:
        response_body["reason"] = "travel_time_exceeds_budget"

    return response_body


def main():
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


@app.get("/config.js", include_in_schema=False)
async def config_js():
    """Expose a public Mapbox token to the browser."""
    token = os.getenv("MAPBOX_PUBLIC_TOKEN") or os.getenv("MAPBOX_TOKEN")
    body = (
        f'window.MAPBOX_TOKEN = "{token}";\n'
        if token
        else "window.MAPBOX_TOKEN = null;\n"
    )
    return PlainTextResponse(body, media_type="application/javascript")


# Serve the static single-page frontend.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    main()
