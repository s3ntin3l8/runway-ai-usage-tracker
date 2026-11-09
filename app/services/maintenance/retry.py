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

MAX_RETRIES = 5
INITIAL_BACKOFF = 0.05


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
DEFAULT_LOCK_PROFILE = LockRetryProfile(max_retries=MAX_RETRIES, initial_backoff=INITIAL_BACKOFF)

#: The Data Health applies run against a live, writing server, where a lock is
#: a busy moment rather than a fault. They wait much longer before giving up —
#: the connection itself already waits 30 s for the lock before the error
#: surfaces (see `app/core/db.py`), so the profile only adds the patience
#: *after* that.
DATA_HEALTH_LOCK_PROFILE = LockRetryProfile(max_retries=6, initial_backoff=2.0)


def is_sqlite_locked_or_busy(exc: OperationalError) -> bool:
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


def retry_on_lock[T](
    fn: Callable[[], T],
    *,
    session: Session,
    action_desc: str,
    max_retries: int = MAX_RETRIES,
    initial_backoff: float = INITIAL_BACKOFF,
) -> T:
    """Execute `fn`, rolling back and retrying with exponential backoff on SQLite lock contention."""
    if max_retries <= 0:
        raise ValueError(f"max_retries must be positive, got {max_retries}")
    if initial_backoff < 0:
        raise ValueError(f"initial_backoff must be non-negative, got {initial_backoff}")
    blocked_total = 0.0
    for attempt in range(max_retries):
        started = time.monotonic()
        try:
            return fn()
        except OperationalError as exc:
            # Wall time of the whole failed attempt: any `busy_timeout` wait
            # plus the statements run around it. A lock error that SQLite
            # raises without invoking the busy handler (a deferred-BEGIN
            # read->write escalation) reads as ~0s here, which is the
            # momentary-vs-stuck signal.
            waited = time.monotonic() - started
            if not is_sqlite_locked_or_busy(exc):
                raise
            blocked_total += waited
            # Log only the operation's name and the timings: never `exc`, whose
            # text carries the statement and its bound parameters.
            if attempt < max_retries - 1:
                session.rollback()
                delay = initial_backoff * (2**attempt) + random.uniform(0.01, 0.05)
                logger.warning(
                    "Database locked during %s (attempt %d/%d) after %.2fs in the attempt; retrying in %.2fs",
                    action_desc,
                    attempt + 1,
                    max_retries,
                    waited,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.error(
                "Database still locked during %s after %d attempt%s; %.2fs spent in failed attempts in total",
                action_desc,
                max_retries,
                "" if max_retries == 1 else "s",
                blocked_total,
            )
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

    retry_on_lock(
        _write,
        session=session,
        action_desc=action_desc,
        max_retries=profile.max_retries,
        initial_backoff=profile.initial_backoff,
    )
