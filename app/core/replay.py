"""Durable single-use receipts for authenticated sidecar envelopes."""

import hashlib
import logging
import time

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, col

from app.models.db import SidecarRequestReceipt

logger = logging.getLogger(__name__)
RECEIPT_TTL = 360


def receipt_engine() -> Engine:
    from app.core.db import engine

    return engine


def claim_signature(api_key: str, signature: str) -> None:
    """Commit a unique receipt before any handler effects, including failures.

    The primary key makes concurrent claims atomic across processes. A fresh
    signature is required for a retry after a lost response or handler error.
    Receipts contain neither the request body nor reusable signature material.
    """
    now = time.time()
    receipt_id = hashlib.sha256(
        f"{hashlib.sha256(api_key.encode()).hexdigest()}:{signature}".encode()
    ).hexdigest()
    try:
        with Session(receipt_engine()) as session:
            # Start with a write, avoiding SQLite read-to-write snapshot upgrades.
            session.execute(
                delete(SidecarRequestReceipt).where(col(SidecarRequestReceipt.expires_at) < now)
            )
            session.add(SidecarRequestReceipt(receipt_id=receipt_id, expires_at=now + RECEIPT_TTL))
            session.commit()
    except IntegrityError:
        raise HTTPException(status_code=409, detail={"error": "replayed_request"}) from None
    except SQLAlchemyError:
        logger.error("Sidecar replay receipt storage unavailable")
        raise HTTPException(
            status_code=503, detail="Sidecar replay protection unavailable; retry later"
        ) from None
