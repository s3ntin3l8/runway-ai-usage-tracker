import pytest

from app.core.rate_limit import limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi in-memory counters before each integration test.

    All integration tests share the same app singleton, so rate-limit buckets
    accumulate across tests.  Without this reset, endpoints with tight limits
    (e.g. 10/minute) become 429 by the time later tests reach them.
    """
    limiter.reset()
