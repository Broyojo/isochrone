import httpx
import pytest
from fastapi import HTTPException

import main


@pytest.mark.asyncio
async def test_mapbox_get_retries_transient_status(monkeypatch):
    attempts = 0
    main._metrics.clear()

    async def no_sleep(_seconds: float) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await main._mapbox_get(
            client,
            "https://api.mapbox.test/example",
            params={},
            service="test",
            failure_detail="Test request failed",
        )

    assert response.status_code == 200
    assert attempts == 2
    metrics = main._metrics_text()
    assert 'isochrone_mapbox_requests_total{service="test",status="503"} 1' in metrics
    assert 'isochrone_mapbox_requests_total{service="test",status="200"} 1' in metrics
    assert 'isochrone_mapbox_retries_total{reason="503",service="test"} 1' in metrics


@pytest.mark.asyncio
async def test_matrix_response_dimensions_are_validated(monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"durations": [[60.0]]})

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    access = "unit-test"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as exc_info:
            await main._matrix_durations_seconds(
                client,
                origins=[(0.0, 0.0), (1.0, 1.0)],
                destinations=[(2.0, 2.0)],
                profile="driving",
                token=access,
            )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Matrix response had unexpected dimensions"


@pytest.mark.asyncio
async def test_mapbox_invalid_json_returns_bad_gateway():
    access_value = "unit-test"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as exc_info:
            await main._matrix_durations_seconds(
                client,
                origins=[(0.0, 0.0)],
                destinations=[(1.0, 1.0)],
                profile="driving",
                token=access_value,
            )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Mapbox matrix returned invalid JSON"


@pytest.mark.asyncio
async def test_geocoding_malformed_coordinates_return_bad_gateway():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"features": [{"geometry": {"coordinates": ["not-a-number", 33.0]}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as exc_info:
            await main._geocode_address(client, "A st", None, "unit-test")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Geocoding response had invalid coordinates"


@pytest.mark.asyncio
async def test_isochrone_invalid_geometry_returns_bad_gateway():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": [{"geometry": {"type": "Nope", "coordinates": []}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as exc_info:
            await main._fetch_isochrone(client, (0.0, 0.0), 15, "driving", "unit-test")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Isochrone response contained invalid geometry"


@pytest.mark.asyncio
async def test_mapbox_request_error_does_not_leak_access_token(monkeypatch, caplog):
    async def no_sleep(_seconds: float) -> None:
        return None

    access_value = "sk.secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not connect to {request.url}", request=request)

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("WARNING", logger="isochrone"):
            with pytest.raises(HTTPException) as exc_info:
                await main._mapbox_get(
                    client,
                    "https://api.mapbox.test/example",
                    params={"access_token": access_value},
                    service="test",
                    failure_detail="Test request failed",
                )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Mapbox test request failed"
    assert access_value not in str(exc_info.value.detail)
    assert access_value not in caplog.text
    assert "access_token" not in caplog.text
    assert "ConnectError" in caplog.text
