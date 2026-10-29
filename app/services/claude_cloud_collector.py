"""Server-side collector for Claude Code cloud-runner sessions.

Local scraping covers CLI, Desktop and bridge sessions. Sessions that run on Anthropic's cloud
runner (``environment_kind == "anthropic_cloud"`` in ``GET /v1/code/sessions``) leave no local
transcript, but their list row can carry ``external_metadata.usage``. This collector pages the
session list with the cached Claude Code login, keeps the last block it saw per session, and
writes only the increase since the previous sweep as ``usage_events``.

It reuses the verifier's read-only plumbing (``claude_cloud_probe._Probe``): exact cached
source, Code provenance, no refresh, account and organization checks, fixed host, bounded
responses. Bridge rows are skipped, so local scraping is never double counted. The block has no
per-turn timing, so an increase is dated by the row's ``updated_at`` (or the sweep time).

A block that shrinks writes nothing and is counted, never subtracted. A counter the API did not
report stays unknown in the state row; the event it feeds carries 0 for it, because an event
cannot hold "unknown".
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.core.db import engine
from app.core.source_apps import CLAUDE_CODE
from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.models.db import ClaudeCloudCollectorState, ClaudeCloudSessionUsage
from app.models.schemas import UsageEventPush
from app.services import claude_cloud_probe as probe
from app.services.credential_sources import describe_origin_full
from app.services.event_ingestor import EventIngestor
from app.services.model_normalization import normalize_anthropic_model
from app.services.probe_mode import probing

logger = logging.getLogger(__name__)

CLOUD_KIND = "anthropic_cloud"
PAGE_LIMIT = 20
MAX_PAGES = 200
MAX_BYTES = 32 * 1024 * 1024
CYCLE_TIMEOUT = 120.0
PAGE_PAUSE = 0.2
# A row this much older than the high-water mark counts as already processed.
HIGH_WATER_MARGIN = timedelta(minutes=10)
# Every Nth cycle ignores the high-water mark, in case ``updated_at`` does not move with usage.
FULL_SWEEP_EVERY = 24
EVENT_ENTRYPOINT = "cloud"

_MODEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# (block key in the API row, attribute on ClaudeCloudSessionUsage / UsageEventPush)
_FIELDS = (
    ("input_tokens", "tokens_input"),
    ("output_tokens", "tokens_output"),
    ("cache_read_tokens", "tokens_cache_read"),
    ("cache_write_tokens", "tokens_cache_create"),
)


@dataclass
class SessionObservation:
    """What one list row says about a cloud session; no title, id or content."""

    session_id: str
    usage: dict[str, int | None]
    model: str | None
    updated_at: datetime | None


@dataclass
class CycleReport:
    outcome: str = "never_run"
    pages: int = 0
    listed: int = 0
    cloud_sessions: int = 0
    events: int = 0
    regressions: int = 0
    notes: list[str] = field(default_factory=list)
    detail: str = ""


def session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 2**53 else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_usage(row: dict[str, Any]) -> dict[str, int | None] | None:
    metadata = row.get("external_metadata")
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {key: _count(usage.get(key)) for key, _ in _FIELDS}
    return counts if any(value is not None for value in counts.values()) else None


def read_model(row: dict[str, Any]) -> str | None:
    metadata = row.get("external_metadata")
    value = row.get("last_served_model")
    if value is None and isinstance(metadata, dict):
        value = metadata.get("last_served_model")
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def usage_delta(
    previous: dict[str, int | None] | None, current: dict[str, int | None]
) -> tuple[dict[str, int], bool]:
    """The increase of each reported counter, and whether any counter went backwards."""
    delta: dict[str, int] = {}
    for key, _ in _FIELDS:
        new = current.get(key)
        if new is None:
            continue
        base = (previous or {}).get(key) or 0
        if new < base:
            return {}, True
        if new > base:
            delta[key] = new - base
    return delta, False


class _CollectorProbe(probe._Probe):
    def __init__(self, account: str, source_id: str, client: httpx.AsyncClient):
        request = SimpleNamespace(
            account_id=account,
            source_id=source_id,
            organization_id=None,
            session_id=None,
            max_history_pages=0,
            report_version=1,
        )
        super().__init__(cast(ClaudeCloudProbeRequest, request), client, total_byte_limit=MAX_BYTES)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        # The endpoint lock is held per request, so a sweep never turns an operator-triggered
        # probe or verification away for longer than one page.
        async with probe._gate:
            return await super().get(*args, **kwargs)


async def _sweep(
    attempt: _CollectorProbe, high_water: datetime | None, full: bool, report: CycleReport
) -> tuple[list[SessionObservation], datetime | None, bool, bool]:
    """Page the session list.

    Returns the cloud rows, the newest ``updated_at``, whether the list was read to a safe
    stopping point, and whether every row seen was newest-first.
    """
    headers = await attempt.identity()
    observed: list[SessionObservation] = []
    newest: datetime | None = None
    previous: datetime | None = None
    ordered = True
    seen: set[str] = set()
    listed_ids: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": PAGE_LIMIT}
        if cursor is not None:
            params["cursor"] = cursor
        attempt.report["stage"] = "page_get"
        payload = await attempt.get("/v1/code/sessions", headers, params)
        attempt.report["stage"] = "page_shape"
        rows, cursor = probe._page(payload, PAGE_LIMIT)
        attempt.report["stage"] = "rows"
        report.pages += 1
        report.listed += len(rows)
        already_processed = bool(rows) and high_water is not None
        for row in rows:
            sid = row.get("id")
            if not isinstance(sid, str) or not probe._SESSION_ID.fullmatch(sid):
                attempt.report["stage"] = "row_id"
                raise probe.ProbeError("unsupported_contract")
            if sid in listed_ids:
                # One session twice would double count it; the verifier rejects this too.
                attempt.report["stage"] = "row_duplicate"
                raise probe.ProbeError("unsupported_contract")
            listed_ids.add(sid)
            updated = _timestamp(row.get("updated_at"))
            if updated is None:
                ordered = already_processed = False
            else:
                if previous is not None and updated > previous:
                    ordered = False
                previous = updated
                newest = updated if newest is None or updated > newest else newest
                if high_water is None or updated > high_water - HIGH_WATER_MARGIN:
                    already_processed = False
            if row.get("environment_kind") != CLOUD_KIND:
                continue
            usage = read_usage(row)
            if usage is not None:
                observed.append(SessionObservation(sid, usage, read_model(row), updated))
        if cursor is None:
            return observed, newest, True, ordered
        # An early stop needs the pages read so far to be newest-first. Pages not yet read can
        # still disagree; the periodic full sweep (FULL_SWEEP_EVERY) is the backstop for that.
        if not full and ordered and already_processed:
            return observed, newest, True, ordered
        if cursor in seen or report.pages >= MAX_PAGES:
            report.notes.append("cursor_repeat" if cursor in seen else "max_pages")
            return observed, newest, False, ordered
        seen.add(cursor)
        await asyncio.sleep(PAGE_PAUSE)


def _apply(
    account: str,
    observed: list[SessionObservation],
    newest: datetime | None,
    advance: bool,
    report: CycleReport,
    now: float,
) -> None:
    """Write the increases as usage events, then move the stored blocks forward.

    The events go first and carry an identity derived from the session and its revision, so a
    crash between the two steps replays the same increase as a duplicate, never twice.
    """
    with Session(engine) as session:
        digests = {item.session_id: session_digest(item.session_id) for item in observed}
        stored = {
            row.session_digest: row
            for row in session.exec(
                select(ClaudeCloudSessionUsage).where(ClaudeCloudSessionUsage.account_id == account)
            ).all()
            if row.session_digest in digests.values()
        }
        pushes: list[UsageEventPush] = []
        moves: list[tuple[ClaudeCloudSessionUsage, dict[str, int | None], str | None]] = []
        for item in observed:
            digest = digests[item.session_id]
            row = stored.get(digest) or ClaudeCloudSessionUsage(
                account_id=account, session_digest=digest
            )
            previous = (
                {key: getattr(row, attr) for key, attr in _FIELDS} if row.id is not None else None
            )
            delta, regressed = usage_delta(previous, item.usage)
            if regressed:
                # Nothing is written for a shrink. Only a shrink seen on two consecutive sweeps
                # becomes the baseline (counters that restarted count again once they climb); a
                # one-sweep dip that recovers is not billed twice.
                row.regressions += 1
                report.regressions += 1
                row.shrunk_streak += 1
                if row.shrunk_streak >= 2:
                    row.shrunk_streak = 0
                    moves.append((row, item.usage, item.model))
                else:
                    row.last_seen = now
                    session.add(row)
                continue
            row.shrunk_streak = 0
            if delta:
                moment = item.updated_at or datetime.fromtimestamp(now, UTC)
                pushes.append(
                    UsageEventPush(
                        provider_id="anthropic",
                        account_id=account,
                        event_id=f"cloud:{digest}:{row.revision + 1}",
                        ts=moment.isoformat(),
                        model_id=normalize_anthropic_model(item.model or ""),
                        source_app=CLAUDE_CODE,
                        session_id=digest,
                        entrypoint=EVENT_ENTRYPOINT,
                        tokens_input=delta.get("input_tokens", 0),
                        tokens_output=delta.get("output_tokens", 0),
                        tokens_cache_read=delta.get("cache_read_tokens", 0),
                        tokens_cache_create=delta.get("cache_write_tokens", 0),
                    )
                )
            moves.append((row, item.usage, item.model))
        if pushes:
            EventIngestor(session).ingest(pushes, sidecar_id="server")
        report.events = len(pushes)
        pushed = {push.session_id for push in pushes}
        for row, usage, model in moves:
            for key, attr in _FIELDS:
                if usage[key] is not None:
                    setattr(row, attr, usage[key])
            row.model_id = model or row.model_id
            row.last_seen = now
            if row.session_digest in pushed:
                row.revision += 1
            session.add(row)
        state = session.get(ClaudeCloudCollectorState, account) or ClaudeCloudCollectorState(
            account_id=account
        )
        state.cycles += 1
        state.last_cycle_at = now
        state.last_events = report.events
        state.last_regressions = report.regressions
        if advance and newest is not None:
            kept = _timestamp(state.high_water)
            state.high_water = (max(newest, kept) if kept else newest).isoformat()
        session.add(state)
        session.commit()


def _state(account: str) -> tuple[datetime | None, int]:
    with Session(engine) as session:
        state = session.get(ClaudeCloudCollectorState, account)
        return (_timestamp(state.high_water), state.cycles) if state else (None, 0)


def _record_outcome(account: str, outcome: str, now: float) -> None:
    with Session(engine) as session:
        state = session.get(ClaudeCloudCollectorState, account) or ClaudeCloudCollectorState(
            account_id=account
        )
        state.last_outcome = outcome
        state.last_cycle_at = now
        session.add(state)
        session.commit()


async def _read(
    attempt: _CollectorProbe,
    source_id: str,
    high_water: datetime | None,
    full: bool,
    report: CycleReport,
) -> tuple[list[SessionObservation], datetime | None, bool, bool] | None:
    """The network half of a sweep; sets ``report.outcome`` and returns None on failure."""
    try:
        async with asyncio.timeout(CYCLE_TIMEOUT):
            attempt.tokens, attempt.expected_org, attempt.authority = await probe._load_source(
                attempt.account, source_id
            )
            async with probe.token_cache.using_source("anthropic", attempt.account, source_id):
                try:
                    result = await _sweep(attempt, high_water, full, report)
                except (probe.ProbeError, httpx.HTTPError, ValueError, RecursionError):
                    await attempt.check_source()
                    raise
                # Results are discarded unless the exact source is still the one used.
                await attempt.check_source()
                return result
    except probe.ProbeError as exc:
        report.outcome = str(exc)
        # Which step of the sweep failed: a fixed label, never a value from the response.
        report.detail = str(attempt.report.get("stage") or "")
    except (TimeoutError, httpx.TimeoutException):
        report.outcome = "timeout"
    except SQLAlchemyError:
        report.outcome = "source_unavailable"
    except httpx.HTTPError:
        report.outcome = "unavailable"
    except (ValueError, RecursionError):
        report.outcome = "unsupported_contract"
    return None


def _record(account: str, outcome: str, now: float) -> None:
    try:
        _record_outcome(account, outcome, now)
    except SQLAlchemyError:
        logger.warning("Claude cloud collector could not record its outcome")


async def collect_account(account: str, source_id: str, client: httpx.AsyncClient) -> CycleReport:
    """One bounded sweep for one account through one exact cached source."""
    report = CycleReport()
    if probe._gate.locked():
        # An operator-triggered probe or verification holds the endpoint: add no load.
        report.outcome = "busy"
        return report
    high_water, cycles = await asyncio.to_thread(_state, account)
    full = high_water is None or cycles % FULL_SWEEP_EVERY == 0
    attempt = _CollectorProbe(account, source_id, client)
    with probing():
        sweep = await _read(attempt, source_id, high_water, full, report)
    now = time.time()
    if sweep is None:
        await asyncio.to_thread(_record, attempt.account, report.outcome, now)
        return report
    observed, newest, complete, ordered = sweep
    report.cloud_sessions = len(observed)
    try:
        # A list that could not be read to its end still advances the mark when what was read
        # was newest-first, so one oversized list does not pin the collector to full sweeps.
        await asyncio.to_thread(
            _apply, attempt.account, observed, newest, complete or ordered, report, now
        )
    except Exception as exc:  # one account's failure must not abort the others
        report.outcome = "storage_error" if isinstance(exc, SQLAlchemyError) else "internal_error"
        await asyncio.to_thread(_record, attempt.account, report.outcome, now)
        return report
    report.outcome = "ok" if complete else "truncated"
    await asyncio.to_thread(_record, attempt.account, report.outcome, now)
    return report


async def collect_all(client: httpx.AsyncClient) -> dict[str, CycleReport]:
    """Sweep every account that has a usable Claude Code login, once per account."""
    reports: dict[str, CycleReport] = {}
    candidates = await probe.token_cache.get_account_source_candidates("anthropic")
    by_account: dict[str, list[tuple[str, str]]] = {}
    for candidate in candidates:
        slot, source_id = candidate.get("account_slot"), candidate.get("source_id")
        if isinstance(slot, str) and isinstance(source_id, str):
            origin = candidate.get("credential_origin")
            kind = describe_origin_full(origin if isinstance(origin, str) else None).kind
            by_account.setdefault(slot, []).append((source_id, kind))
    logins: dict[str, list[str]] = {}
    for account, sources in by_account.items():
        attempts = logins.setdefault(account, [])
        # Two logins of one account see the same sessions: the first usable one is enough.
        for source_id, kind in sorted(sources):
            report = await collect_account(account, source_id, client)
            # A short one-way digest tells two logins of one kind apart without naming either.
            detail = f"/{report.detail}" if report.detail else ""
            attempts.append(f"{kind}#{session_digest(source_id)[:6]}:{report.outcome}{detail}")
            if report.outcome in ("ok", "truncated"):
                reports[account] = report
                break
            reports.setdefault(account, report)
            if report.outcome == "busy":
                break
    for account, report in reports.items():
        logger.info(
            "Claude cloud collector: outcome=%s pages=%d listed=%d cloud=%d events=%d regressions=%d notes=%s logins=%s",
            report.outcome,
            report.pages,
            report.listed,
            report.cloud_sessions,
            report.events,
            report.regressions,
            ",".join(report.notes) or "-",
            ",".join(logins.get(account, [])) or "-",
        )
    return reports


class ClaudeCloudCollector:
    """Background task that runs ``collect_all`` on an interval."""

    def __init__(self, interval_seconds: int):
        self._interval = max(60, interval_seconds)
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Claude cloud collector started: interval=%ss", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            logger.debug("Claude cloud collector cancelled during shutdown")
        self._task = None

    async def _run_loop(self) -> None:
        # The token cache fills as sidecars report; wait one interval-sized beat so the first
        # sweep finds the login rather than an empty cache.
        await asyncio.sleep(min(self._interval, 120))
        while self._running:
            try:
                async with httpx.AsyncClient(
                    timeout=5, follow_redirects=False, trust_env=False
                ) as client:
                    await collect_all(client)
            except Exception as exc:
                logger.warning("Claude cloud collector error: %s", type(exc).__name__)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
