"""Usage events that arrived without an account, and their manual assignment."""

import json
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, func, select

from app.core.db import get_session
from app.core.security import require_admin_key
from app.models._datetime import iso_utc
from app.models.db import LatestUsage, PendingUsageEvent, ProviderConfig, UsageEvent
from app.models.schemas import UsageEventPush
from app.services import audit_log
from app.services.account_identity import (
    account_config_provider_id,
    allowed_assignment_provider_ids,
    resolve_account_id,
)
from app.services.credential_tags import CredentialTagRepo
from app.services.event_ingestor import EventIngestor, redirect_push
from app.services.token_cache import token_cache

router = APIRouter()


class PendingEventAssignment(BaseModel):
    event_ids: list[int]
    account_id: str
    # Provider owning ``account_id`` when it isn't the event's own provider
    # (e.g. Gemini usage assigned to an Antigravity account).
    target_provider_id: str | None = None


class PendingEventBatchAssignment(BaseModel):
    assignments: list[PendingEventAssignment]


@router.get("/events/pending")
async def list_pending_usage_events(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    rows = list(
        session.exec(
            select(PendingUsageEvent)
            .order_by(cast(Any, PendingUsageEvent.ts).desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    items = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append(
            {
                "id": row.id,
                "provider_id": row.provider_id,
                "event_id": row.event_id,
                "sidecar_id": row.sidecar_id,
                "ts": row.ts.isoformat(),
                "reason": row.reason,
                "model_id": payload.get("model_id"),
                "session_id": payload.get("session_id"),
            }
        )
    return {
        "items": items,
        "total": session.exec(select(func.count()).select_from(PendingUsageEvent)).one(),
        "offset": offset,
        "limit": limit,
    }


@router.get("/events/pending/sessions")
async def list_pending_usage_sessions(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    sidecar_id: str | None = Query(None),
    provider_id: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Group pending usage by provider, sidecar, and session for review."""
    return _pending_usage_session_groups(
        session,
        offset=offset,
        limit=limit,
        sidecar_id=sidecar_id,
        provider_id=provider_id,
        search=search,
    )


# The pending table can hold tens of thousands of rows and the Fleet card
# polls this endpoint every minute, so the grouping happens in SQL. Loading
# every pending row into Python to group it there made the page slower the
# bigger the backlog got — and a bigger backlog is exactly what a backlog
# that cannot be drained in one request produces.
#
# The rows are grouped by provider, sidecar and session. A row without a
# usable session id stands alone: missing session ids cannot safely
# identify related events.
#
# SQLite: this query needs **SQLite 3.38 or newer**. It uses the JSON
# functions (native from 3.38, JSON1 before that) and `AS MATERIALIZED`
# (3.35+), which is what keeps the extracted rows in a transient table
# instead of being re-evaluated per reference — without it the plan
# collapses and a 50,000-row backlog takes minutes rather than seconds.
# The app runs against the *system* SQLite, not the one bundled with
# Python, so the minimum is pinned here and checked at startup
# (`app/core/db.py`): Debian 12 ships 3.40, macOS 14 ships 3.43, both
# fine. The query deliberately avoids `group_concat(... ORDER BY ...)`
# (3.44+) — the group's event ids are a set, not a sequence.
_PENDING_SESSION_GROUPS_SQL = text(
    r"""
    WITH extracted AS MATERIALIZED (
        SELECT
            id,
            provider_id,
            sidecar_id,
            ts,
            CASE
                WHEN NOT json_valid(payload_json) THEN 'event:' || id
                WHEN json_type(payload_json, '$.session_id') = 'text'
                     AND json_extract(payload_json, '$.session_id') <> ''
                    THEN 'session:' || json_extract(payload_json, '$.session_id')
                ELSE 'event:' || id
            END AS session_key,
            CASE
                WHEN NOT json_valid(payload_json) THEN NULL
                WHEN json_type(payload_json, '$.session_id') = 'text'
                     AND json_extract(payload_json, '$.session_id') <> ''
                    THEN json_extract(payload_json, '$.session_id')
                ELSE NULL
            END AS session_id,
            CASE
                WHEN NOT json_valid(payload_json) THEN NULL
                WHEN json_type(payload_json, '$.model_id') = 'text'
                     AND json_extract(payload_json, '$.model_id') <> ''
                    THEN json_extract(payload_json, '$.model_id')
                ELSE NULL
            END AS model_id
        FROM pending_usage_events
    ),
    matched AS (
        -- The groups the filters select. The search matches a row when the
        -- needle is in its session id or in its model id, so a group is
        -- selected when one of its rows matches.
        SELECT provider_id, sidecar_id, session_key
        FROM extracted
        WHERE (:sidecar_id IS NULL OR sidecar_id = :sidecar_id)
          AND (:provider_id IS NULL OR provider_id = :provider_id)
          AND (
              :search IS NULL
              OR lower(COALESCE(session_id, '')) LIKE :search ESCAPE '\'
              OR lower(COALESCE(model_id, '')) LIKE :search ESCAPE '\'
          )
        GROUP BY provider_id, sidecar_id, session_key
    ),
    groups AS (
        SELECT
            e.provider_id,
            e.sidecar_id,
            e.session_key,
            MIN(e.session_id) AS session_id,
            COUNT(*) AS event_count,
            MIN(e.ts) AS first_ts,
            MAX(e.ts) AS last_ts,
            -- The ids of a group are a set, not a sequence: the server
            -- promotes them together and the client sends them back as
            -- they came. Sorting them would need SQLite 3.44's
            -- `group_concat(... ORDER BY ...)`, which the system SQLite on
            -- Debian 12 and macOS 14 does not have.
            group_concat(e.id) AS event_ids,
            group_concat(DISTINCT e.model_id) AS model_ids
        FROM extracted e
        JOIN matched m
          ON m.provider_id = e.provider_id
         AND m.sidecar_id = e.sidecar_id
         AND m.session_key = e.session_key
        GROUP BY e.provider_id, e.sidecar_id, e.session_key
    ),
    page AS (
        -- The session key breaks ties, so a group cannot move between two
        -- pages while the operator pages through the backlog.
        SELECT * FROM groups ORDER BY last_ts DESC, session_key ASC
        LIMIT :limit OFFSET :offset
    ),
    counts AS (
        SELECT
            (SELECT COUNT(*) FROM groups) AS total_groups,
            (SELECT COALESCE(SUM(event_count), 0) FROM groups) AS matching_events,
            (SELECT COUNT(*) FROM pending_usage_events) AS total_events,
            (
                SELECT json_group_array(value)
                FROM (SELECT DISTINCT sidecar_id AS value FROM pending_usage_events ORDER BY sidecar_id)
            ) AS sidecars,
            (
                SELECT json_group_array(value)
                FROM (SELECT DISTINCT provider_id AS value FROM pending_usage_events ORDER BY provider_id)
            ) AS providers
    )
    SELECT
        p.provider_id,
        p.sidecar_id,
        p.session_id,
        p.event_count,
        p.first_ts,
        p.last_ts,
        p.event_ids,
        p.model_ids,
        c.total_groups,
        c.matching_events,
        c.total_events,
        c.sidecars,
        c.providers
    FROM counts c
    LEFT JOIN page p ON 1 = 1
    """
)


def _escape_like(value: str) -> str:
    """Escape the characters a LIKE pattern would read as wildcards.

    The search is a plain substring search, so a needle that contains a
    ``%`` or a ``_`` has to be taken literally.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pending_stored_ts(value: str) -> datetime:
    """Read a stored timestamp the way the ORM reads it."""
    return datetime.fromisoformat(value)


def _pending_usage_session_groups(
    session: Session,
    *,
    offset: int,
    limit: int,
    sidecar_id: str | None,
    provider_id: str | None,
    search: str | None,
) -> dict[str, Any]:
    """Group the pending usage events for the review table.

    The grouping runs in SQL (see :data:`_PENDING_SESSION_GROUPS_SQL`) so
    the page cost does not grow with the backlog.
    """
    needle = search.strip().casefold() if search else ""
    pattern = f"%{_escape_like(needle)}%" if needle else None
    rows = session.execute(
        _PENDING_SESSION_GROUPS_SQL,
        {
            "sidecar_id": sidecar_id,
            "provider_id": provider_id,
            "search": pattern,
            "limit": limit,
            "offset": offset,
        },
    ).all()

    items: list[dict[str, Any]] = []
    totals: dict[str, Any] = {
        "total_events": 0,
        "matching_events": 0,
        "total_groups": 0,
        "sidecars": [],
        "providers": [],
    }
    for row in rows:
        totals = {
            "total_events": int(row.total_events or 0),
            "matching_events": int(row.matching_events or 0),
            "total_groups": int(row.total_groups or 0),
            # Sorted here rather than in SQL: an aggregate's row order is
            # not guaranteed, and the list is the same either way.
            "sidecars": sorted(json.loads(row.sidecars)) if row.sidecars else [],
            "providers": sorted(json.loads(row.providers)) if row.providers else [],
        }
        if row.provider_id is None:
            # The counts row of an empty page.
            continue
        items.append(
            {
                "provider_id": row.provider_id,
                "sidecar_id": row.sidecar_id,
                "session_id": row.session_id,
                "event_ids": [int(value) for value in (row.event_ids or "").split(",") if value],
                "event_count": int(row.event_count or 0),
                "first_ts": iso_utc(_pending_stored_ts(row.first_ts)),
                "last_ts": iso_utc(_pending_stored_ts(row.last_ts)),
                "model_ids": sorted({value for value in (row.model_ids or "").split(",") if value}),
            }
        )
    return {
        "items": items,
        "total_events": totals["total_events"],
        "matching_events": totals["matching_events"],
        "total_groups": totals["total_groups"],
        "sidecars": totals["sidecars"],
        "providers": totals["providers"],
        "offset": offset,
        "limit": limit,
    }


async def _validate_pending_event_assignments(
    assignments: list[PendingEventAssignment], session: Session
) -> list[tuple[str, str, list[PendingUsageEvent]]]:
    event_ids = [event_id for assignment in assignments for event_id in assignment.event_ids]
    if not assignments or not event_ids or len(event_ids) > 10_000:
        raise HTTPException(status_code=422, detail="Select between 1 and 10000 events.")
    if any(not assignment.event_ids for assignment in assignments):
        raise HTTPException(status_code=422, detail="Each account assignment must include events.")
    if len(event_ids) != len(set(event_ids)):
        raise HTTPException(status_code=422, detail="An event can only appear once per batch.")

    # Discovered identities can be valid assignment targets even without a
    # provider_configs row (for example sidecar-managed Antigravity accounts).
    # Only accept those that are still known from the cache or latest_usage.
    active_account_keys = {
        (provider_id, resolve_account_id("", account_id, None))
        for provider_id, account_id, _label in await token_cache.get_all_active_accounts()
    }
    latest_account_keys = {
        (provider_id, resolve_account_id("", active_id, None))
        for provider_id, active_id in session.exec(
            select(LatestUsage.provider_id, LatestUsage.account_id).distinct()
        ).all()
        if active_id
    }
    providers_with_config = set(session.exec(select(ProviderConfig.provider_id).distinct()).all())
    rows = list(
        session.exec(
            select(PendingUsageEvent).where(cast(Any, PendingUsageEvent.id).in_(event_ids))
        ).all()
    )
    if len(rows) != len(event_ids):
        raise HTTPException(status_code=404, detail="One or more pending events were not found.")
    by_id = {row.id: row for row in rows}
    # Check mapping collisions before validating account configuration so a
    # malformed batch cannot partially populate the validated work list.
    future_tags: dict[tuple[str, str, str], tuple[str, str]] = {}
    for assignment in assignments:
        account_id = resolve_account_id("", assignment.account_id, None)
        for event_id in assignment.event_ids:
            row = by_id[event_id]
            target = assignment.target_provider_id or account_config_provider_id(row.provider_id)
            if target not in allowed_assignment_provider_ids(row.provider_id):
                raise HTTPException(
                    status_code=422,
                    detail=f"Account provider {target!r} is not a valid target for "
                    f"{row.provider_id!r} events.",
                )
            origin = json.loads(row.payload_json).get(
                "credential_origin"
            ) or CredentialTagRepo.provider_origin(row.provider_id)
            tag_key = (row.provider_id, row.sidecar_id, origin)
            if tag_key in future_tags and future_tags[tag_key] != (target, account_id):
                raise HTTPException(
                    status_code=422,
                    detail="A provider on one host can only be assigned to one account per batch.",
                )
            future_tags[tag_key] = (target, account_id)
    validated: list[tuple[str, str, list[PendingUsageEvent]]] = []
    assignable_accounts: dict[tuple[str, str], bool] = {}
    for assignment in assignments:
        account_id = resolve_account_id("", assignment.account_id, None)
        # One entry per config provider: an assignment whose events span providers
        # must not take its first row's target for the rest.
        rows_by_config_provider: dict[str, list[PendingUsageEvent]] = {}
        for event_id in assignment.event_ids:
            row = by_id[event_id]
            rows_by_config_provider.setdefault(
                account_config_provider_id(row.provider_id), []
            ).append(row)
        for row_config_provider, assignment_rows in rows_by_config_provider.items():
            assignment_target = assignment.target_provider_id or row_config_provider
            account_key = (assignment_target, account_id)
            if account_key not in assignable_accounts:
                configured = session.exec(
                    select(ProviderConfig).where(
                        ProviderConfig.provider_id == assignment_target,
                        ProviderConfig.account_id == account_id,
                    )
                ).first()
                if configured is not None:
                    # Archived rows are always disabled, so accept them explicitly;
                    # disabled-but-not-archived accounts stay rejected.
                    assignable_accounts[account_key] = configured.enabled or configured.archived
                else:
                    assignable_accounts[account_key] = account_key in active_account_keys or (
                        assignment_target not in providers_with_config
                        and account_key in latest_account_keys
                    )
            if not assignable_accounts[account_key]:
                raise HTTPException(
                    status_code=404,
                    detail=f"No active account {account_id!r} known for "
                    f"{assignment_rows[0].provider_id!r}.",
                )
            validated.append((assignment_target, account_id, assignment_rows))
    return validated


def _promote_pending_event_rows(
    request: Request,
    session: Session,
    rows: list[PendingUsageEvent],
    target_provider_id: str,
    account_id: str,
    *,
    clear_pending: bool = True,
    record_audit: bool = True,
) -> None:
    # Ingest is idempotent. Keep pending rows until every promotion succeeds;
    # a retry after a partial failure safely deduplicates already-promoted rows.
    pushes_by_sidecar: dict[str, list[UsageEventPush]] = {}
    event_key_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        event_key = (row.provider_id, row.event_id)
        event_key_counts[event_key] = event_key_counts.get(event_key, 0) + 1
    ingestor = EventIngestor(session)
    for row in rows:
        redirected = target_provider_id != account_config_provider_id(row.provider_id)
        payload = UsageEventPush.model_validate_json(row.payload_json).model_copy(
            update={"account_id": account_id, "account_source": "tag"}
        )
        if redirected:
            payload = redirect_push(payload, target_provider_id, account_id)
        # OpenCode doesn't identify which credential origin handled a
        # message. Persist an explicit provider-level tag scoped to the
        # source sidecar so later messages use the operator's resolution.
        CredentialTagRepo.set_tag(
            session,
            provider_id=row.provider_id,
            credential_origin=payload.credential_origin
            or CredentialTagRepo.provider_origin(row.provider_id),
            account_id=account_id,
            sidecar_id=row.sidecar_id,
            set_by="operator",
            target_provider_id=target_provider_id if redirected else None,
        )
        if (
            redirected
            and session.exec(
                select(UsageEvent.id).where(
                    UsageEvent.provider_id == row.provider_id,
                    UsageEvent.event_id == row.event_id,
                )
            ).first()
            is not None
        ):
            # Already counted under the source provider; just drop the pending copy.
            continue
        existing = session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == payload.provider_id,
                UsageEvent.event_id == row.event_id,
            )
        ).first()
        # A newly resolved push from another sidecar may have arrived while
        # this pending copy was waiting. Reuse its sidecar identity so the
        # explicit operator assignment can safely move that event and its
        # rollups instead of being mistaken for a competing host replay.
        promotion_sidecar = existing.sidecar_id if existing is not None else row.sidecar_id
        if event_key_counts[(row.provider_id, row.event_id)] > 1:
            # Keep duplicate replays sequential: a later copy must observe
            # the first promoted row and reuse its sidecar identity.
            ingestor.ingest([payload], sidecar_id=promotion_sidecar)
        else:
            pushes_by_sidecar.setdefault(promotion_sidecar, []).append(payload)
    for sidecar_id, pushes in pushes_by_sidecar.items():
        ingestor.ingest(pushes, sidecar_id=sidecar_id)
    if clear_pending:
        for row in rows:
            session.delete(row)
        session.commit()
    if record_audit:
        audit_log.record(
            session,
            request,
            action="usage.pending_events_assigned",
            target_id=(
                f"{rows[0].provider_id}->{target_provider_id}/{account_id}"
                if rows and target_provider_id != account_config_provider_id(rows[0].provider_id)
                else f"{rows[0].provider_id if rows else ''}/{account_id}"
            ),
            payload={"event_count": len(rows)},
        )


@router.post("/events/pending/assign")
async def assign_pending_usage_events(
    request: Request,
    body: PendingEventAssignment,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    if not body.event_ids or len(body.event_ids) > 1000:
        raise HTTPException(status_code=422, detail="Select between 1 and 1000 events.")
    validated = await _validate_pending_event_assignments([body], session)
    # One entry per config provider (events may span providers sharing a config).
    for target_provider_id, account_id, rows in validated:
        _promote_pending_event_rows(request, session, rows, target_provider_id, account_id)
    first_target, _first_account, first_rows = validated[0]
    result: dict[str, Any] = {
        "assigned": sum(len(rows) for _target, _account, rows in validated),
        "provider_id": first_rows[0].provider_id if first_rows else None,
    }
    # Only echoed when events were actually re-homed to another provider.
    if first_rows and first_target != account_config_provider_id(first_rows[0].provider_id):
        result["target_provider_id"] = first_target
    # Every validated group as {provider_id, target_provider_id, account_id, assigned}: a
    # subset of the batch endpoint's mapping shape (no sidecar_id, since a group already
    # coalesces by config provider). The top-level fields above describe only the first
    # group (back-compat).
    result["mappings"] = [
        {
            "provider_id": rows[0].provider_id,
            "target_provider_id": target,
            "account_id": account,
            "assigned": len(rows),
        }
        for target, account, rows in validated
    ]
    return result


@router.post("/events/pending/assign-batch")
async def assign_pending_usage_events_batch(
    request: Request,
    body: PendingEventBatchAssignment,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    validated = await _validate_pending_event_assignments(body.assignments, session)
    for target_provider_id, account_id, rows in validated:
        _promote_pending_event_rows(
            request,
            session,
            rows,
            target_provider_id,
            account_id,
            clear_pending=False,
            record_audit=False,
        )
    for _target, _account_id, rows in validated:
        for row in rows:
            session.delete(row)
    session.commit()
    assigned = sum(len(rows) for _target, _account_id, rows in validated)
    providers = sorted(
        {row.provider_id for _target, _account_id, rows in validated for row in rows}
    )
    mapping_targets = sorted(
        {
            (row.provider_id, row.sidecar_id, target_provider_id, account_id)
            for target_provider_id, account_id, rows in validated
            for row in rows
        }
    )
    audit_log.record(
        session,
        request,
        action="usage.pending_events_batch_assigned",
        target_id=",".join(providers),
        payload={"event_count": assigned, "assignment_count": len(validated)},
    )
    return {
        "assigned": assigned,
        "providers": providers,
        "mappings": [
            {
                "provider_id": provider_id,
                "sidecar_id": sidecar_id,
                "target_provider_id": target_provider_id,
                "account_id": account_id,
            }
            for provider_id, sidecar_id, target_provider_id, account_id in mapping_targets
        ],
    }


__all__ = [
    "PendingEventAssignment",
    "PendingEventBatchAssignment",
    "_PENDING_SESSION_GROUPS_SQL",
    "_escape_like",
    "_pending_stored_ts",
    "_pending_usage_session_groups",
    "_promote_pending_event_rows",
    "_validate_pending_event_assignments",
    "assign_pending_usage_events",
    "assign_pending_usage_events_batch",
    "list_pending_usage_events",
    "list_pending_usage_sessions",
]
