import asyncio
import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from shapely.geometry import box, Polygon

import sys
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main


@pytest.fixture(autouse=True)
def patch_mapbox(monkeypatch):
    # Avoid real Mapbox calls; provide deterministic geometry and times.
    async def fake_geocode(client, address: str, city_hint: str, token: str) -> Tuple[float, float]:
        # Encode address into simple coordinates to ensure uniqueness.
        idx = hash(address) % 10
        return float(idx), float(idx)

    async def fake_isochrone(client, coord, minutes: int, profile: str, token: str):
        # Return a shared 1x1 box so intersections stay non-empty.
        return box(0, 0, 1, 1)

    async def fake_travel_time(client, origin, destination, profile: str, token: str):
        # 10 minutes expressed in seconds.
        return 600.0

    monkeypatch.setattr(main, "_mapbox_token", lambda: "test-token")
    monkeypatch.setattr(main, "_geocode_address", fake_geocode)
    monkeypatch.setattr(main, "_fetch_isochrone", fake_isochrone)
    monkeypatch.setattr(main, "_travel_time_seconds", fake_travel_time)

    http_client = httpx.AsyncClient(timeout=5)
    main.app.state.http_client = http_client

    yield

    asyncio.get_event_loop().run_until_complete(http_client.aclose())


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


def test_polygonal_region_handles_geometry_collection():
    # Ensure helper returns polygon even when mixed geometry types are present.
    p1 = box(0, 0, 1, 1)
    p2 = box(0.5, 0.5, 1.5, 1.5)
    collection = p1.union(p2).buffer(0).boundary.union(p1)  # mixed geometry
    result = main._polygonal_region(collection)
    assert isinstance(result, Polygon)
    assert result.area > 0
