"""Retry utilities for maintenance operations under SQLite lock contention."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_INITIAL_BACKOFF = 0.05


@dataclass(frozen=True)
class LockRetryProfile:
    """How hard to try again when SQLite says the database is locked.

    `max_retries` is the number of attempts in total (not the number of
    *retries* after the first), and `initial_backoff` is the delay before the
    second attempt; each further attempt doubles it.
    """

    max_retries: int
    initial_backoff: float


#: The default patience: enough for a momentary lock, not enough to hold a
#: repair up. The CLI scripts use it — they run against a stopped server.
DEFAULT_LOCK_PROFILE = LockRetryProfile(max_retries=_MAX_RETRIES, initial_backoff=_INITIAL_BACKOFF)

#: The Data Health applies run against a live, writing server, where a lock is
#: a busy moment rather than a fault. They wait much longer before giving up —
#: the connection itself already waits 30 s for the lock before the error
#: surfaces (see `app/core/db.py`), so the profile only adds the patience
#: *after* that.
DATA_HEALTH_LOCK_PROFILE = LockRetryProfile(max_retries=6, initial_backoff=2.0)


def _is_sqlite_locked_or_busy(exc: OperationalError) -> bool:
    """True if `exc` represents SQLite SQLITE_BUSY or SQLITE_LOCKED contention.

    Inspects `exc.orig` for the driver-level error name/code first to prevent false
    positives from bound parameters containing the words 'locked' or 'busy'.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        errorname = getattr(orig, "sqlite_errorname", None)
        if errorname and (
            errorname in ("SQLITE_BUSY", "SQLITE_LOCKED")
            or errorname.startswith(("SQLITE_BUSY_", "SQLITE_LOCKED_"))
        ):
            return True
        errorcode = getattr(orig, "sqlite_errorcode", None)
        if errorcode is not None and (errorcode & 0xFF) in (5, 6):  # SQLITE_BUSY=5, SQLITE_LOCKED=6
            return True
        msg = str(orig).lower()
        if "database is locked" in msg or "database is busy" in msg:
            return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _retry_on_lock[T](
    fn: Callable[[], T],
    *,
    session: Session,
    action_desc: str,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> T:
    """Execute `fn`, rolling back and retrying with exponential backoff on SQLite lock contention."""
    if max_retries <= 0:
        raise ValueError(f"max_retries must be positive, got {max_retries}")
    if initial_backoff < 0:
        raise ValueError(f"initial_backoff must be non-negative, got {initial_backoff}")
    for attempt in range(max_retries):
        try:
            return fn()
        except OperationalError as exc:
            if _is_sqlite_locked_or_busy(exc) and attempt < max_retries - 1:
                session.rollback()
                delay = initial_backoff * (2**attempt) + random.uniform(0.01, 0.05)
                logger.warning(
                    "Database locked during %s (attempt %d/%d); retrying in %.2fs",
                    action_desc,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            raise
    raise AssertionError("unreachable: retry loop either returns or re-raises")


def commit_batch(
    session: Session,
    model: type[SQLModel],
    values: list[dict[str, Any]],
    *,
    action_desc: str,
    profile: LockRetryProfile = DEFAULT_LOCK_PROFILE,
) -> None:
    """UPDATE the given rows with per-row values and commit them as one batch.

    The `execute` and the `commit` are both inside the retried closure: the
    retry helper rolls the session back before it tries again, so the batch
    has to be written again, not only committed. The batch sets values by
    primary key, so writing it twice is idempotent.
    """
    if not values:
        return

    def _write() -> None:
        session.execute(update(model), values)
        session.commit()

    _retry_on_lock(
        _write,
        session=session,
        action_desc=action_desc,
        max_retries=profile.max_retries,
        initial_backoff=profile.initial_backoff,
    )
