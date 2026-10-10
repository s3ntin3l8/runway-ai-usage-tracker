from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core import replay
from app.models.db import SidecarRequestReceipt


def test_receipt_survives_new_connections_and_concurrent_requests():
    replay.receipt_engine()  # Initialize the isolated fixture before concurrent claims.

    def claim(_):
        try:
            replay.claim_signature("synthetic-key", "signed-envelope")
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))
    assert outcomes.count(200) == 1
    assert outcomes.count(409) == 7
    replay.receipt_engine().dispose()
    assert claim(None) == 409
    with Session(replay.receipt_engine()) as session:
        assert len(session.exec(select(SidecarRequestReceipt)).all()) == 1


def test_rotation_and_expiry(monkeypatch):
    monkeypatch.setattr(replay.time, "time", lambda: 1000)
    replay.claim_signature("old-key", "signature")
    replay.claim_signature("new-key", "signature")
    monkeypatch.setattr(replay.time, "time", lambda: 1361)
    replay.claim_signature("old-key", "signature")
    with Session(replay.receipt_engine()) as session:
        assert len(session.exec(select(SidecarRequestReceipt)).all()) == 1


def test_missing_table_fails_closed():
    SidecarRequestReceipt.__table__.drop(replay.receipt_engine())
    with pytest.raises(HTTPException) as exc:
        replay.claim_signature("key", "signature")
    assert exc.value.status_code == 503
