import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from app.api.endpoints.shared import MONTH_KEY_RE, local_period_anchors, month_bounds_utc
from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.utils import resolve_user_tz
from app.services.queries import (
    query_cumulative_live,
)

# Higher rank = longer / more authoritative window.

router = APIRouter()


@router.get("/cumulative")
@limiter.limit("30/minute")
async def get_cumulative_usage(  # noqa: PLR0915 — known-debt: default/month-live/range-live/filtered bucket assembly, splits poorly
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    period_type: str | None = None,
    period_key: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Authoritative cumulative usage rolled up across sidecars from usage_period_rollup.

    Default: returns one entry per (provider_id, account_id) with
    `lifetime`, current-year, and current-month totals, plus per-model and
    per-sidecar breakdowns (matches spec §8.6).

    Query params narrow the scope:
    - provider_id / account_id: identity filter
    - period_type: one of 'lifetime' | 'year' | 'month'
    - period_key: a specific bucket (e.g. '2026', '2026-05', 'all')

    Requesting a specific month (period_type='month', period_key='YYYY-MM')
    takes a tz-correct path: the month bucket is aggregated live from
    usage_events on the user's local-calendar boundaries (see
    `month_bounds_utc`), matching the live current-month gauge. The UTC-keyed
    rollup is intentionally bypassed here because its month boundaries can land
    on the wrong calendar month for users far from UTC. `current_month_key`
    points at the requested month so callers read it the same way as the
    default response.

    Passing a `since` (and optional exclusive `until`) instant aggregates the
    bucket live over that arbitrary [since, until) window — the rolling-range
    path the provider Activity/Cost tabs use for "last N days" (issue #87).
    `current_month_key` points at the range bucket so callers read it the same
    way. Takes precedence over period_type/period_key.

    Passing `period_type='month'` with no `period_key` aggregates just the
    *current* local-tz month, live — the same boundary and query the default
    call uses for its month bucket, but without the accompanying full
    `usage_period_rollup` read or year-to-date scan. For a caller (e.g. the
    Home token/message card) that only needs "this month," this is much
    cheaper than the default call.

    Synchronous rollup/live-aggregation work is offloaded to a worker thread
    via `asyncio.to_thread` (see `fetch_fleet_view` for why) — this is the
    largest home-page payload (~850KB raw) and the slowest of the cheap
    endpoints, so keeping it off the event loop matters most here.
    """
    return await asyncio.to_thread(
        _get_cumulative_usage_sync,
        session,
        provider_id=provider_id,
        account_id=account_id,
        period_type=period_type,
        period_key=period_key,
        since=since,
        until=until,
        sidecar_id=sidecar_id,
    )


def _get_cumulative_usage_sync(  # noqa: PLR0915 — known-debt: default/month-live/range-live/filtered bucket assembly, splits poorly
    session: Session,
    *,
    provider_id: str | None,
    account_id: str | None,
    period_type: str | None,
    period_key: str | None,
    since: str | None,
    until: str | None,
    sidecar_id: str | None = None,
) -> dict[str, Any]:
    from app.models.db import UsagePeriodRollup

    now = datetime.now(UTC)
    is_range_live = bool(since)
    is_default = not is_range_live and period_type is None and period_key is None
    is_month_live = (
        not is_range_live
        and period_type == "month"
        and bool(period_key)
        and bool(MONTH_KEY_RE.match(period_key or ""))
    )
    # period_type=month with no period_key: current local-tz month only, live —
    # the same live aggregation the default call does for its month bucket, but
    # without the accompanying full rollup read + year-to-date scan. Lets a
    # caller that only needs "this month" (e.g. the Home token/msg card) skip
    # both without losing the local-calendar boundary. (No need to also guard
    # on `not is_range_live` here — the elif chain below checks is_range_live
    # first, so a caller passing both `since` and period_type='month' still
    # takes the range-live branch.)
    is_current_month_live = period_type == "month" and not period_key

    stmt = select(UsagePeriodRollup)
    if provider_id:
        stmt = stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsagePeriodRollup.account_id == account_id)
    if sidecar_id:
        stmt = stmt.where(UsagePeriodRollup.sidecar_id == sidecar_id)
    if period_type:
        stmt = stmt.where(UsagePeriodRollup.period_type == period_type)
    if period_key:
        stmt = stmt.where(UsagePeriodRollup.period_key == period_key)
    # The live-aggregation paths (tz-correct month, arbitrary range) ignore the
    # rollup rows in favour of an on-demand event aggregation, so skip the fetch.
    rows = (
        []
        if (is_month_live or is_range_live or is_current_month_live)
        else session.exec(stmt).all()
    )

    # The default (unfiltered) call powers the live "This period" / "Yearly"
    # gauges, which reset on the user's local calendar. Anchor those two
    # buckets at local-tz period starts and compute them live from events;
    # lifetime + historical drill-downs stay on the UTC rollup.
    month_live: dict[tuple[str, str], dict[str, Any]] = {}
    if is_default:
        anchors = local_period_anchors(resolve_user_tz(session))
        current_year = anchors["current_year"]
        current_month = anchors["current_month"]
        # provider_id/account_id must be threaded through here too — every
        # other live-aggregation branch below does — otherwise a caller that
        # scopes the request to one identity (e.g. ProviderPage's
        # `fetchCumulative({ provider_id, account_id })`, which hits this
        # branch since it passes no period_type) silently gets the live
        # month/year totals for *every* provider/account mixed into the
        # response: inflated payload, inflated aggregation cost, and a
        # response that leaks other accounts' data into a filtered request.
        live_month = query_cumulative_live(
            session,
            since=anchors["month_start_utc"],
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
        )
        live_year = query_cumulative_live(
            session,
            since=anchors["year_start_utc"],
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
        )
    elif is_range_live:
        # Arbitrary [since, until) window — aggregate live, surfaced under a
        # synthetic 'range' bucket that `current_month_key` points at.
        month_live = query_cumulative_live(
            session,
            since=parse_iso8601_utc(since or ""),
            until=parse_iso8601_utc(until) if until else None,
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
        )
        current_year = "range"
        current_month = "range"
        live_month = {}
        live_year = {}
    elif is_month_live:
        since_utc, until_utc = month_bounds_utc(resolve_user_tz(session), period_key or "")
        month_live = query_cumulative_live(
            session,
            since=since_utc,
            until=until_utc,
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
        )
        current_year = (period_key or "")[:4]
        current_month = period_key or ""
        live_month = {}
        live_year = {}
    elif is_current_month_live:
        # Current local-tz month only, live — no `until` (open-ended, matches
        # "now"). Same anchor the default call uses for its month bucket.
        anchors = local_period_anchors(resolve_user_tz(session))
        month_live = query_cumulative_live(
            session,
            since=anchors["month_start_utc"],
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
        )
        current_year = anchors["current_year"]
        current_month = anchors["current_month"]
        live_month = {}
        live_year = {}
    else:
        # Filtered (historical drill-down) path: serve the rollup as-is. The
        # returned current_*_key here are UTC-based and only meaningful on the
        # default call — no consumer reads them on a filtered request.
        current_year = now.strftime("%Y")
        current_month = now.strftime("%Y-%m")
        live_month = {}
        live_year = {}

    # Group by (provider_id, account_id) → bucket label → totals + by_model + by_sidecar
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for r in rows:
        ident = (r.provider_id, r.account_id)
        bucket_key = _cumulative_bucket_label(r.period_type, r.period_key)
        bucket = grouped.setdefault(ident, {}).setdefault(
            bucket_key,
            {
                "tokens_input": 0,
                "tokens_output": 0,
                "tokens_cache_read": 0,
                "tokens_cache_create": 0,
                "tokens_reasoning": 0,
                "cost_usd": 0.0,
                "cost_cache": 0.0,
                "msgs": 0,
                "by_model": {},
                "by_sidecar": {},
            },
        )
        # Cache portion of cost (cache_read + cache_create), for exclude-cache views.
        cost_cache = r.cost_cache_read + r.cost_cache_create
        if r.sidecar_id == (sidecar_id or "") and r.model_id == "":
            # Top-level totals row for all sources or the selected source.
            bucket["tokens_input"] = r.tokens_input
            bucket["tokens_output"] = r.tokens_output
            bucket["tokens_cache_read"] = r.tokens_cache_read
            bucket["tokens_cache_create"] = r.tokens_cache_create
            bucket["tokens_reasoning"] = r.tokens_reasoning
            bucket["cost_usd"] = r.cost_usd
            bucket["cost_cache"] = cost_cache
            bucket["msgs"] = r.msgs
            if sidecar_id:
                bucket["by_sidecar"][r.sidecar_id] = {
                    "tokens_input": r.tokens_input,
                    "tokens_output": r.tokens_output,
                    "tokens_cache_read": r.tokens_cache_read,
                    "tokens_cache_create": r.tokens_cache_create,
                    "tokens_reasoning": r.tokens_reasoning,
                    "cost_usd": r.cost_usd,
                    "cost_cache": cost_cache,
                    "msgs": r.msgs,
                }
        elif r.model_id != "" and r.sidecar_id == (sidecar_id or ""):
            # Per-model grain for all sources or the selected source.
            bucket["by_model"][r.model_id] = {
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "tokens_cache_read": r.tokens_cache_read,
                "tokens_cache_create": r.tokens_cache_create,
                "tokens_reasoning": r.tokens_reasoning,
                "cost_usd": r.cost_usd,
                "cost_cache": cost_cache,
                "msgs": r.msgs,
            }
        elif not sidecar_id and r.model_id == "" and r.sidecar_id != "":
            # Per-sidecar grain (all models combined) when unfiltered.
            bucket["by_sidecar"][r.sidecar_id] = {
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "tokens_cache_read": r.tokens_cache_read,
                "tokens_cache_create": r.tokens_cache_create,
                "tokens_reasoning": r.tokens_reasoning,
                "cost_usd": r.cost_usd,
                "cost_cache": cost_cache,
                "msgs": r.msgs,
            }
        # Cross-product rows (model_id != '' AND sidecar_id != '') are skipped —
        # those are for detailed analytics, not needed at this endpoint.

    # Stable shape: every entry always exposes lifetime + current year + current month
    # (filled with zero-value buckets when absent).
    month_key_out = f"month_{current_month}"
    year_key_out = f"year_{current_year}"

    def _empty_bucket() -> dict[str, Any]:
        return {
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_cache_read": 0,
            "tokens_cache_create": 0,
            "tokens_reasoning": 0,
            "cost_usd": 0.0,
            "cost_cache": 0.0,
            "msgs": 0,
            "by_model": {},
            "by_sidecar": {},
        }

    # Union identities from the rollup and the live windows so an account that
    # only has events in the current period (not yet in any historical rollup
    # bucket beyond lifetime) still gets an entry.
    idents = set(grouped) | set(live_month) | set(live_year) | set(month_live)
    cumulative = []
    for pid, aid in sorted(idents):
        buckets = grouped.get((pid, aid), {})
        entry: dict[str, Any] = {"provider_id": pid, "account_id": aid}
        entry["lifetime"] = buckets.get("lifetime", _empty_bucket())
        if is_default:
            entry[year_key_out] = live_year.get((pid, aid), _empty_bucket())
            entry[month_key_out] = live_month.get((pid, aid), _empty_bucket())
        elif is_month_live or is_range_live or is_current_month_live:
            # Live aggregation (tz-correct historical or current month, or an
            # arbitrary range); the rollup year bucket isn't fetched here, so
            # report empty.
            entry[year_key_out] = _empty_bucket()
            entry[month_key_out] = month_live.get((pid, aid), _empty_bucket())
        else:
            entry[year_key_out] = buckets.get(year_key_out, _empty_bucket())
            entry[month_key_out] = buckets.get(month_key_out, _empty_bucket())
        # Surface any additional historical buckets the caller didn't filter out
        for k, v in buckets.items():
            if k not in entry:
                entry[k] = v
        cumulative.append(entry)

    return {
        "cumulative": cumulative,
        "current_month_key": month_key_out,
        "current_year_key": year_key_out,
        "generated_at": now.isoformat(),
    }


def _cumulative_bucket_label(period_type: str, period_key: str) -> str:
    """Map (period_type, period_key) into a stable response field name."""
    if period_type == "lifetime":
        return "lifetime"
    return f"{period_type}_{period_key}"


__all__ = ["_cumulative_bucket_label", "_get_cumulative_usage_sync", "get_cumulative_usage"]
