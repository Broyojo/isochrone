from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass

import httpx

REQUIRED_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=(self)",
}


class SmokeFailureError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmokeOptions:
    base_url: str
    allow_not_ready: bool = False
    expect_public_token: bool = False
    timeout_seconds: float = 10.0
    attempts: int = 1
    retry_delay_seconds: float = 1.0


def _require(*, condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailureError(message)


def _json_response(response: httpx.Response, endpoint: str) -> dict[str, object]:
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise SmokeFailureError(f"{endpoint} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise SmokeFailureError(f"{endpoint} returned non-object JSON")
    return data


def _check_security_headers(response: httpx.Response, endpoint: str) -> None:
    for header, expected in REQUIRED_SECURITY_HEADERS.items():
        actual = response.headers.get(header)
        _require(
            condition=actual == expected,
            message=f"{endpoint} missing {header}: expected {expected!r}, got {actual!r}",
        )


def _check_public_token(config_body: str) -> None:
    _require(
        condition=config_body.startswith('window.MAPBOX_TOKEN = "pk.'),
        message="config.js did not expose a public pk Mapbox token",
    )
    _require(condition="sk." not in config_body, message="config.js exposed a secret sk Mapbox token")


async def _run_smoke_once(
    options: SmokeOptions,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    checks: list[str] = []
    async with httpx.AsyncClient(
        base_url=options.base_url,
        follow_redirects=False,
        timeout=options.timeout_seconds,
        transport=transport,
    ) as client:
        health = await client.get("/health")
        _require(condition=health.status_code == 200, message=f"/health returned {health.status_code}")
        _check_security_headers(health, "/health")
        _require(
            condition=_json_response(health, "/health").get("status") == "ok",
            message="/health did not report ok",
        )
        checks.append("health")

        ready = await client.get("/ready")
        if options.allow_not_ready:
            _require(condition=ready.status_code in {200, 503}, message=f"/ready returned {ready.status_code}")
        else:
            _require(condition=ready.status_code == 200, message=f"/ready returned {ready.status_code}")
            _require(
                condition=_json_response(ready, "/ready").get("status") == "ready",
                message="/ready did not report ready",
            )
        checks.append("ready")

        config = await client.get("/config.js")
        _require(condition=config.status_code == 200, message=f"/config.js returned {config.status_code}")
        _require(condition=config.headers.get("cache-control") == "no-store", message="/config.js is not no-store")
        _require(condition="sk." not in config.text, message="config.js exposed a secret sk Mapbox token")
        if options.expect_public_token:
            _check_public_token(config.text)
        checks.append("config")

        index = await client.get("/")
        _require(condition=index.status_code == 200, message=f"/ returned {index.status_code}")
        _require(condition="Meeting Point Finder" in index.text, message="/ did not look like the app shell")
        checks.append("index")

        metrics = await client.get("/metrics")
        _require(condition=metrics.status_code == 200, message=f"/metrics returned {metrics.status_code}")
        _require(
            condition="# TYPE isochrone_http_requests_total counter" in metrics.text,
            message="/metrics did not include HTTP request counters",
        )
        checks.append("metrics")

    return checks


async def run_smoke(
    options: SmokeOptions,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    attempts = max(1, options.attempts)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _run_smoke_once(options, transport=transport)
        except (SmokeFailureError, httpx.HTTPError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            await asyncio.sleep(max(0.0, options.retry_delay_seconds))

    if last_error is None:
        raise SmokeFailureError("Smoke failed without an error")
    raise last_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke test a running Meeting Point Finder deployment.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Deployment base URL.")
    parser.add_argument("--allow-not-ready", action="store_true", help="Allow /ready to return 503.")
    parser.add_argument("--expect-public-token", action="store_true", help="Require config.js to expose a pk token.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    parser.add_argument("--attempts", type=int, default=1, help="Number of smoke attempts before failing.")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Delay between smoke attempts in seconds.")
    return parser


async def _async_main() -> int:
    args = _parser().parse_args()
    options = SmokeOptions(
        base_url=args.base_url,
        allow_not_ready=args.allow_not_ready,
        expect_public_token=args.expect_public_token,
        timeout_seconds=args.timeout,
        attempts=args.attempts,
        retry_delay_seconds=args.retry_delay,
    )
    try:
        checks = await run_smoke(options)
    except (SmokeFailureError, httpx.HTTPError) as exc:
        sys.stderr.write(f"Smoke failed: {exc}\n")
        return 1

    sys.stdout.write(f"Smoke passed: {', '.join(checks)}\n")
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
