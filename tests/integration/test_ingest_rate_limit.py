"""POST /api/v1/fleet/ingest must be rate-limited.

Closes audit finding S9. Every other route in fleet.py carries a
`@limiter.limit(...)` decorator; `/ingest` did not. HMAC verification
rejects bad signatures cheaply, but the server still reads the body,
runs constant-time compare, and parses Pydantic on every request — an
attacker with network reach can saturate the ingest worker indefinitely
without ever passing auth.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.rate_limit import limiter
from app.main import (
    app,  # noqa: F401 — imported for slowapi side effects (decorator-time registration)
)

_INGEST_ROUTE_KEY = "app.api.endpoints.fleet.ingest_metrics"


def test_ingest_endpoint_is_registered_with_limiter():
    """Fast smoke check: a `@limiter.limit(...)` decorator must be wired,
    catching a future refactor that drops it before the integration test
    gets to run."""
    limits = limiter._route_limits.get(_INGEST_ROUTE_KEY, [])
    assert limits, (
        f"/api/v1/fleet/ingest has no rate limit registered under "
        f"{_INGEST_ROUTE_KEY!r} in limiter._route_limits"
    )


def test_ingest_returns_429_once_limit_is_exceeded():
    """Behaviour check: slowapi actually returns 429 after the configured
    burst is consumed.

    Tightens the registered limit to 3/minute for the duration of the
    test so saturating it is cheap. The 6 unsigned POSTs trip the rate
    limiter (which runs before HMAC parse), so 429 is observable without
    crafting a valid signature.
    """
    from limits import parse as parse_rate
    from slowapi.wrappers import Limit

    original = list(limiter._route_limits[_INGEST_ROUTE_KEY])
    sample = original[0]
    # Mirror the existing Limit shape; only the rate spec changes.
    tight = Limit(
        limit=parse_rate("3/minute"),
        key_func=sample.key_func,
        scope=sample.scope,
        per_method=sample.per_method,
        methods=sample.methods,
        error_message=sample.error_message,
        exempt_when=sample.exempt_when,
        cost=sample.cost,
        override_defaults=sample.override_defaults,
    )
    limiter._route_limits[_INGEST_ROUTE_KEY] = [tight]
    limiter.reset()

    try:
        client = TestClient(app)
        results = [
            client.post(
                "/api/v1/fleet/ingest",
                content=b"{}",
                headers={
                    "X-Signature": "deadbeef",
                    "X-Timestamp": "0",
                    "Content-Type": "application/json",
                },
            ).status_code
            for _ in range(6)
        ]
    finally:
        limiter._route_limits[_INGEST_ROUTE_KEY] = original
        limiter.reset()

    assert 429 in results, (
        f"expected at least one 429 within 6 requests at a 3/minute limit, "
        f"got status codes {results}"
    )


@pytest.fixture(autouse=True)
def _reset_after():
    yield
    limiter.reset()
