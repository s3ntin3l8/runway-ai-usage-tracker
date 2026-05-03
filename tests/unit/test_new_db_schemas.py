# tests/unit/test_new_db_schemas.py
import os
import tempfile
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CumulativeUsage, LatestUsage


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


def test_latest_usage_creation(session: Session):
    record = LatestUsage(
        provider_id="anthropic",
        account_id="user1",
        sidecar_id="laptop-1",
        window_type="monthly",
        variant="default",
        card_json='{"status": "ok"}',
        updated_at=datetime.now(UTC),
    )
    session.add(record)
    session.commit()

    fetched = session.exec(select(LatestUsage)).first()
    assert fetched is not None
    assert fetched.provider_id == "anthropic"


def test_cumulative_usage_creation(session: Session):
    record = CumulativeUsage(
        provider_id="openai",
        account_id="user2",
        sidecar_id="server-1",
        period_type="lifetime",
        period_key="all",
        unit_type="tokens_input",
        total_value=50000.0,
    )
    session.add(record)
    session.commit()

    fetched = session.exec(select(CumulativeUsage)).first()
    assert fetched is not None
    assert fetched.total_value == 50000.0
