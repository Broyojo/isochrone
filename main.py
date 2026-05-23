from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import urllib.parse
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import reduce
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

import httpx
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
    box,
    mapping,
    shape,
)

logger = logging.getLogger("isochrone")


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _positive_float_from_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


# Configuration constants
MAX_PARTICIPANTS = 10
MAX_ADDRESS_CHARS = 240
MAX_CITY_HINT_CHARS = 120
MAX_REQUEST_BODY_BYTES = _positive_int_from_env("MAX_REQUEST_BODY_BYTES", 64_000)
DEFAULT_MAX_MINUTES = 15
MAX_MAX_MINUTES = 60
SUPPORTED_OBJECTIVES = {"min_sum", "min_max"}
SUPPORTED_PROFILES = {"walking", "cycling", "driving", "driving-traffic"}
DEFAULT_GRID_RESOLUTION_M = 200
MIN_GRID_RESOLUTION_M = 50
MAX_GRID_RESOLUTION_M = 5_000
MAX_CANDIDATES = 225
MAX_MATRIX_REQUESTS_PER_SEARCH = 24
MAX_IN_FLIGHT_SEARCHES = _positive_int_from_env("MAX_IN_FLIGHT_SEARCHES", 4)
MAX_GEOCODE_CACHE_SIZE = 512
MAX_RATE_LIMIT_KEYS = 4_096
MEETING_POINT_PATH = "/api/meeting-point"
REQUIRE_MAPBOX_PUBLIC_TOKEN = _env_flag("REQUIRE_MAPBOX_PUBLIC_TOKEN", default=True)
MEETING_POINT_TIMEOUT_SECONDS = _positive_float_from_env("MEETING_POINT_TIMEOUT_SECONDS", 45.0)
MEETING_POINT_RATE_LIMIT = _positive_int_from_env("MEETING_POINT_RATE_LIMIT", 30)
MEETING_POINT_RATE_WINDOW_SECONDS = _positive_float_from_env("MEETING_POINT_RATE_WINDOW_SECONDS", 60.0)
MAPBOX_HTTP_TIMEOUT_SECONDS = 20.0
MAPBOX_CONNECT_TIMEOUT_SECONDS = 5.0
MAPBOX_MAX_ATTEMPTS = 3
MAPBOX_RETRY_BASE_DELAY_SECONDS = 0.2
MAPBOX_RETRY_MAX_DELAY_SECONDS = 2.0
MAPBOX_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(self)",
}
METRIC_DEFINITIONS = {
    "isochrone_http_requests_total": ("Total HTTP requests handled by the app.", "counter"),
    "isochrone_http_request_duration_seconds": ("HTTP request duration in seconds.", "summary"),
    "isochrone_mapbox_requests_total": ("Total Mapbox upstream request attempts.", "counter"),
    "isochrone_mapbox_retries_total": ("Total Mapbox upstream retries.", "counter"),
    "isochrone_meeting_point_searches_total": ("Total meeting-point searches by outcome.", "counter"),
}

# Bounded in-memory cache for geocoding results.
_geocode_cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
_rate_limit_buckets: OrderedDict[str, list[float]] = OrderedDict()
_metrics: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], float] = OrderedDict()


@dataclass(frozen=True)
class ParticipantInput:
    address: str
    profile: str


@dataclass(frozen=True)
class CandidateEvaluation:
    point: tuple[float, float]
    minutes: list[float]
    score: float


def _mapbox_token() -> str:
    token = os.getenv("MAPBOX_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MAPBOX_TOKEN environment variable is required",
        )
    return token


def _public_mapbox_token() -> str | None:
    public_token = os.getenv("MAPBOX_PUBLIC_TOKEN")
    if public_token:
        if public_token.startswith("pk."):
            return public_token
        logger.warning("Ignoring MAPBOX_PUBLIC_TOKEN because it is not a public pk token")

    token = os.getenv("MAPBOX_TOKEN")
    if token and token.startswith("pk."):
        return token
    return None


def _normalize_profile_value(value: str) -> str:
    profile = value.strip().lower()
    if profile.startswith("mapbox/"):
        profile = profile.split("/", 1)[1]
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(
            f"profile must be one of: {', '.join(sorted(SUPPORTED_PROFILES))}",
        )
    return profile


class MeetingPointRequest(BaseModel):
    addresses: list[str] = Field(..., min_length=1)
    profile: str = Field("walking")
    profiles: list[str] | None = None
    max_minutes: int = Field(DEFAULT_MAX_MINUTES, ge=5, le=MAX_MAX_MINUTES)
    objective: str = Field("min_sum")
    grid_resolution_m: float | None = Field(None, ge=MIN_GRID_RESOLUTION_M, le=MAX_GRID_RESOLUTION_M)
    city_hint: str | None = None
    use_grid_search: bool = Field(default=False)
    include_debug: bool = Field(default=False)

    @field_validator("addresses")
    @classmethod
    def validate_addresses(cls, value: list[str]) -> list[str]:
        cleaned = []
        for raw_address in value:
            address = raw_address.strip()
            if not address:
                continue
            if len(address) > MAX_ADDRESS_CHARS:
                raise ValueError(f"addresses must be {MAX_ADDRESS_CHARS} characters or fewer")
            cleaned.append(address)
        if not cleaned:
            raise ValueError("addresses cannot be empty")
        if len(cleaned) > MAX_PARTICIPANTS:
            raise ValueError(f"maximum {MAX_PARTICIPANTS} addresses are supported")
        return cleaned

    @field_validator("city_hint")
    @classmethod
    def normalize_city_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        city_hint = value.strip()
        if not city_hint:
            return None
        if len(city_hint) > MAX_CITY_HINT_CHARS:
            raise ValueError(f"city_hint must be {MAX_CITY_HINT_CHARS} characters or fewer")
        return city_hint

    @field_validator("profile")
    @classmethod
    def normalize_profile(cls, value: str) -> str:
        return _normalize_profile_value(value)

    @field_validator("profiles")
    @classmethod
    def normalize_profiles(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return [_normalize_profile_value(profile) for profile in value]

    @model_validator(mode="after")
    def validate_profile_count(self):
        if self.profiles is not None and len(self.profiles) != len(self.addresses):
            raise ValueError("profiles must have the same length as addresses")
        return self

    @field_validator("objective")
    @classmethod
    def normalize_objective(cls, value: str) -> str:
        objective = value.lower()
        if objective not in SUPPORTED_OBJECTIVES:
            raise ValueError(
                f"objective must be one of: {', '.join(sorted(SUPPORTED_OBJECTIVES))}",
            )
        return objective


class PointResponse(BaseModel):
    lat: float
    lng: float


class ParticipantResponse(PointResponse):
    address: str
    profile: str
    eta_minutes: float


class DebugResponse(BaseModel):
    intersection_polygons_geojson: dict[str, Any]
    candidate_points_geojson: dict[str, Any] | None = None


class MeetingPointResponse(BaseModel):
    reachable: bool
    max_minutes: int
    meeting_point: PointResponse | None = None
    participants: list[ParticipantResponse] = Field(default_factory=list)
    objective: str | None = None
    objective_value: float | None = None
    reason: str | None = None
    debug: DebugResponse | None = None


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    mapbox_token_configured: bool
    mapbox_public_token_configured: bool


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(
        MAPBOX_HTTP_TIMEOUT_SECONDS,
        connect=MAPBOX_CONNECT_TIMEOUT_SECONDS,
    )
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    client = httpx.AsyncClient(timeout=timeout, limits=limits)
    app.state.http_client = client
    app.state.search_semaphore = asyncio.Semaphore(MAX_IN_FLIGHT_SEARCHES)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="Isochrone Meeting Point API",
    version="0.1.0",
    description="Compute a fair meeting point reachable within a shared travel-time budget.",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    start = time.perf_counter()
    try:
        response, guarded_request = await _guard_meeting_point_request(request)
        if response is None:
            response = await call_next(guarded_request)
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        _record_http_metrics(request, status.HTTP_500_INTERNAL_SERVER_ERROR, duration_ms / 1000)
        logger.exception(
            "request failed method=%s path=%s request_id=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            request_id,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["x-request-id"] = request_id
    _apply_security_headers(response)
    _record_http_metrics(request, response.status_code, duration_ms / 1000)
    logger.info(
        "request complete method=%s path=%s status=%s request_id=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        request_id,
        duration_ms,
    )
    return response


def _metric_add(name: str, labels: dict[str, str], value: float = 1.0) -> None:
    key = (name, tuple(sorted(labels.items())))
    _metrics[key] = _metrics.get(key, 0.0) + value
    _metrics.move_to_end(key)


def _prometheus_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prometheus_sample(name: str, labels: tuple[tuple[str, str], ...], value: float) -> str:
    label_text = ",".join(f'{key}="{_prometheus_escape(label_value)}"' for key, label_value in labels)
    sample_name = f"{name}{{{label_text}}}" if label_text else name
    sample_value = str(int(value)) if value.is_integer() else f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{sample_name} {sample_value}"


def _metrics_text() -> str:
    lines: list[str] = []
    for metric_name, (help_text, metric_type) in METRIC_DEFINITIONS.items():
        lines.append(f"# HELP {metric_name} {help_text}")
        lines.append(f"# TYPE {metric_name} {metric_type}")
        for (name, labels), value in _metrics.items():
            if name == metric_name or name.startswith(f"{metric_name}_"):
                lines.append(_prometheus_sample(name, labels, value))
    return "\n".join(lines) + "\n"


def _route_label(path: str) -> str:
    if path in {MEETING_POINT_PATH, "/health", "/ready", "/metrics", "/config.js"}:
        return path
    return "_static" if "." in path or path == "/" else "_other"


def _record_http_metrics(request: Request, status_code: int, duration_seconds: float) -> None:
    labels = {
        "method": request.method,
        "path": _route_label(request.url.path),
        "status": str(status_code),
    }
    _metric_add("isochrone_http_requests_total", labels)
    _metric_add("isochrone_http_request_duration_seconds_count", labels)
    _metric_add("isochrone_http_request_duration_seconds_sum", labels, duration_seconds)


def _apply_security_headers(response: Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value


def _is_meeting_point_request(request: Request) -> bool:
    return request.method == "POST" and request.url.path == MEETING_POINT_PATH


def _client_rate_key(request: Request) -> str:
    if _env_flag("TRUST_PROXY_HEADERS", default=False):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_retry_after_seconds(client_key: str, now: float) -> int | None:
    window_start = now - MEETING_POINT_RATE_WINDOW_SECONDS
    timestamps = [timestamp for timestamp in _rate_limit_buckets.get(client_key, []) if timestamp > window_start]
    if len(timestamps) >= MEETING_POINT_RATE_LIMIT:
        _rate_limit_buckets[client_key] = timestamps
        _rate_limit_buckets.move_to_end(client_key)
        return max(1, math.ceil(timestamps[0] + MEETING_POINT_RATE_WINDOW_SECONDS - now))

    timestamps.append(now)
    _rate_limit_buckets[client_key] = timestamps
    _rate_limit_buckets.move_to_end(client_key)
    while len(_rate_limit_buckets) > MAX_RATE_LIMIT_KEYS:
        _rate_limit_buckets.popitem(last=False)
    return None


def _body_too_large_response() -> JSONResponse:
    return JSONResponse(
        {"detail": f"Request body must be {MAX_REQUEST_BODY_BYTES} bytes or fewer"},
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
    )


def _request_content_length_response(request: Request) -> JSONResponse | None:
    raw_content_length = request.headers.get("content-length")
    if raw_content_length is None:
        return None
    try:
        content_length = int(raw_content_length)
    except ValueError:
        return JSONResponse(
            {"detail": "Invalid Content-Length header"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if content_length < 0:
        return JSONResponse(
            {"detail": "Invalid Content-Length header"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if content_length > MAX_REQUEST_BODY_BYTES:
        return _body_too_large_response()
    return None


async def _request_with_bounded_body(request: Request) -> tuple[JSONResponse | None, Request]:
    body = bytearray()
    receive = request._receive  # noqa: SLF001
    extra_messages: list[Any] = []

    while True:
        message = await receive()
        if message["type"] != "http.request":
            extra_messages.append(message)
            break

        chunk = message.get("body", b"")
        if not chunk:
            if not message.get("more_body", False):
                break
            continue
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BODY_BYTES:
            return _body_too_large_response(), request
        if not message.get("more_body", False):
            break

    body_bytes = bytes(body)
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            if extra_messages:
                return extra_messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request._receive = receive  # noqa: SLF001
    return None, request


async def _guard_meeting_point_request(request: Request) -> tuple[JSONResponse | None, Request]:
    if not _is_meeting_point_request(request):
        return None, request

    size_response = _request_content_length_response(request)
    if size_response is not None:
        _metric_add("isochrone_meeting_point_searches_total", {"outcome": "rejected_body_size"})
        return size_response, request

    retry_after = _rate_limit_retry_after_seconds(_client_rate_key(request), time.monotonic())
    if retry_after is not None:
        _metric_add("isochrone_meeting_point_searches_total", {"outcome": "rate_limited"})
        return (
            JSONResponse(
                {"detail": "Too many meeting-point searches. Try again shortly."},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
            ),
            request,
        )

    size_response, bounded_request = await _request_with_bounded_body(request)
    if size_response is not None:
        _metric_add("isochrone_meeting_point_searches_total", {"outcome": "rejected_body_size"})
        return size_response, request
    return None, bounded_request


def _retry_after_delay_seconds(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        raw_retry_after = response.headers.get("retry-after")
        if raw_retry_after:
            try:
                return min(MAPBOX_RETRY_MAX_DELAY_SECONDS, max(0.0, float(raw_retry_after)))
            except ValueError:
                pass
    return min(MAPBOX_RETRY_MAX_DELAY_SECONDS, MAPBOX_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))


def _mapbox_request_failure_detail(service: str) -> str:
    return f"Mapbox {service} request failed"


async def _mapbox_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any],
    service: str,
    failure_detail: str,
) -> httpx.Response:
    for attempt in range(1, MAPBOX_MAX_ATTEMPTS + 1):
        try:
            response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            _metric_add("isochrone_mapbox_requests_total", {"service": service, "status": "request_error"})
            if attempt < MAPBOX_MAX_ATTEMPTS:
                _metric_add("isochrone_mapbox_retries_total", {"reason": "request_error", "service": service})
                logger.warning(
                    "mapbox request retry service=%s attempt=%s error_type=%s",
                    service,
                    attempt,
                    type(exc).__name__,
                )
                await asyncio.sleep(_retry_after_delay_seconds(None, attempt))
                continue
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_mapbox_request_failure_detail(service),
            ) from exc

        _metric_add("isochrone_mapbox_requests_total", {"service": service, "status": str(response.status_code)})
        if response.status_code in MAPBOX_RETRY_STATUS_CODES and attempt < MAPBOX_MAX_ATTEMPTS:
            _metric_add("isochrone_mapbox_retries_total", {"reason": str(response.status_code), "service": service})
            logger.warning(
                "mapbox request retry service=%s attempt=%s status=%s",
                service,
                attempt,
                response.status_code,
            )
            await asyncio.sleep(_retry_after_delay_seconds(response, attempt))
            continue
        return response

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{failure_detail}: retry budget exhausted",
    )


def _raise_for_mapbox_status(
    response: httpx.Response,
    *,
    service: str,
    rejected_detail: str,
) -> None:
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid Mapbox token")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="Mapbox rate limit exceeded. Try again shortly.")
    if response.status_code >= 500:
        raise HTTPException(status_code=502, detail=f"Mapbox {service} service error")
    if response.status_code >= 400:
        raise HTTPException(status_code=400, detail=rejected_detail)


def _mapbox_json(response: httpx.Response, service: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Mapbox {service} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"Mapbox {service} returned invalid JSON")
    return data


def _mapbox_feature(data: dict[str, Any], service: str) -> dict[str, Any] | None:
    features = data.get("features")
    if not features:
        return None
    if not isinstance(features, list):
        raise HTTPException(status_code=502, detail=f"Mapbox {service} response had invalid features")
    feature = features[0]
    if not isinstance(feature, dict):
        raise HTTPException(status_code=502, detail=f"Mapbox {service} response had invalid features")
    return feature


def _geocoding_coordinates(feature: dict[str, Any]) -> tuple[float, float]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(status_code=502, detail="Geocoding response had invalid coordinates")

    coords = geometry.get("coordinates")
    if not isinstance(coords, list | tuple) or len(coords) < 2:
        raise HTTPException(status_code=502, detail="Geocoding response had invalid coordinates")

    try:
        return float(coords[0]), float(coords[1])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Geocoding response had invalid coordinates") from exc


def _geocode_cache_key(address: str, city_hint: str | None) -> str:
    normalized_address = address.strip().casefold()
    normalized_hint = (city_hint or "").strip().casefold()
    return f"{normalized_address}|{normalized_hint}"


def _cache_geocode_result(cache_key: str, coord: tuple[float, float]) -> None:
    _geocode_cache[cache_key] = coord
    _geocode_cache.move_to_end(cache_key)
    while len(_geocode_cache) > MAX_GEOCODE_CACHE_SIZE:
        _geocode_cache.popitem(last=False)


def _deduplicate_participants(
    participants: Sequence[ParticipantInput],
) -> list[ParticipantInput]:
    seen = set()
    deduped: list[ParticipantInput] = []
    for participant in participants:
        key = (participant.address.lower(), participant.profile)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(participant)
    return deduped


def _participants_from_payload(payload: MeetingPointRequest) -> list[ParticipantInput]:
    profiles = payload.profiles or [payload.profile] * len(payload.addresses)
    return [
        ParticipantInput(address=address, profile=profile)
        for address, profile in zip(payload.addresses, profiles, strict=True)
    ]


async def _geocode_address(
    client: httpx.AsyncClient,
    address: str,
    city_hint: str | None,
    token: str,
) -> tuple[float, float]:
    """Return (lng, lat) for a single address using Mapbox Geocoding."""
    cache_key = _geocode_cache_key(address, city_hint)
    if cache_key in _geocode_cache:
        _geocode_cache.move_to_end(cache_key)
        return _geocode_cache[cache_key]

    query = f"{address}, {city_hint}" if city_hint else address
    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{urllib.parse.quote(query)}.json"
    params = {"access_token": token, "limit": 1, "autocomplete": False}

    resp = await _mapbox_get(
        client,
        url,
        params=params,
        service="geocoding",
        failure_detail=f"Geocoding failed for '{address}'",
    )
    _raise_for_mapbox_status(
        resp,
        service="geocoding",
        rejected_detail=f"Geocoding error for '{address}'",
    )

    data = _mapbox_json(resp, "geocoding")
    feature = _mapbox_feature(data, "geocoding")
    if feature is None:
        raise HTTPException(status_code=400, detail=f"Address not found: '{address}'")

    lng, lat = _geocoding_coordinates(feature)
    _cache_geocode_result(cache_key, (lng, lat))
    return lng, lat


async def _fetch_isochrone(
    client: httpx.AsyncClient,
    coordinate: tuple[float, float],
    minutes: int,
    profile: str,
    token: str,
):
    lng, lat = coordinate
    url = f"https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
    params = {"contours_minutes": minutes, "polygons": "true", "access_token": token}

    resp = await _mapbox_get(
        client,
        url,
        params=params,
        service="isochrone",
        failure_detail=f"Isochrone request failed for coordinate {coordinate}",
    )
    _raise_for_mapbox_status(
        resp,
        service="isochrone",
        rejected_detail="Isochrone request rejected by Mapbox",
    )

    data = _mapbox_json(resp, "isochrone")
    feature = _mapbox_feature(data, "isochrone")
    if feature is None:
        raise HTTPException(
            status_code=502,
            detail="Isochrone response contained no geometry",
        )

    geom = feature.get("geometry")
    try:
        polygon = shape(geom)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Isochrone response contained invalid geometry") from exc
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


def _grid_step_degrees(resolution_m: float, lat_deg: float) -> tuple[float, float]:
    # Approximate meters per degree.
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * math.cos(math.radians(lat_deg))
    lat_step = resolution_m / meters_per_deg_lat
    lon_step = resolution_m / max(meters_per_deg_lon, 1e-6)
    return lon_step, lat_step


def _generate_candidate_points(
    region: Polygon,
    resolution_m: float,
) -> list[tuple[float, float]]:
    """Generate candidate points spread across the full polygon, up to MAX_CANDIDATES."""
    minx, miny, maxx, maxy = region.bounds
    center_lat = (miny + maxy) / 2
    lon_step, lat_step = _grid_step_degrees(resolution_m, center_lat)

    width = max(maxx - minx, 0.0)
    height = max(maxy - miny, 0.0)
    if width == 0 or height == 0:
        point = region.representative_point()
        return [(point.x, point.y)]

    base_cols = max(1, math.ceil(width / lon_step))
    base_rows = max(1, math.ceil(height / lat_step))
    cols = base_cols
    rows = base_rows

    if cols * rows > MAX_CANDIDATES:
        scale = math.sqrt((cols * rows) / MAX_CANDIDATES)
        cols = max(1, min(base_cols, round(base_cols / scale)))
        rows = max(1, min(base_rows, round(base_rows / scale)))
        while cols * rows > MAX_CANDIDATES:
            if cols >= rows and cols > 1:
                cols -= 1
            elif rows > 1:
                rows -= 1
            else:
                break

    candidates: list[tuple[float, float]] = []
    cell_width = width / cols
    cell_height = height / rows
    for row in range(rows):
        y0 = miny + row * cell_height
        y1 = maxy if row == rows - 1 else y0 + cell_height
        for col in range(cols):
            x0 = minx + col * cell_width
            x1 = maxx if col == cols - 1 else x0 + cell_width
            clipped = region.intersection(box(x0, y0, x1, y1))
            if clipped.is_empty:
                continue
            point = clipped.representative_point()
            candidates.append((point.x, point.y))

    if not candidates:
        point = region.representative_point()
        candidates.append((point.x, point.y))
    return candidates


def _matrix_coordinate_limit(profile: str) -> int:
    return 10 if profile == "driving-traffic" else 25


def _matrix_chunk_sizes(origin_count: int, candidate_count: int, profile: str) -> tuple[int, int]:
    limit = _matrix_coordinate_limit(profile)
    best_origin_chunk_size = 1
    best_candidate_chunk_size = max(1, limit - 1)
    best_request_count = math.inf
    for origin_chunk_size in range(1, min(origin_count, limit - 1) + 1):
        candidate_chunk_size = max(1, limit - origin_chunk_size)
        request_count = math.ceil(origin_count / origin_chunk_size) * math.ceil(candidate_count / candidate_chunk_size)
        if request_count < best_request_count:
            best_request_count = request_count
            best_origin_chunk_size = origin_chunk_size
            best_candidate_chunk_size = candidate_chunk_size
    return best_origin_chunk_size, best_candidate_chunk_size


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _profile_index_groups(profiles: Sequence[str]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for idx, profile in enumerate(profiles):
        groups.setdefault(profile, []).append(idx)
    return groups


def _estimated_matrix_requests(profiles: Sequence[str], candidate_count: int) -> int:
    total = 0
    for profile, origin_indices in _profile_index_groups(profiles).items():
        origin_chunk_size, candidate_chunk_size = _matrix_chunk_sizes(len(origin_indices), candidate_count, profile)
        total += math.ceil(len(origin_indices) / origin_chunk_size) * math.ceil(candidate_count / candidate_chunk_size)
    return total


def _sample_candidates(candidates: list[tuple[float, float]], max_count: int) -> list[tuple[float, float]]:
    if len(candidates) <= max_count:
        return candidates
    if max_count <= 1:
        return [candidates[-1]]

    step = (len(candidates) - 1) / (max_count - 1)
    sampled: list[tuple[float, float]] = []
    used_indices: set[int] = set()
    for idx in range(max_count):
        candidate_index = min(round(idx * step), len(candidates) - 1)
        if candidate_index in used_indices:
            continue
        used_indices.add(candidate_index)
        sampled.append(candidates[candidate_index])
    if sampled[-1] != candidates[-1]:
        sampled[-1] = candidates[-1]
    return sampled


def _limit_candidates_for_matrix(
    candidates: list[tuple[float, float]],
    profiles: Sequence[str],
) -> list[tuple[float, float]]:
    if not candidates:
        return candidates

    candidate_count = len(candidates)
    while (
        candidate_count > 1 and _estimated_matrix_requests(profiles, candidate_count) > MAX_MATRIX_REQUESTS_PER_SEARCH
    ):
        candidate_count = max(1, candidate_count // 2)
    return _sample_candidates(candidates, candidate_count)


async def _matrix_durations_seconds(
    client: httpx.AsyncClient,
    origins: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
    profile: str,
    token: str,
) -> list[list[float | None]]:
    """Call Mapbox Matrix API and return duration rows from origins to destinations."""
    if not origins or not destinations:
        return []

    coordinates = [*origins, *destinations]
    coord_text = ";".join(f"{lng},{lat}" for lng, lat in coordinates)
    destination_offset = len(origins)
    source_indices = ";".join(str(idx) for idx in range(len(origins)))
    destination_indices = ";".join(str(idx) for idx in range(destination_offset, len(coordinates)))
    url = f"https://api.mapbox.com/directions-matrix/v1/mapbox/{profile}/{coord_text}"
    params = {
        "access_token": token,
        "annotations": "duration",
        "sources": source_indices,
        "destinations": destination_indices,
    }

    resp = await _mapbox_get(
        client,
        url,
        params=params,
        service="matrix",
        failure_detail=f"Matrix request failed for {profile}",
    )
    _raise_for_mapbox_status(
        resp,
        service="matrix",
        rejected_detail="Matrix request rejected by Mapbox",
    )

    data = _mapbox_json(resp, "matrix")
    durations = data.get("durations")
    if not isinstance(durations, list):
        raise HTTPException(status_code=502, detail="Matrix response missing durations")

    rows: list[list[float | None]] = []
    for row in durations:
        if not isinstance(row, list):
            raise HTTPException(status_code=502, detail="Matrix response contained an invalid duration row")
        if len(row) != len(destinations):
            raise HTTPException(status_code=502, detail="Matrix response had unexpected dimensions")
        try:
            rows.append([None if value is None else float(value) for value in row])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Matrix response contained an invalid duration value") from exc
    if len(rows) != len(origins):
        raise HTTPException(status_code=502, detail="Matrix response had unexpected dimensions")
    return rows


async def _candidate_duration_rows(
    client: httpx.AsyncClient,
    coords: Sequence[tuple[float, float]],
    profiles: Sequence[str],
    candidates: Sequence[tuple[float, float]],
    token: str,
) -> list[list[float | None]]:
    durations_by_candidate: list[list[float | None]] = [[None] * len(coords) for _ in candidates]

    for profile, origin_indices in _profile_index_groups(profiles).items():
        origin_chunk_size, candidate_chunk_size = _matrix_chunk_sizes(len(origin_indices), len(candidates), profile)
        for origin_chunk in _chunks(origin_indices, origin_chunk_size):
            origin_coords = [coords[idx] for idx in origin_chunk]
            for start in range(0, len(candidates), candidate_chunk_size):
                destination_chunk = candidates[start : start + candidate_chunk_size]
                matrix_rows = await _matrix_durations_seconds(
                    client,
                    origin_coords,
                    destination_chunk,
                    profile,
                    token,
                )
                for row_idx, origin_idx in enumerate(origin_chunk):
                    for candidate_offset, duration in enumerate(matrix_rows[row_idx]):
                        durations_by_candidate[start + candidate_offset][origin_idx] = duration

    return durations_by_candidate


def _objective_value(values: list[float], objective: str) -> float:
    if objective == "min_sum":
        return float(sum(values))
    return float(max(values))


def _debug_payload(
    region: Polygon,
    candidate_points_geojson: dict[str, Any] | None,
    *,
    include_debug: bool,
) -> DebugResponse | None:
    if not include_debug:
        return None
    return DebugResponse(
        intersection_polygons_geojson=mapping(region),
        candidate_points_geojson=candidate_points_geojson,
    )


def _participant_response(
    address: str,
    profile: str,
    coord: tuple[float, float],
    eta_minutes: float,
) -> ParticipantResponse:
    return ParticipantResponse(
        address=address,
        profile=profile,
        lat=coord[1],
        lng=coord[0],
        eta_minutes=round(eta_minutes, 1),
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/ready", response_model=ReadinessResponse)
async def ready(response: Response) -> ReadinessResponse:
    mapbox_token_configured = bool(os.getenv("MAPBOX_TOKEN"))
    mapbox_public_token_configured = _public_mapbox_token() is not None
    if not mapbox_token_configured:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        status_text = "missing_mapbox_token"
    elif REQUIRE_MAPBOX_PUBLIC_TOKEN and not mapbox_public_token_configured:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        status_text = "missing_mapbox_public_token"
    else:
        status_text = "ready"

    return ReadinessResponse(
        status=status_text,
        mapbox_token_configured=mapbox_token_configured,
        mapbox_public_token_configured=mapbox_public_token_configured,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(_metrics_text(), media_type="text/plain; version=0.0.4")


MeetingPointPayload = Annotated[MeetingPointRequest, Body()]


@app.post(
    "/api/meeting-point",
    response_model=MeetingPointResponse,
    response_model_exclude_none=True,
)
async def meeting_point(payload: MeetingPointPayload) -> MeetingPointResponse:
    semaphore: asyncio.Semaphore | None = getattr(app.state, "search_semaphore", None)
    if semaphore is None:
        return await _timed_meeting_point(payload)

    async with semaphore:
        return await _timed_meeting_point(payload)


async def _timed_meeting_point(payload: MeetingPointRequest) -> MeetingPointResponse:
    try:
        async with asyncio.timeout(MEETING_POINT_TIMEOUT_SECONDS):
            return await _instrumented_meeting_point(payload)
    except TimeoutError as exc:
        _metric_add("isochrone_meeting_point_searches_total", {"outcome": "timeout"})
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Meeting-point search timed out. Try again shortly.",
        ) from exc


async def _instrumented_meeting_point(payload: MeetingPointRequest) -> MeetingPointResponse:
    try:
        result = await _compute_meeting_point(payload)
    except HTTPException:
        _metric_add("isochrone_meeting_point_searches_total", {"outcome": "error"})
        raise

    outcome = "reachable" if result.reachable else result.reason or "unreachable"
    _metric_add("isochrone_meeting_point_searches_total", {"outcome": outcome})
    return result


async def _compute_meeting_point(payload: MeetingPointRequest) -> MeetingPointResponse:
    token = _mapbox_token()
    participants_input = _deduplicate_participants(_participants_from_payload(payload))
    if not participants_input:
        raise HTTPException(
            status_code=400,
            detail="No addresses supplied after deduplication",
        )
    addresses = [participant.address for participant in participants_input]
    profiles = [participant.profile for participant in participants_input]

    client: httpx.AsyncClient = app.state.http_client

    # Geocode all addresses concurrently
    geocode_tasks = [_geocode_address(client, address, payload.city_hint, token) for address in addresses]
    coords = await asyncio.gather(*geocode_tasks)

    async def compute_with_minutes(minutes: int):
        iso_tasks = [
            _fetch_isochrone(client, coord, minutes, profile, token)
            for coord, profile in zip(coords, profiles, strict=True)
        ]
        shapes = await asyncio.gather(*iso_tasks)
        return _intersection(shapes)

    effective_max_minutes = payload.max_minutes
    intersection_geom = await compute_with_minutes(effective_max_minutes)

    if intersection_geom.is_empty:
        return MeetingPointResponse(
            reachable=False,
            reason="no_common_reachable_region",
            max_minutes=effective_max_minutes,
            objective=payload.objective,
        )

    region = _polygonal_region(intersection_geom)

    if region.is_empty:
        return MeetingPointResponse(
            reachable=False,
            reason="no_common_reachable_region",
            max_minutes=effective_max_minutes,
            objective=payload.objective,
        )

    candidates: list[tuple[float, float]] = []
    candidate_points_geojson = None

    if payload.use_grid_search:
        res_m = payload.grid_resolution_m or DEFAULT_GRID_RESOLUTION_M
        candidates = _generate_candidate_points(region, res_m)
        centroid = region.centroid
        if (centroid.x, centroid.y) not in candidates:
            candidates.append((centroid.x, centroid.y))
    else:
        centroid = region.centroid
        candidates = [(centroid.x, centroid.y)]

    candidates = _limit_candidates_for_matrix(candidates, profiles)
    duration_rows = await _candidate_duration_rows(client, coords, profiles, candidates, token)
    evaluations: list[CandidateEvaluation | None] = []
    for point, durations in zip(candidates, duration_rows, strict=True):
        if any(duration is None for duration in durations):
            evaluations.append(None)
            continue
        minutes = [duration / 60 for duration in durations if duration is not None]
        if any(minutes_value > effective_max_minutes for minutes_value in minutes):
            evaluations.append(None)
            continue
        score = _objective_value(minutes, payload.objective)
        evaluations.append(CandidateEvaluation(point=point, minutes=minutes, score=score))
    valid = [e for e in evaluations if e is not None]

    if payload.use_grid_search:
        candidate_points_geojson = {
            "type": "FeatureCollection",
            "features": [],
        }
        for pt, ev in zip(candidates, evaluations, strict=True):
            props = {
                "reachable": bool(ev),
                "score": round(ev.score, 4) if ev else None,
            }
            candidate_points_geojson["features"].append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": {"type": "Point", "coordinates": [pt[0], pt[1]]},
                },
            )

    if not valid:
        return MeetingPointResponse(
            reachable=False,
            reason="no_candidate_within_budget",
            max_minutes=effective_max_minutes,
            objective=payload.objective,
            debug=_debug_payload(region, candidate_points_geojson, include_debug=payload.include_debug),
        )

    best = min(valid, key=lambda e: e.score)
    participants = [
        _participant_response(address, profile, coord, eta)
        for address, profile, coord, eta in zip(addresses, profiles, coords, best.minutes, strict=True)
    ]

    return MeetingPointResponse(
        meeting_point=PointResponse(lat=best.point[1], lng=best.point[0]),
        participants=participants,
        objective=payload.objective,
        objective_value=round(best.score, 2),
        max_minutes=effective_max_minutes,
        reachable=True,
        debug=_debug_payload(region, candidate_points_geojson, include_debug=payload.include_debug),
    )


def _port_from_env() -> int:
    raw_port = os.getenv("PORT", "8000")
    try:
        return int(raw_port)
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc


def main():
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=_port_from_env(),
        reload=_env_flag("RELOAD", default=False),
    )


@app.get("/config.js", include_in_schema=False)
async def config_js():
    """Expose a public Mapbox token to the browser."""
    token = _public_mapbox_token()
    body = f"window.MAPBOX_TOKEN = {json.dumps(token)};\n"
    return PlainTextResponse(
        body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


# Serve the static single-page frontend.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    main()
