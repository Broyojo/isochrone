import httpx
import pytest

from scripts import smoke


def _security_headers() -> dict[str, str]:
    return {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(self)",
    }


def _smoke_response(request: httpx.Request, *, missing_security_header: bool = False) -> httpx.Response:
    if request.url.path == "/health":
        headers = _security_headers()
        if missing_security_header:
            headers.pop("X-Frame-Options")
        return httpx.Response(200, json={"status": "ok"}, headers=headers)
    if request.url.path == "/ready":
        return httpx.Response(200, json={"status": "ready"})
    if request.url.path == "/config.js":
        return httpx.Response(
            200,
            text='window.MAPBOX_TOKEN = "pk.test";\n',
            headers={"Cache-Control": "no-store"},
        )
    if request.url.path == "/":
        return httpx.Response(200, text="Meeting Point Finder")
    if request.url.path == "/metrics":
        return httpx.Response(200, text="# TYPE isochrone_http_requests_total counter\n")
    return httpx.Response(404)


def _smoke_transport(*, missing_security_header: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return _smoke_response(request, missing_security_header=missing_security_header)

    return httpx.MockTransport(handler)


def _flaky_health_transport() -> httpx.MockTransport:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/health":
            attempts += 1
            if attempts == 1:
                return httpx.Response(503)
        return _smoke_response(request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_smoke_script_passes_for_running_app_shape():
    checks = await smoke.run_smoke(
        smoke.SmokeOptions(base_url="http://testserver", expect_public_token=True),
        transport=_smoke_transport(),
    )

    assert checks == ["health", "ready", "config", "index", "metrics"]


@pytest.mark.asyncio
async def test_smoke_script_fails_when_security_headers_are_missing():
    with pytest.raises(smoke.SmokeFailureError, match="x-frame-options"):
        await smoke.run_smoke(
            smoke.SmokeOptions(base_url="http://testserver"),
            transport=_smoke_transport(missing_security_header=True),
        )


@pytest.mark.asyncio
async def test_smoke_script_retries_startup_failures():
    checks = await smoke.run_smoke(
        smoke.SmokeOptions(base_url="http://testserver", attempts=2, retry_delay_seconds=0),
        transport=_flaky_health_transport(),
    )

    assert checks == ["health", "ready", "config", "index", "metrics"]
