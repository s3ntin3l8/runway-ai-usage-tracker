"""Rebuild closed `usage_windows` rows after events move or get recosted.

`UsageWindow` rows are a frozen archive — one row per grain, written exactly
once when a window's authoritative `reset_at` advances past its end, never
updated in place (see the model docstring in `app/models/db.py`). A repair
that changes which events fall in a window (a reassign, a merge) or their
cost (a recost) has to delete the stale rows and replay `close_window` to
get correct totals — but the window's own scrape-time metadata
(`limit_value`, `pct_used`) isn't recoverable from `usage_events`, so it must
be captured before the delete and threaded back in.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, delete, select

from app.models.db import UsageWindow
from app.services.maintenance._chunked_sql import _INITIAL_BACKOFF, _MAX_RETRIES, _retry_on_lock
from app.services.window_closer import close_window

# Bounds the SQLAlchemy identity map during a large replay — commit and
# expunge every this-many windows rather than holding the whole rebuild in
# one transaction (recost_events.py's phase D handled ~90k events / ~11k
# windows for a single heavy account; without this an all-provider rebuild
# is O(n^2) in identity-map bookkeeping).
_WINDOW_BATCH = 200

_WindowKey = tuple[str, str, str, datetime, datetime, str, str]
_WindowMeta = tuple[float | None, float | None]


def _index_by_identity(rows: list[UsageWindow]) -> dict[_WindowKey, _WindowMeta]:
    """Group by the 7-tuple window identity, preferring the all-grains
    (model_id='', sidecar_id='') row for the limit/pct metadata to restore."""
    index: dict[_WindowKey, _WindowMeta] = {}
    for w in rows:
        key = (
            w.provider_id,
            w.account_id,
            w.window_type,
            w.window_start,
            w.window_end,
            w.series_model_id,
            w.series_variant,
        )
        if w.model_id == "" and w.sidecar_id == "":
            index[key] = (w.limit_value, w.pct_used)
        else:
            index.setdefault(key, (w.limit_value, w.pct_used))
    return index


def _replay(
    session: Session,
    index: dict[_WindowKey, _WindowMeta],
    *,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> None:
    for (pid, aid, wtype, start, end, series_model_id, series_variant), (lv, pu) in index.items():

        def _close_single() -> None:
            close_window(
                session,
                provider_id=pid,
                account_id=aid,
                window_type=wtype,
                window_start=start,
                window_end=end,
                series_model_id=series_model_id,
                series_variant=series_variant,
                limit_value=lv,
                pct_used=pu,
            )
            session.commit()
            session.expunge_all()

        _retry_on_lock(
            _close_single,
            session=session,
            action_desc=f"rebuild window {pid}/{aid} {wtype} {start}",
            max_retries=max_retries,
            initial_backoff=initial_backoff,
        )


def count_windows_for_providers(session: Session, providers: list[str] | None) -> int:
    """Read-only preview of `rebuild_windows_for_providers` — the number of
    distinct window identities it would attempt, without writing anything.
    """
    stmt = select(UsageWindow)
    if providers:
        stmt = stmt.where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
    return len(_index_by_identity(list(session.exec(stmt).all())))


def rebuild_windows_for_providers(
    session: Session,
    providers: list[str] | None,
    *,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> int:
    """Rebuild every closed window for the given providers (`None` = all).

    Deletes and replays every `UsageWindow` row in scope. Caller commits
    beforehand if needed; this function commits internally as it batches.
    Returns the number of distinct window identities rebuilt.
    """
    stmt = select(UsageWindow)
    del_stmt = delete(UsageWindow)
    if providers:
        stmt = stmt.where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
        del_stmt = del_stmt.where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
    index = _index_by_identity(list(session.exec(stmt).all()))
    session.commit()
    session.expunge_all()  # drop identity-mapped rows the SELECT above loaded

    def _delete() -> None:
        session.exec(del_stmt)  # type: ignore[call-overload]
        session.commit()

    _retry_on_lock(
        _delete,
        session=session,
        action_desc="delete provider windows for rebuild",
        max_retries=max_retries,
        initial_backoff=initial_backoff,
    )

    _replay(session, index, max_retries=max_retries, initial_backoff=initial_backoff)
    return len(index)


def rebuild_windows_overlapping(
    session: Session,
    *,
    provider_id: str,
    account_ids: list[str],
    ts_min: datetime,
    ts_max: datetime,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> int:
    """Rebuild only the closed windows overlapping `[ts_min, ts_max]` for one
    provider across the given accounts (e.g. a source and a target account
    in a reassign/merge). Cheaper than `rebuild_windows_for_providers` when
    only a bounded slice of history moved. Returns the number of window
    identities rebuilt.
    """
    where = (
        UsageWindow.provider_id == provider_id,
        UsageWindow.account_id.in_(account_ids),  # type: ignore[attr-defined]
        UsageWindow.window_start <= ts_max,
        UsageWindow.window_end > ts_min,
    )
    del_stmt = delete(UsageWindow).where(*where)  # type: ignore[arg-type]
    rows = list(session.exec(select(UsageWindow).where(*where)).all())
    index = _index_by_identity(rows)
    session.commit()
    session.expunge_all()

    def _delete() -> None:
        session.exec(del_stmt)  # type: ignore[call-overload]
        session.commit()

    _retry_on_lock(
        _delete,
        session=session,
        action_desc="delete overlapping windows for rebuild",
        max_retries=max_retries,
        initial_backoff=initial_backoff,
    )

    _replay(session, index, max_retries=max_retries, initial_backoff=initial_backoff)
    return len(index)
