import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app
from app.models.db import UsageSnapshot, UsageSnapshotModel


# Create a temporary database for testing
@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        yield session

    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_get_history_empty(client: TestClient):
    response = client.get("/api/v1/usage/history")
    assert response.status_code == 200
    data = response.json()
    assert data == {"averages": [], "peaks": []}


def test_get_history_with_data(client: TestClient, session: Session):
    # Add some dummy data
    now = datetime.now(UTC)
    s1 = UsageSnapshot(
        provider_id="anthropic",
        account_id="user1",
        service_name="Claude Pro",
        health="good",
        data_source="api",
        timestamp=now,
    )
    s2 = UsageSnapshot(
        provider_id="openai",
        account_id="user2",
        service_name="ChatGPT Plus",
        health="warning",
        data_source="api",
        timestamp=now - timedelta(hours=1),
    )
    session.add(s1)
    session.add(s2)
    session.commit()

    response = client.get("/api/v1/usage/history")
    assert response.status_code == 200
    data = response.json()
    assert len(data["averages"]) == 2
    assert data["averages"][0]["provider_id"] == "anthropic"
    assert "windows" in data["averages"][0]
    assert data["averages"][1]["provider_id"] == "openai"
    assert "windows" in data["averages"][1]


def test_get_history_filtering(client: TestClient, session: Session):
    now = datetime.now(UTC)
    s1 = UsageSnapshot(
        provider_id="anthropic",
        account_id="user1",
        service_name="Claude Pro",
        health="good",
        data_source="api",
        timestamp=now,
    )
    s2 = UsageSnapshot(
        provider_id="openai",
        account_id="user2",
        service_name="ChatGPT Plus",
        health="warning",
        data_source="api",
        timestamp=now,
    )
    session.add(s1)
    session.add(s2)
    session.commit()

    response = client.get("/api/v1/usage/history?provider_id=anthropic")
    data = response.json()
    assert len(data["averages"]) == 1
    assert data["averages"][0]["provider_id"] == "anthropic"


def test_get_history_limit(client: TestClient, session: Session):
    now = datetime.now(UTC)
    # Space across distinct hours so hourly bucketing doesn't collapse them.
    for i in range(10):
        s = UsageSnapshot(
            provider_id="test",
            account_id="user1",
            service_name=f"Service {i}",
            health="good",
            data_source="api",
            timestamp=now - timedelta(hours=i),
        )
        session.add(s)
    session.commit()

    response = client.get("/api/v1/usage/history?limit=5&days=1")
    data = response.json()
    assert len(data["averages"]) == 5


def test_get_history_multi_day_not_truncated_by_limit(client: TestClient, session: Session):
    """Regression: high-volume today must not push older days out of the response.

    With a flat `ORDER BY timestamp DESC LIMIT N`, a day with >N snapshots consumes
    the whole budget and the caller never sees older days. Server-side hourly
    bucketing per (provider, account, model, window, unit) must preserve coverage
    across the full time window.
    """
    # Use noon UTC to ensure subtracting ~10 mins (600s) doesn't leak into the
    # previous calendar day if run shortly after midnight.
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    # 3 days × 600 rows/day, all same group key within each hour — real pollers
    # produce many rows per hour for the same card (different values, same key).
    # Use seconds (not minutes) so 600 rows span ~10 minutes, never crossing into
    # a neighbouring calendar day regardless of what hour the test runs.
    for day in range(3):
        for i in range(600):
            s = UsageSnapshot(
                provider_id="anthropic",
                account_id="user1",
                model_id="claude-sonnet",
                window_type="5hr_limit",
                unit_type="percent",
                used_value=float(i % 100),
                service_name="Claude",
                health="good",
                data_source="api",
                timestamp=now - timedelta(days=day, seconds=i),
            )
            session.add(s)
    session.commit()

    response = client.get("/api/v1/usage/history?days=7&limit=500")
    assert response.status_code == 200
    data = response.json()

    # Each day should contribute roughly ~10 hourly buckets (600 rows spread over
    # ~10 hours). All 3 days must appear — this is the bug being regressed.
    days_present = {row["timestamp"][:10] for row in data["averages"]}
    assert len(days_present) == 3, (
        f"Expected rows from 3 distinct days, got {len(days_present)}: {days_present}"
    )


def test_get_history_bucket_size_adapts_to_window(client: TestClient, session: Session):
    """Short windows must return sub-hour resolution.

    Inserts minute-spaced snapshots for the past hour. A 1h window (days=0.042)
    should preserve multiple points (5-min buckets → ~12 points); a 7d window
    would collapse them to one hourly/daily bucket.
    """
    now = datetime.now(UTC)
    for i in range(60):
        s = UsageSnapshot(
            provider_id="anthropic",
            account_id="user1",
            model_id="claude-sonnet",
            window_type="5hr_limit",
            unit_type="percent",
            used_value=float(i),
            service_name="Claude",
            health="good",
            data_source="api",
            timestamp=now - timedelta(minutes=i),
        )
        session.add(s)
    session.commit()

    # 1h view: 5-minute buckets → ~12 rows (5 min-spaced snapshots per bucket)
    r1h = client.get("/api/v1/usage/history?days=0.042&limit=500").json()
    assert 10 <= len(r1h["averages"]) <= 14, (
        f"1h window expected ~12 points (5-min buckets), got {len(r1h['averages'])}"
    )

    # 6h view: 15-minute buckets → expect ~4 rows
    r6h = client.get("/api/v1/usage/history?days=0.25&limit=500").json()
    assert 3 <= len(r6h["averages"]) <= 5, (
        f"6h window expected ~4 points, got {len(r6h['averages'])}"
    )

    # 1d view: 30-min buckets → 60min span crosses at most 3 half-hour boundaries
    r1d = client.get("/api/v1/usage/history?days=1&limit=500").json()
    assert 1 <= len(r1d["averages"]) <= 3, (
        f"1d window expected 1-3 points (30-min buckets), got {len(r1d['averages'])}"
    )


def test_get_history_includes_by_model(client: TestClient, session: Session):
    """History response includes aggregated by_model breakdown per bucket."""
    now = datetime.now(UTC)

    # Create a snapshot
    snap = UsageSnapshot(
        provider_id="gemini",
        account_id="user1",
        service_name="Gemini Advanced",
        health="good",
        data_source="api",
        timestamp=now,
        used_value=50.0,
        limit_value=100.0,
        unit_type="percent",
        window_type="session",
    )
    session.add(snap)
    session.flush()  # Get snap.id

    # Create per-model records
    session.add(
        UsageSnapshotModel(
            snapshot_id=snap.id,
            model_id="flash",
            cost=0.30,
            msgs=3,
            tokens_input=1200.0,
            tokens_output=800.0,
            tokens_total=2000.0,
        )
    )
    session.add(
        UsageSnapshotModel(
            snapshot_id=snap.id,
            model_id="pro",
            cost=0.15,
            msgs=1,
            tokens_input=500.0,
            tokens_output=300.0,
            tokens_total=800.0,
        )
    )
    session.commit()

    response = client.get("/api/v1/usage/history?days=1")
    assert response.status_code == 200
    data = response.json()

    assert len(data["averages"]) == 1
    row = data["averages"][0]
    # by_model is now INSIDE each window
    assert "windows" in row
    window = row["windows"][0]
    assert "by_model" in window
    assert window["by_model"] is not None
    assert len(window["by_model"]) == 2

    by_model = {m["model_id"]: m for m in window["by_model"]}
    assert by_model["flash"]["cost"] == 0.30
    assert by_model["flash"]["msgs"] == 3
    assert by_model["flash"]["tokens_total"] == 2000.0
    assert by_model["pro"]["cost"] == 0.15
    assert by_model["pro"]["msgs"] == 1


def test_get_history_deltas_computes_positive_deltas(client: TestClient, session: Session):
    """Positive deltas sum actual consumption; resets (negative jumps) are ignored."""
    now = datetime.now(UTC)
    base = now - timedelta(hours=4)

    # One series: tokens climb, reset, then climb again
    tokens = [1000, 1500, 2000, 0, 300]
    for i, tok in enumerate(tokens):
        session.add(
            UsageSnapshot(
                provider_id="anthropic",
                account_id="user1",
                service_name="Claude",
                health="good",
                data_source="api",
                timestamp=base + timedelta(minutes=i * 10),
                tokens_total=float(tok),
                unit_type="tokens",
                window_type="session",
            )
        )
    session.commit()

    response = client.get("/api/v1/usage/history/deltas?days=1")
    assert response.status_code == 200
    data = response.json()

    # Expected deltas: 
    # [1000] -> Baseline
    # [1500] -> +500
    # [2000] -> +500
    # [0]    -> Glitch to zero (ignored)
    # [300]  -> Still < 2000 (ignored recovery)
    # Total: 1000.0
    assert data["token_delta_total"] == 1000.0
    assert data["provider_token_deltas"]["anthropic"] == 1000.0
    assert data["critical_series_count"] == 0
    assert data["series_sampled"] is False
    assert len(data["series"]) == 1
    assert data["series"][0]["token_delta"] == 1000.0
    assert data["series"][0]["cost_delta"] == 0.0


def test_get_history_deltas_filters_by_provider(client: TestClient, session: Session):
    """Provider filter isolates deltas to matching rows."""
    now = datetime.now(UTC)

    for i in range(3):
        # Insert chronologically ascending (oldest first) so deltas are positive
        session.add(
            UsageSnapshot(
                provider_id="anthropic",
                account_id="a1",
                service_name="Claude",
                health="good",
                data_source="api",
                timestamp=now - timedelta(minutes=(2 - i) * 10),
                tokens_total=float((i + 1) * 1000),
                unit_type="tokens",
                window_type="session",
            )
        )
        session.add(
            UsageSnapshot(
                provider_id="openai",
                account_id="o1",
                service_name="ChatGPT",
                health="good",
                data_source="api",
                timestamp=now - timedelta(minutes=(2 - i) * 10),
                tokens_total=float((i + 1) * 500),
                unit_type="tokens",
                window_type="session",
            )
        )
    session.commit()

    response = client.get("/api/v1/usage/history/deltas?days=1&provider_id=anthropic")
    assert response.status_code == 200
    data = response.json()

    # Anthropic deltas: 1000 + 1000 = 2000
    assert data["token_delta_total"] == 2000.0
    assert "anthropic" in data["provider_token_deltas"]
    assert "openai" not in data["provider_token_deltas"]
    assert len(data["series"]) == 1


def test_get_history_deltas_counts_critical_series(client: TestClient, session: Session):
    """Critical flag is per-series (any reading >= 90%), not per-row."""
    now = datetime.now(UTC)

    # Series A: peaks at 92% (critical)
    for i, pct in enumerate([50.0, 75.0, 92.0, 80.0]):
        session.add(
            UsageSnapshot(
                provider_id="anthropic",
                account_id="user1",
                service_name="Claude",
                health="good",
                data_source="api",
                timestamp=now - timedelta(minutes=i * 10),
                used_value=pct,
                limit_value=100.0,
                unit_type="percent",
                window_type="session",
            )
        )

    # Series B: stays at 70% (not critical)
    for i in range(3):
        session.add(
            UsageSnapshot(
                provider_id="openai",
                account_id="user2",
                service_name="ChatGPT",
                health="good",
                data_source="api",
                timestamp=now - timedelta(minutes=i * 10),
                used_value=70.0,
                limit_value=100.0,
                unit_type="percent",
                window_type="session",
            )
        )
    session.commit()

    response = client.get("/api/v1/usage/history/deltas?days=1")
    assert response.status_code == 200
    data = response.json()

    assert data["critical_series_count"] == 1
    anthropic_series = next(s for s in data["series"] if s["provider_id"] == "anthropic")
    assert anthropic_series["critical"] is True
    openai_series = next(s for s in data["series"] if s["provider_id"] == "openai")
    assert openai_series["critical"] is False
