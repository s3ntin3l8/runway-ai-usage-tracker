"""Small in-memory TTL cache for expensive, frequently-recomputed read paths.

Not a general-purpose cache — no cross-process sharing (a non-issue for the
single-uvicorn-worker deployment this app runs, see Dockerfile/main.py).
Scoped to endpoints whose underlying data only changes on a slow cadence
(collector poll interval, an admin config write) but that otherwise
recompute an aggregation from scratch on every dashboard load. Keys are
caller-supplied strings, not derived from unhashable arguments like
SQLAlchemy `Session` objects — several callers key on caller-supplied query
params (since/until/days/...), so entry count scales with the number of
distinct param combinations a client requests, not just the number of
endpoints. `_MAX_ENTRIES` plus real LRU eviction (on both read and write)
bounds that growth instead of relying on entries being re-read past their
TTL to get reaped.
"""

import time
from collections import OrderedDict
from threading import Lock
from typing import Any

_MAX_ENTRIES = 256
_store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_lock = Lock()


def cache_get(key: str) -> Any | None:
    """Return the cached value for `key`, or None if missing or expired.

    `None` is never a meaningful cached value for this module's callers
    (they all cache dicts / Pydantic response models), so callers can treat
    a `None` return as an unconditional cache miss.
    """
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del _store[key]
            return None
        _store.move_to_end(key)
        return value


def cache_set(key: str, value: Any, ttl_seconds: float) -> None:
    with _lock:
        _store[key] = (time.monotonic() + ttl_seconds, value)
        _store.move_to_end(key)
        while len(_store) > _MAX_ENTRIES:
            _store.popitem(last=False)  # evict least-recently-used


def cache_clear() -> None:
    """Drop every cached entry — used when an admin write invalidates
    assumptions baked into cached responses (e.g. the user's timezone,
    which shifts period-boundary math for /fleet, /cumulative, etc.)."""
    with _lock:
        _store.clear()
