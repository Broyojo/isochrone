import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from shapely.geometry import Polygon, box

import main


@pytest.fixture(autouse=True)
def patch_mapbox(monkeypatch):
    # Avoid real Mapbox calls; provide deterministic geometry and times.
    main._rate_limit_buckets.clear()
    main._metrics.clear()

    async def fake_geocode(client, address: str, city_hint: str, token: str) -> tuple[float, float]:
        # Encode address into simple coordinates to ensure uniqueness.
        idx = hash(address) % 10
        return float(idx), float(idx)

    async def fake_isochrone(client, coord, minutes: int, profile: str, token: str):
        # Return a shared 1x1 box so intersections stay non-empty.
        return box(0, 0, 1, 1)

    async def fake_matrix(client, origins, destinations, profile: str, token: str):
        # 10 minutes from every origin to every candidate destination.
        return [[600.0 for _destination in destinations] for _origin in origins]

    monkeypatch.setattr(main, "_mapbox_token", lambda: "test-token")
    monkeypatch.setattr(main, "_geocode_address", fake_geocode)
    monkeypatch.setattr(main, "_fetch_isochrone", fake_isochrone)
    monkeypatch.setattr(main, "_matrix_durations_seconds", fake_matrix)

    http_client = httpx.AsyncClient(timeout=5)
    main.app.state.http_client = http_client

    yield

    main._rate_limit_buckets.clear()
    main._metrics.clear()
    asyncio.run(http_client.aclose())


@pytest.mark.asyncio
async def test_meeting_point_success():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "city_hint": "Nowhere",
            "max_minutes": 15,
            "objective": "min_sum",
            "profile": "walking",
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["reachable"] is True
        assert "meeting_point" in data
        assert "debug" not in data
        assert [participant["profile"] for participant in data["participants"]] == ["walking", "walking"]
        # Centroid of box(0,0,1,1) is (0.5,0.5)
        assert pytest.approx(0.5, rel=1e-3) == data["meeting_point"]["lat"]
        assert pytest.approx(0.5, rel=1e-3) == data["meeting_point"]["lng"]
        assert data["objective_value"] == 20.0  # two participants * 10 minutes each


@pytest.mark.asyncio
async def test_unreachable_when_intersection_empty(monkeypatch):
    async def empty_isochrone(client, coord, minutes: int, profile: str, token: str):
        return Polygon()  # empty geometry forces no intersection

    monkeypatch.setattr(main, "_fetch_isochrone", empty_isochrone)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "max_minutes": 15,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["reachable"] is False
        assert data["reason"] == "no_common_reachable_region"


@pytest.mark.asyncio
async def test_debug_payload_is_opt_in():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "max_minutes": 15,
            "include_debug": True,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["reachable"] is True
        assert "debug" in data
        assert data["debug"]["intersection_polygons_geojson"]["type"] == "Polygon"


@pytest.mark.asyncio
async def test_debug_grid_search_includes_candidate_scores(monkeypatch):
    monkeypatch.setattr(main, "_generate_candidate_points", lambda _region, _res: [(0.25, 0.25), (0.75, 0.75)])

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "max_minutes": 15,
            "use_grid_search": True,
            "include_debug": True,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        features = data["debug"]["candidate_points_geojson"]["features"]
        assert features
        assert all("score" in feature["properties"] for feature in features)
        assert all("reachable" in feature["properties"] for feature in features)


@pytest.mark.asyncio
async def test_no_candidate_within_budget_returns_no_meeting_point(monkeypatch):
    async def slow_matrix(client, origins, destinations, profile: str, token: str):
        return [[16 * 60 for _destination in destinations] for _origin in origins]

    monkeypatch.setattr(main, "_matrix_durations_seconds", slow_matrix)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "max_minutes": 15,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["reachable"] is False
        assert data["reason"] == "no_candidate_within_budget"
        assert "meeting_point" not in data


def test_polygonal_region_handles_geometry_collection():
    # Ensure helper returns polygon even when mixed geometry types are present.
    p1 = box(0, 0, 1, 1)
    p2 = box(0.5, 0.5, 1.5, 1.5)
    collection = p1.union(p2).buffer(0).boundary.union(p1)  # mixed geometry
    result = main._polygonal_region(collection)
    assert isinstance(result, Polygon)
    assert result.area > 0


def test_candidate_grid_spans_large_region_when_capped():
    candidates = main._generate_candidate_points(box(0, 0, 10, 10), resolution_m=1_000)

    assert 100 <= len(candidates) <= main.MAX_CANDIDATES
    xs = [lng for lng, _lat in candidates]
    ys = [lat for _lng, lat in candidates]
    assert min(xs) < 2
    assert max(xs) > 8
    assert min(ys) < 2
    assert max(ys) > 8


def test_matrix_candidate_limit_keeps_dense_samples_when_chunking_allows_it():
    candidates = [(float(idx), 0.0) for idx in range(40)]
    profiles = ["driving-traffic"] * 10

    assert main._estimated_matrix_requests(profiles, len(candidates)) <= main.MAX_MATRIX_REQUESTS_PER_SEARCH
    assert main._limit_candidates_for_matrix(candidates, profiles) == candidates


@pytest.mark.asyncio
async def test_grid_search_respects_objective(monkeypatch):
    # Two candidates: near (0,0) and far (10,0).
    monkeypatch.setattr(main, "_generate_candidate_points", lambda _region, _res: [(0.0, 0.0), (10.0, 0.0)])

    async def iso_big(client, coord, minutes: int, profile: str, token: str):
        return box(-1, -1, 11, 1)

    async def geocode_fixed(client, address: str, city_hint: str, token: str):
        return (0.0, 0.0) if "A" in address else (1.0, 0.0)

    async def matrix(client, origins, destinations, profile: str, token: str):
        # Candidate near 0: times 8 and 14 minutes. Candidate near 10: times 12 and 12.
        rows = []
        for origin in origins:
            row = []
            for destination in destinations:
                minutes = (8 if origin[0] < 0.5 else 14) if destination[0] < 5 else 12
                row.append(minutes * 60)
            rows.append(row)
        return rows

    monkeypatch.setattr(main, "_fetch_isochrone", iso_big)
    monkeypatch.setattr(main, "_geocode_address", geocode_fixed)
    monkeypatch.setattr(main, "_matrix_durations_seconds", matrix)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "use_grid_search": True,
            "objective": "min_max",
            "max_minutes": 15,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        # For min_max, candidate at lng=10 yields max=12 vs max=14 at lng=0.
        assert pytest.approx(10.0, rel=1e-3) == data["meeting_point"]["lng"]


@pytest.mark.asyncio
async def test_per_participant_profiles_are_used(monkeypatch):
    iso_profiles = []
    matrix_profiles = []

    async def iso_profiled(client, coord, minutes: int, profile: str, token: str):
        iso_profiles.append(profile)
        return box(0, 0, 1, 1)

    async def matrix_profiled(client, origins, destinations, profile: str, token: str):
        matrix_profiles.append(profile)
        duration = 300.0 if profile == "driving" else 600.0
        return [[duration for _destination in destinations] for _origin in origins]

    monkeypatch.setattr(main, "_fetch_isochrone", iso_profiled)
    monkeypatch.setattr(main, "_matrix_durations_seconds", matrix_profiled)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "profiles": ["walking", "driving"],
            "max_minutes": 15,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["reachable"] is True
        assert [participant["profile"] for participant in data["participants"]] == ["walking", "driving"]
        assert [participant["eta_minutes"] for participant in data["participants"]] == [10.0, 5.0]

    assert iso_profiles == ["walking", "driving"]
    assert matrix_profiles == ["walking", "driving"]


@pytest.mark.asyncio
async def test_grid_search_batches_candidate_scoring_with_matrix(monkeypatch):
    candidates = [(float(idx), 0.0) for idx in range(40)]
    matrix_calls = []

    monkeypatch.setattr(main, "_generate_candidate_points", lambda _region, _res: candidates)

    async def iso_big(client, coord, minutes: int, profile: str, token: str):
        return box(-1, -1, 41, 1)

    async def matrix_batched(client, origins, destinations, profile: str, token: str):
        matrix_calls.append((len(origins), len(destinations), profile))
        return [[600.0 for _destination in destinations] for _origin in origins]

    monkeypatch.setattr(main, "_fetch_isochrone", iso_big)
    monkeypatch.setattr(main, "_matrix_durations_seconds", matrix_batched)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st", "C st", "D st", "E st", "F st"],
            "profiles": ["driving"] * 6,
            "max_minutes": 60,
            "objective": "min_max",
            "use_grid_search": True,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        assert res.json()["reachable"] is True

    # Six origins plus up to 19 destinations fit in one Matrix request, so 40 candidates need only 3 requests.
    assert len(matrix_calls) == 3
    assert matrix_calls == [(6, 19, "driving"), (6, 19, "driving"), (6, 2, "driving")]


@pytest.mark.asyncio
async def test_matrix_null_duration_makes_candidate_unreachable(monkeypatch):
    async def missing_duration_matrix(client, origins, destinations, profile: str, token: str):
        return [[None for _destination in destinations] for _origin in origins]

    monkeypatch.setattr(main, "_matrix_durations_seconds", missing_duration_matrix)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {
            "addresses": ["A st", "B st"],
            "max_minutes": 15,
        }
        res = await client.post("/api/meeting-point", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["reachable"] is False
        assert data["reason"] == "no_candidate_within_budget"


@pytest.mark.asyncio
async def test_config_exposes_public_token(monkeypatch):
    monkeypatch.setenv("MAPBOX_PUBLIC_TOKEN", "pk.public-token")
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/config.js")

    assert res.status_code == 200
    assert 'window.MAPBOX_TOKEN = "pk.public-token";' in res.text
    assert res.headers["cache-control"] == "no-store"
    assert "sk.secret-token" not in res.text


@pytest.mark.asyncio
async def test_config_does_not_expose_secret_token(monkeypatch):
    monkeypatch.delenv("MAPBOX_PUBLIC_TOKEN", raising=False)
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/config.js")

    assert res.status_code == 200
    assert "window.MAPBOX_TOKEN = null;" in res.text
    assert "sk.secret-token" not in res.text


@pytest.mark.asyncio
async def test_config_allows_public_token_from_mapbox_token(monkeypatch):
    monkeypatch.delenv("MAPBOX_PUBLIC_TOKEN", raising=False)
    monkeypatch.setenv("MAPBOX_TOKEN", "pk.public-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/config.js")

    assert res.status_code == 200
    assert 'window.MAPBOX_TOKEN = "pk.public-token";' in res.text


@pytest.mark.asyncio
async def test_config_ignores_secret_public_token(monkeypatch):
    monkeypatch.setenv("MAPBOX_PUBLIC_TOKEN", "sk.wrong-place-secret")
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.server-secret")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/config.js")

    assert res.status_code == 200
    assert "window.MAPBOX_TOKEN = null;" in res.text
    assert "wrong-place-secret" not in res.text
    assert "server-secret" not in res.text


@pytest.mark.asyncio
async def test_config_escapes_public_token(monkeypatch):
    public_value = 'pk.public"; window.LEAKED = true; //'
    monkeypatch.setenv("MAPBOX_PUBLIC_TOKEN", public_value)
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.secret-token")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/config.js")

    raw_value = res.text.removeprefix("window.MAPBOX_TOKEN = ").removesuffix(";\n")
    assert json.loads(raw_value) == public_value
    assert res.text == f"window.MAPBOX_TOKEN = {json.dumps(public_value)};\n"


@pytest.mark.asyncio
async def test_request_id_header_is_echoed():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/health", headers={"x-request-id": "test-request-id"})

    assert res.status_code == 200
    assert res.headers["x-request-id"] == "test-request-id"


@pytest.mark.asyncio
async def test_security_headers_are_set():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/health")

    assert res.status_code == 200
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert res.headers["permissions-policy"] == "geolocation=(self)"


@pytest.mark.asyncio
async def test_metrics_records_http_and_meeting_point_activity():
    main._metrics.clear()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        search = await client.post("/api/meeting-point", json={"addresses": ["A st", "B st"]})
        metrics = await client.get("/metrics")

    assert search.status_code == 200
    assert metrics.status_code == 200
    body = metrics.text
    assert "# TYPE isochrone_http_requests_total counter" in body
    assert 'isochrone_http_requests_total{method="POST",path="/api/meeting-point",status="200"} 1' in body
    assert (
        'isochrone_http_request_duration_seconds_count{method="POST",path="/api/meeting-point",status="200"} 1' in body
    )
    assert 'isochrone_meeting_point_searches_total{outcome="reachable"} 1' in body


@pytest.mark.asyncio
async def test_ready_requires_mapbox_token(monkeypatch):
    monkeypatch.delenv("MAPBOX_TOKEN", raising=False)
    monkeypatch.delenv("MAPBOX_PUBLIC_TOKEN", raising=False)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/ready")

    assert res.status_code == 503
    assert res.json() == {
        "status": "missing_mapbox_token",
        "mapbox_token_configured": False,
        "mapbox_public_token_configured": False,
    }


@pytest.mark.asyncio
async def test_ready_reports_configured_tokens(monkeypatch):
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.server-secret")
    monkeypatch.setenv("MAPBOX_PUBLIC_TOKEN", "pk.public-token")
    monkeypatch.setattr(main, "REQUIRE_MAPBOX_PUBLIC_TOKEN", True)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/ready")

    assert res.status_code == 200
    assert res.json() == {
        "status": "ready",
        "mapbox_token_configured": True,
        "mapbox_public_token_configured": True,
    }


@pytest.mark.asyncio
async def test_ready_requires_public_token_by_default(monkeypatch):
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.server-secret")
    monkeypatch.delenv("MAPBOX_PUBLIC_TOKEN", raising=False)
    monkeypatch.setattr(main, "REQUIRE_MAPBOX_PUBLIC_TOKEN", True)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/ready")

    assert res.status_code == 503
    assert res.json() == {
        "status": "missing_mapbox_public_token",
        "mapbox_token_configured": True,
        "mapbox_public_token_configured": False,
    }


@pytest.mark.asyncio
async def test_ready_allows_api_only_public_token_override(monkeypatch):
    monkeypatch.setenv("MAPBOX_TOKEN", "sk.server-secret")
    monkeypatch.delenv("MAPBOX_PUBLIC_TOKEN", raising=False)
    monkeypatch.setattr(main, "REQUIRE_MAPBOX_PUBLIC_TOKEN", False)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/ready")

    assert res.status_code == 200
    assert res.json() == {
        "status": "ready",
        "mapbox_token_configured": True,
        "mapbox_public_token_configured": False,
    }


def test_request_normalizes_addresses_and_city_hint():
    payload = main.MeetingPointRequest(addresses=["  A st  "], city_hint="   ")

    assert payload.addresses == ["A st"]
    assert payload.city_hint is None


@pytest.mark.asyncio
async def test_rejects_overlong_address():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/meeting-point",
            json={"addresses": ["x" * (main.MAX_ADDRESS_CHARS + 1)]},
        )

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_rejects_too_fine_grid_resolution():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/meeting-point",
            json={
                "addresses": ["A st", "B st"],
                "use_grid_search": True,
                "grid_resolution_m": main.MIN_GRID_RESOLUTION_M - 1,
            },
        )

    assert res.status_code == 422


@pytest.mark.asyncio
async def test_oversized_meeting_point_request_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 8)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/meeting-point",
            json={"addresses": ["A st", "B st"]},
        )

    assert res.status_code == 413
    assert res.json()["detail"] == "Request body must be 8 bytes or fewer"


@pytest.mark.asyncio
async def test_oversized_streaming_request_without_content_length_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 20)

    async def body_chunks():
        yield b'{"addresses":'
        yield b'["A st","B st"]}'

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/meeting-point",
            content=body_chunks(),
            headers={"content-type": "application/json"},
        )

    assert res.status_code == 413
    assert res.json()["detail"] == "Request body must be 20 bytes or fewer"


@pytest.mark.asyncio
async def test_streaming_request_without_content_length_can_pass(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 128)

    async def body_chunks():
        yield b'{"addresses":'
        yield b'["A st","B st"]}'

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post(
            "/api/meeting-point",
            content=body_chunks(),
            headers={"content-type": "application/json"},
        )

    assert res.status_code == 200
    assert res.json()["reachable"] is True


@pytest.mark.asyncio
async def test_meeting_point_timeout_returns_504(monkeypatch):
    monkeypatch.setattr(main, "MEETING_POINT_TIMEOUT_SECONDS", 0.001)
    main._metrics.clear()

    async def slow_compute(payload):
        await asyncio.sleep(1)
        raise AssertionError("timeout should cancel the search")

    monkeypatch.setattr(main, "_compute_meeting_point", slow_compute)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post("/api/meeting-point", json={"addresses": ["A st", "B st"]})

    assert res.status_code == 504
    assert res.json()["detail"] == "Meeting-point search timed out. Try again shortly."
    assert 'isochrone_meeting_point_searches_total{outcome="timeout"} 1' in main._metrics_text()


@pytest.mark.asyncio
async def test_rate_limits_meeting_point_searches(monkeypatch):
    monkeypatch.setattr(main, "MEETING_POINT_RATE_LIMIT", 1)
    monkeypatch.setattr(main, "MEETING_POINT_RATE_WINDOW_SECONDS", 60.0)
    main._rate_limit_buckets.clear()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = {"addresses": ["A st", "B st"], "max_minutes": 15}
        first = await client.post("/api/meeting-point", json=payload)
        second = await client.post("/api/meeting-point", json=payload)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert second.json()["detail"] == "Too many meeting-point searches. Try again shortly."


@pytest.mark.asyncio
async def test_rate_limit_only_applies_to_costly_endpoint(monkeypatch):
    monkeypatch.setattr(main, "MEETING_POINT_RATE_LIMIT", 0)
    main._rate_limit_buckets.clear()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.get("/health")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_meeting_point_uses_search_semaphore(monkeypatch):
    class CountingSemaphore:
        entered = 0
        exited = 0

        async def __aenter__(self):
            self.entered += 1
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            self.exited += 1

    semaphore = CountingSemaphore()
    monkeypatch.setattr(main.app.state, "search_semaphore", semaphore, raising=False)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post("/api/meeting-point", json={"addresses": ["A st", "B st"]})

    assert res.status_code == 200
    assert semaphore.entered == 1
    assert semaphore.exited == 1


def test_geocode_cache_is_bounded_and_normalized():
    main._geocode_cache.clear()
    assert main._geocode_cache_key(" 123 Main St ", "Atlanta, GA") == "123 main st|atlanta, ga"

    for idx in range(main.MAX_GEOCODE_CACHE_SIZE + 2):
        main._cache_geocode_result(f"key-{idx}", (float(idx), float(idx)))

    assert len(main._geocode_cache) == main.MAX_GEOCODE_CACHE_SIZE
    assert "key-0" not in main._geocode_cache
    assert "key-1" not in main._geocode_cache
    assert "key-2" in main._geocode_cache
