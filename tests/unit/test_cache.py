"""Unit tests for the process-wide TTL response cache (app/core/cache.py)."""

import app.core.cache as cache_mod

cache_clear = cache_mod.cache_clear
cache_get = cache_mod.cache_get
cache_set = cache_mod.cache_set


def test_get_missing_key_returns_none():
    assert cache_get("nope") is None


def test_set_then_get_returns_value():
    cache_set("k", {"a": 1}, ttl_seconds=60.0)
    assert cache_get("k") == {"a": 1}


def test_entry_expires_after_ttl(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: t[0])

    cache_set("k", "value", ttl_seconds=10.0)
    assert cache_get("k") == "value"

    t[0] += 10.0  # exactly at expiry — treated as expired (>=)
    assert cache_get("k") is None


def test_entry_still_valid_just_before_ttl(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: t[0])

    cache_set("k", "value", ttl_seconds=10.0)
    t[0] += 9.999
    assert cache_get("k") == "value"


def test_expired_entry_is_evicted_not_just_hidden(monkeypatch):
    """A read past expiry removes the entry rather than leaving it to linger."""
    t = [1000.0]
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: t[0])

    cache_set("k", "value", ttl_seconds=5.0)
    t[0] += 5.0
    assert cache_get("k") is None
    assert "k" not in cache_mod._store


def test_cache_clear_drops_everything():
    cache_set("a", 1, ttl_seconds=60.0)
    cache_set("b", 2, ttl_seconds=60.0)
    cache_clear()
    assert cache_get("a") is None
    assert cache_get("b") is None


def test_independent_keys_do_not_collide():
    cache_set("fleet", {"x": 1}, ttl_seconds=60.0)
    cache_set("global-stats", {"y": 2}, ttl_seconds=60.0)
    assert cache_get("fleet") == {"x": 1}
    assert cache_get("global-stats") == {"y": 2}
