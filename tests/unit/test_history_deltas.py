from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api.endpoints.usage import get_usage_history_deltas
from app.models.db import UsageSnapshot


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _snap(ts, tokens=None, used=None, unit="tokens", provider="anthropic"):
    return UsageSnapshot(
        timestamp=ts,
        provider_id=provider,
        account_id="acc1",
        service_name="Test",
        used_value=used,
        unit_type=unit,
        tokens_total=tokens,
        health="good",
        data_source="oauth",
        window_type="session",
    )


@pytest.mark.asyncio
async def test_delta_glitch_filtering(session: Session):
    """Minor drops should be ignored as glitches, substantial drops as resets."""
    now = datetime.now(UTC)
    
    # 1. Normal increase
    session.add(_snap(now - timedelta(minutes=20), tokens=1000.0))
    session.add(_snap(now - timedelta(minutes=15), tokens=1100.0)) # +100
    
    # 2. Minor drop (glitch) - 1050 is > 50% of 1100
    session.add(_snap(now - timedelta(minutes=10), tokens=1050.0)) # ignore
    
    # 3. Recovery after glitch
    session.add(_snap(now - timedelta(minutes=5), tokens=1200.0)) # +100 from high-water mark 1100
    
    # 4. Substantial drop (reset) - 400 is < 50% of 1200
    session.add(_snap(now, tokens=400.0)) # reset high-water to 400
    
    # 5. Increase after reset
    session.add(_snap(now + timedelta(minutes=5), tokens=500.0)) # +100
    
    session.commit()

    # We need to mock the Request object for the dependency
    scope = {"type": "http", "client": ("127.0.0.1", 12345), "path": "/"}
    mock_request = Request(scope=scope)

    result = await get_usage_history_deltas(
        request=mock_request,
        days=1.0,
        session=session
    )

    # Expected delta: (1100-1000) + (1200-1100) + (500-400) = 100 + 100 + 100 = 300
    assert result["token_delta_total"] == 300.0


@pytest.mark.asyncio
async def test_delta_first_read_is_baseline(session: Session):
    """The first non-zero read should be treated as a baseline, not consumption."""
    now = datetime.now(UTC)
    
    # 1. First poll sees 500M tokens (baseline)
    session.add(_snap(now - timedelta(minutes=10), tokens=500_000_000.0))
    
    # 2. Second poll sees 500M + 1000 tokens (consumption)
    session.add(_snap(now, tokens=500_001_000.0))
    
    session.commit()

    scope = {"type": "http", "client": ("127.0.0.1", 12345), "path": "/"}
    mock_request = Request(scope=scope)

    result = await get_usage_history_deltas(
        request=mock_request,
        days=1.0,
        session=session
    )

    # Should be 1000, NOT 500,001,000
    assert result["token_delta_total"] == 1000.0


@pytest.mark.asyncio
async def test_delta_recovery_from_zero_is_ignored(session: Session):
    """If a reading drops to 0 and jumps back to the previous peak, ignore it."""
    now = datetime.now(UTC)
    
    session.add(_snap(now - timedelta(minutes=15), tokens=1000.0)) # baseline
    session.add(_snap(now - timedelta(minutes=10), tokens=0.0))    # glitch to zero
    session.add(_snap(now - timedelta(minutes=5), tokens=1000.0))  # recovery (ignore)
    session.add(_snap(now, tokens=1100.0))                         # real usage (+100)
    
    session.commit()

    scope = {"type": "http", "client": ("127.0.0.1", 12345), "path": "/"}
    mock_request = Request(scope=scope)

    result = await get_usage_history_deltas(
        request=mock_request,
        days=1.0,
        session=session
    )

    assert result["token_delta_total"] == 100.0


@pytest.mark.asyncio
async def test_cost_delta_glitch_filtering(session: Session):
    """Same logic for currency/cost deltas."""
    now = datetime.now(UTC)
    
    session.add(_snap(now - timedelta(minutes=15), used=10.0, unit="currency"))
    session.add(_snap(now - timedelta(minutes=10), used=11.0, unit="currency")) # +1.0
    session.add(_snap(now - timedelta(minutes=5), used=10.5, unit="currency"))  # glitch
    session.add(_snap(now, used=12.0, unit="currency"))                        # +1.0
    
    session.commit()

    scope = {"type": "http", "client": ("127.0.0.1", 12345), "path": "/"}
    mock_request = Request(scope=scope)

    result = await get_usage_history_deltas(
        request=mock_request,
        days=1.0,
        session=session
    )

    assert result["cost_delta_total"] == 2.0


@pytest.mark.asyncio
async def test_no_limit_truncation(session: Session):
    """The endpoint should handle more than 10,000 rows without truncation."""
    now = datetime.now(UTC)
    
    # Add 10,100 rows
    for i in range(10100):
        session.add(_snap(now - timedelta(minutes=i), tokens=float(100000 - i)))
    
    session.commit()

    scope = {"type": "http", "client": ("127.0.0.1", 12345), "path": "/"}
    mock_request = Request(scope=scope)

    result = await get_usage_history_deltas(
        request=mock_request,
        days=30.0, # Large enough window
        session=session
    )

    assert result["series_sampled"] is False
    # Data is increasing from 89901 to 100000 (10100 points, 10099 intervals of +1)
    assert result["token_delta_total"] == 10099.0
