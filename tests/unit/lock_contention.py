"""Shared helpers for the lock-contention tests of the batch-writing repairs.

A repair that writes events in batches can hit SQLite's single-writer lock.
The tests here simulate that: the session's own batch `UPDATE` fails with a
locked `OperationalError`, so the repair's retry and its partial-failure
rebuild can be observed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.expression import Update
from sqlmodel import Session


def lock_batches_after(session: Session, batches: int) -> dict[str, int]:
    """Make the session's batch commits fail from the `batches`-th one on.

    Only the batch's own `UPDATE` counts: the rollup rebuild runs `text(...)`
    statements and the window rebuild deletes through the ORM, so neither is
    mistaken for a batch write — the rebuild that follows a failed batch must
    still be able to commit.

    Returns a counter dict with the number of batch writes attempted.
    """
    state = {"written": 0, "in_batch": False}
    original_execute = session.execute
    original_commit = session.commit

    def tracked_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update):
            state["in_batch"] = True
        return original_execute(statement, *args, **kwargs)

    def flaky_commit() -> None:
        if state["in_batch"]:
            state["written"] += 1
            state["in_batch"] = False
            if state["written"] >= batches:
                raise OperationalError(
                    "UPDATE usage_events SET cost_usd = ? WHERE usage_events.id IN (?)",
                    {"cost_usd": 0.4242, "id_1": 918273},
                    Exception("database is locked"),
                )
        original_commit()

    session.execute = tracked_execute  # type: ignore[method-assign]
    session.commit = flaky_commit  # type: ignore[method-assign]
    return state


def lock_every_batch(session: Session, times: int) -> dict[str, int]:
    """Make the first `times` batch commits fail, then let them through.

    Returns a counter dict with the number of batch commits attempted.
    """
    state = {"failed": 0, "in_batch": False}
    original_execute = session.execute
    original_commit = session.commit

    def tracked_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update):
            state["in_batch"] = True
        return original_execute(statement, *args, **kwargs)

    def flaky_commit() -> None:
        if state["in_batch"]:
            state["in_batch"] = False
            if state["failed"] < times:
                state["failed"] += 1
                raise OperationalError(
                    "UPDATE usage_events SET cost_usd = ? WHERE usage_events.id IN (?)",
                    {"cost_usd": 0.4242, "id_1": 918273},
                    Exception("database is locked"),
                )
        original_commit()

    session.execute = tracked_execute  # type: ignore[method-assign]
    session.commit = flaky_commit  # type: ignore[method-assign]
    return state
