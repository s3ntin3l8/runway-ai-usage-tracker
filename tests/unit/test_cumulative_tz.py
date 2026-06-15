"""Local-timezone handling for the "This period" / "Yearly" cumulative gauges.

Regression coverage for the bug where the monthly cumulative card did not
reset at the user's local month boundary: at 01:49 Europe/Berlin on June 1
(= 23:49 UTC May 31) the card still showed May because every layer bucketed
in UTC. The fix resolves the user timezone server-side, anchors the period at
the local boundary, and aggregates "this period" live from usage_events.

These tests freeze a fixed instant (no wall-clock dependence) and seed events
straddling the local midnight.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api.endpoints.usage import _local_period_anchors
from app.core.utils import resolve_user_tz
from app.models.db import SystemConfig, UsageEvent
from app.services.queries import query_cumulative_live

# 01:49 Europe/Berlin on June 1 (UTC+2 in summer) == 23:49 UTC on May 31.
_BERLIN_BOUNDARY_UTC = datetime(2026, 5, 31, 23, 49, 0, tzinfo=UTC)


def _engine():
    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    return e


def _session() -> Session:
    return Session(_engine())


def _add_event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    provider_id: str = "anthropic",
    account_id: str = "user@example.com",
    model_id: str | None = "sonnet",
    sidecar_id: str = "dev-01",
    tokens_input: int = 100,
    tokens_output: int = 200,
    tokens_cache_read: int = 10,
    tokens_cache_create: int = 5,
    tokens_reasoning: int = 3,
    cost_usd: float = 0.01,
    cost_input: float = 0.0,
    cost_output: float = 0.0,
    cost_cache_read: float = 0.0,
    cost_cache_create: float = 0.0,
    kind: str = "message",
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
            event_id=event_id,
            ts=ts,
            kind=kind,
            model_id=model_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_create=tokens_cache_create,
            tokens_reasoning=tokens_reasoning,
            cost_usd=cost_usd,
            cost_input=cost_input,
            cost_output=cost_output,
            cost_cache_read=cost_cache_read,
            cost_cache_create=cost_cache_create,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# resolve_user_tz
# ---------------------------------------------------------------------------


def test_resolve_user_tz_from_system_config():
    session = _session()
    session.add(SystemConfig(id=1, user_timezone="Europe/Berlin"))
    session.commit()
    assert resolve_user_tz(session) == ZoneInfo("Europe/Berlin")


def test_resolve_user_tz_falls_back_to_env_tz(monkeypatch):
    # No SystemConfig row → the TZ env var (settings.env_timezone) is used.
    from app.core.config import settings

    monkeypatch.setattr(settings, "TZ", "Asia/Tokyo")
    assert resolve_user_tz(_session()) == ZoneInfo("Asia/Tokyo")


def test_resolve_user_tz_defaults_to_utc_when_nothing_set(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TZ", None)
    assert resolve_user_tz(_session()) == ZoneInfo("UTC")


def test_resolve_user_tz_config_overrides_env(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TZ", "Asia/Tokyo")
    session = _session()
    session.add(SystemConfig(id=1, user_timezone="Europe/Berlin"))
    session.commit()
    assert resolve_user_tz(session) == ZoneInfo("Europe/Berlin")


def test_resolve_user_tz_invalid_falls_back_to_utc(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TZ", None)
    session = _session()
    session.add(SystemConfig(id=1, user_timezone="Not/AZone"))
    session.commit()
    assert resolve_user_tz(session) == ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# _local_period_anchors — the boundary computation
# ---------------------------------------------------------------------------


def test_anchors_berlin_rolls_over_at_local_midnight():
    a = _local_period_anchors(ZoneInfo("Europe/Berlin"), now=_BERLIN_BOUNDARY_UTC)
    # It is already June 1 in Berlin even though it is still May 31 in UTC.
    assert a["current_month"] == "2026-06"
    assert a["current_year"] == "2026"
    # Local June 1 00:00 Berlin (UTC+2) == May 31 22:00 UTC.
    assert a["month_start_utc"] == datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
    # Local Jan 1 00:00 Berlin (UTC+1 in winter) == Dec 31 23:00 UTC.
    assert a["year_start_utc"] == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)


def test_anchors_utc_still_in_may_at_same_instant():
    a = _local_period_anchors(ZoneInfo("UTC"), now=_BERLIN_BOUNDARY_UTC)
    assert a["current_month"] == "2026-05"
    assert a["month_start_utc"] == datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# query_cumulative_live — the live aggregation
# ---------------------------------------------------------------------------


def test_live_month_counts_post_local_midnight_activity():
    """The crux: an event after local midnight but before UTC midnight must
    land in the new local month. The UTC rollup would have left it in May."""
    session = _session()
    tz = ZoneInfo("Europe/Berlin")
    anchors = _local_period_anchors(tz, now=_BERLIN_BOUNDARY_UTC)
    month_start = anchors["month_start_utc"]

    # Before the local month → excluded.
    _add_event(session, "a", datetime(2026, 5, 31, 20, 0, tzinfo=UTC), tokens_input=1000)
    # After local June midnight (= 01:00 Berlin) but still May 31 in UTC → included.
    _add_event(session, "b", datetime(2026, 5, 31, 23, 0, tzinfo=UTC), tokens_input=7)

    live = query_cumulative_live(session, since=month_start)
    bucket = live[("anthropic", "user@example.com")]
    assert bucket["tokens_input"] == 7  # only event "b"
    assert bucket["msgs"] == 1


def test_live_groups_by_identity_model_and_sidecar():
    session = _session()
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    ts = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    _add_event(session, "1", ts, model_id="sonnet", sidecar_id="dev-01", tokens_input=100)
    _add_event(session, "2", ts, model_id="opus", sidecar_id="dev-02", tokens_input=50)
    _add_event(session, "3", ts, account_id="other@example.com", model_id="opus", tokens_input=9)

    live = query_cumulative_live(session, since=since)

    primary = live[("anthropic", "user@example.com")]
    assert primary["tokens_input"] == 150  # 100 + 50
    assert primary["by_model"]["sonnet"]["tokens_input"] == 100
    assert primary["by_model"]["opus"]["tokens_input"] == 50
    assert primary["by_sidecar"]["dev-01"]["tokens_input"] == 100
    assert primary["by_sidecar"]["dev-02"]["tokens_input"] == 50
    # Separate identity is its own bucket.
    assert live[("anthropic", "other@example.com")]["tokens_input"] == 9


def test_live_exposes_cost_components_per_grain():
    """Per-component cost (input/output/cache_read/cache_create) flows through to
    the totals and the by_model / by_sidecar grains, with cost_cache = the cache
    pair, so the cost-breakdown views can split a row by token category."""
    session = _session()
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    ts = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    _add_event(
        session,
        "1",
        ts,
        model_id="sonnet",
        sidecar_id="dev-01",
        cost_usd=0.10,
        cost_input=0.04,
        cost_output=0.03,
        cost_cache_read=0.02,
        cost_cache_create=0.01,
    )

    live = query_cumulative_live(session, since=since)
    bucket = live[("anthropic", "user@example.com")]
    for grain in (bucket, bucket["by_model"]["sonnet"], bucket["by_sidecar"]["dev-01"]):
        assert abs(grain["cost_input"] - 0.04) < 1e-9
        assert abs(grain["cost_output"] - 0.03) < 1e-9
        assert abs(grain["cost_cache_read"] - 0.02) < 1e-9
        assert abs(grain["cost_cache_create"] - 0.01) < 1e-9
        # Back-compat: cost_cache stays the cache_read + cache_create pair.
        assert abs(grain["cost_cache"] - 0.03) < 1e-9


def test_live_excludes_error_events():
    session = _session()
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    ts = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    _add_event(session, "ok", ts, tokens_input=100)
    _add_event(session, "err", ts, kind="error", tokens_input=999)

    live = query_cumulative_live(session, since=since)
    assert live[("anthropic", "user@example.com")]["tokens_input"] == 100
