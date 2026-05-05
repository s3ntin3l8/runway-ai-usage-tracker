# tests/unit/test_accumulator.py
import os
import tempfile

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CumulativeUsage
from app.services.accumulator import UsageAccumulator


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


def test_accumulator_adds_delta(session: Session):
    accumulator = UsageAccumulator(session)

    # First report
    accumulator.process_delta(
        provider_id="test",
        account_id="acc1",
        sidecar_id="s1",
        unit_type="tokens",
        delta_value=100.0,
        timestamp="2026-05-03T12:00:00Z",
    )

    # Second report
    accumulator.process_delta(
        provider_id="test",
        account_id="acc1",
        sidecar_id="s1",
        unit_type="tokens",
        delta_value=50.0,
        timestamp="2026-05-03T12:05:00Z",
    )

    records = session.exec(select(CumulativeUsage)).all()
    # Should create lifetime, year, month records
    assert len(records) == 3
    for r in records:
        assert r.total_value == 150.0
