"""Server-side collector for Claude Code cloud-runner sessions.

Local scraping covers CLI, Desktop and bridge sessions. Sessions that run on Anthropic's cloud
runner (``environment_kind == "anthropic_cloud"`` in ``GET /v1/code/sessions``) leave no local
transcript, so this collector reads their history with the cached Claude Code login and writes it as
``usage_events``: one event per assistant message and one per turn and model for what the result
record reports beyond those messages (``claude_cloud_events``; the rules are in
``docs/claude-cloud-event-contract.md``).

The session list is only used to find sessions whose ``last_event_at`` moved. Each such session is
read from the last closed turn (a checkpoint: the sequence number of its last result and the cursor of
the page that holds it), so the open tail after it is simply read again; every event id is derived
from the history, so reading twice is harmless. The list row's usage block is a logged cross-check
and never a source.

It reuses the verifier's read-only plumbing (``claude_cloud_probe.Probe``): exact cached source, Code
provenance, no refresh, account and organization checks, fixed host, bounded responses. Bridge rows
are skipped, so local scraping is never double counted. While events of the first (coarse) collector
exist the cycle writes nothing and reports ``migration_required``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, col, func, select

from app.core.db import engine
from app.core.encryption import DecryptionError, encryption_service
from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.models.db import ClaudeCloudSessionCheckpoint, UsageEvent
from app.models.schemas import UsageEventPush
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services.claude_cloud_events import (
    PROVIDER_ID,
    ClaudeCloudHistoryReducer,
    ReducerState,
    SessionFacts,
    session_digest,
)
from app.services.credential_sources import describe_origin_full
from app.services.event_ingestor import EventIngestor
from app.services.maintenance.cloud_coarse_events import coarse_event_where
from app.services.probe_mode import probing

logger = logging.getLogger(__name__)

CLOUD_KIND = "anthropic_cloud"
LIST_LIMIT = 20
HISTORY_LIMIT = 50
MAX_LIST_PAGES = 200
MAX_BYTES = 32 * 1024 * 1024
CYCLE_TIMEOUT = 300.0
PAGE_PAUSE = 0.25
# Per cycle: history pages and total requests (list, identity and history together).
HISTORY_PAGES_PER_CYCLE = 40
REQUESTS_PER_CYCLE = 200
# A session that cannot close one turn within its page budget gets a larger one next time.
MAX_STALL_FACTOR = 10
# While a cycle ends with work left, the next one runs this soon instead of at the interval.
CATCH_UP_DELAY = 120.0
# Messages after the last result are emitted once the session has been quiet this long.
SETTLE_SECONDS = 3600.0

_COUNTERS = (
    ("input_tokens", "tokens_input"),
    ("output_tokens", "tokens_output"),
    ("cache_read_tokens", "tokens_cache_read"),
    ("cache_write_tokens", "tokens_cache_create"),
)


@dataclass
class CycleReport:
    outcome: str = "never_run"
    pages: int = 0
    history_pages: int = 0
    listed: int = 0
    cloud_sessions: int = 0
    sessions_read: int = 0
    events: int = 0
    backlog: bool = False
    stats: Counter[str] = field(default_factory=Counter)
    crosscheck: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class _Row:
    """What one list row says about a cloud session; no title, content or id beyond the read."""

    session_id: str
    digest: str
    facts: SessionFacts
    last_event_at: datetime | None
    last_event_raw: str | None
    block: dict[str, int] | None


@dataclass
class _Checkpoint:
    committed_sequence: int = 0
    cursor: str | None = None
    state: ReducerState = field(default_factory=ReducerState)
    last_event_at: str | None = None
    open_tail: bool = False
    stalls: int = 0
    known: bool = False


# Sessions a cycle did not finish, per account, so catch-up cycles skip the list pass. It is only
# replaced or dropped once a cycle has succeeded, so a failed cycle (a rate limit, a rotated login)
# keeps it; a restart loses it, which costs one extra list pass at the next interval. The
# checkpoints are the truth.
_PENDING: dict[str, list[_Row]] = {}

# Cycles in a row that may retry at the catch-up pace without reading anything while sessions are
# still pending (a login missing from the token cache); then the configured interval applies.
MAX_UNREAD_CATCH_UPS = 3


def has_pending() -> bool:
    """Whether any account still has sessions a cycle left unfinished."""
    return any(_PENDING.values())


class _StorageError(Exception):
    """A database failure while writing events or a checkpoint."""


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 2**53 else None


def _block(row: dict[str, Any]) -> dict[str, int] | None:
    metadata = row.get("external_metadata")
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {key: _count(usage.get(key)) for key, _ in _COUNTERS}
    if all(value is None for value in counts.values()):
        return None
    return {key: value or 0 for key, value in counts.items()}


def _facts(row: dict[str, Any], account: str, digest: str) -> SessionFacts:
    config: dict[str, Any] = row["config"] if isinstance(row.get("config"), dict) else {}
    repos: list[str] = []
    for outcome in config.get("outcomes") or []:
        info = outcome.get("git_info") if isinstance(outcome, dict) else None
        if isinstance(info, dict) and isinstance(info.get("repo"), str):
            repos.append(info["repo"])
    if not repos:
        repos = [
            source["url"]
            for source in config.get("sources") or []
            if isinstance(source, dict) and isinstance(source.get("url"), str)
        ]
    metadata: dict[str, Any] = (
        row["external_metadata"] if isinstance(row.get("external_metadata"), dict) else {}
    )
    current = metadata.get("current_branches")
    branches = (
        [value for value in current.values() if isinstance(value, str) and value]
        if isinstance(current, dict)
        else []
    )
    effort = config.get("effort_level")
    return SessionFacts(
        digest=digest,
        account_id=account,
        repos=tuple(dict.fromkeys(repos)),
        branch=branches[0] if len(branches) == 1 else None,
        effort=effort if isinstance(effort, str) else None,
    )


def _sequence(record: dict[str, Any]) -> int:
    value = record.get("sequence_num")
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if type(value) is int and value >= 0:
        return value
    raise probe.ProbeError("unsupported_contract")


# -- storage (synchronous; callers run these in a worker thread) --------------------------------


def _migration_pending() -> bool:
    """Whether events of the first (coarse) collector still exist (ids ``cloud:<digest>:<n>``)."""
    with Session(engine) as session:
        return (
            session.exec(select(UsageEvent.id).where(*coarse_event_where()).limit(1)).first()
            is not None
        )


def _load_checkpoint(account: str, digest: str) -> _Checkpoint:
    with Session(engine) as session:
        row = session.exec(
            select(ClaudeCloudSessionCheckpoint).where(
                ClaudeCloudSessionCheckpoint.account_id == account,
                ClaudeCloudSessionCheckpoint.session_digest == digest,
            )
        ).first()
        if row is None:
            return _Checkpoint()
        cursor: str | None = None
        if row.committed_cursor_encrypted:
            try:
                value = encryption_service.decrypt_json(row.committed_cursor_encrypted)
            except DecryptionError:
                value = {}
            cursor = value.get("cursor") if isinstance(value, dict) else None
        try:
            state = ReducerState.from_dict(json.loads(row.reducer_state_json or "{}"))
        except (ValueError, TypeError):
            # A checkpoint that cannot be read is not trusted: start the session over. Event ids
            # make the replay harmless.
            return _Checkpoint(known=True, last_event_at=None)
        return _Checkpoint(
            committed_sequence=row.committed_sequence,
            cursor=cursor if isinstance(cursor, str) else None,
            state=state,
            last_event_at=row.last_event_at,
            open_tail=row.open_tail,
            stalls=row.stalls,
            known=True,
        )


def _crosscheck(session: Session, account: str, digest: str, block: dict[str, int] | None) -> str:
    """Compare a settled session's events with the list row's usage block; labels only."""
    if block is None:
        return "no_block"
    sums = session.exec(
        select(
            func.coalesce(func.sum(UsageEvent.tokens_input), 0),
            func.coalesce(func.sum(UsageEvent.tokens_output), 0),
            func.coalesce(func.sum(UsageEvent.tokens_cache_read), 0),
            func.coalesce(func.sum(UsageEvent.tokens_cache_create), 0),
        ).where(
            col(UsageEvent.provider_id) == PROVIDER_ID,
            col(UsageEvent.account_id) == account,
            col(UsageEvent.session_id) == digest,
        )
    ).one()
    pairs = [(block[key], int(total)) for (key, _), total in zip(_COUNTERS, sums, strict=True)]
    higher = any(theirs > ours for theirs, ours in pairs)
    lower = any(theirs < ours for theirs, ours in pairs)
    if higher and lower:
        return "mixed"
    return "block_higher" if higher else "block_lower" if lower else "agree"


def _commit(
    account: str,
    digest: str,
    events: list[UsageEventPush],
    update: dict[str, Any],
    block: dict[str, int] | None,
) -> tuple[int, str | None]:
    """Ingest events, then move the checkpoint (events first: a crash in between replays harmlessly)."""
    with Session(engine) as session:
        inserted = 0
        if events:
            inserted = EventIngestor(session).ingest(events, sidecar_id="server").events_inserted
        row = session.exec(
            select(ClaudeCloudSessionCheckpoint).where(
                ClaudeCloudSessionCheckpoint.account_id == account,
                ClaudeCloudSessionCheckpoint.session_digest == digest,
            )
        ).first() or ClaudeCloudSessionCheckpoint(account_id=account, session_digest=digest)
        if "last_event_at" in update:
            row.last_event_at = update["last_event_at"]
        if "committed_sequence" in update:
            row.committed_sequence = update["committed_sequence"]
            cursor = update.get("cursor")
            row.committed_cursor_encrypted = (
                encryption_service.encrypt_json({"cursor": cursor}) if cursor else None
            )
            row.reducer_state_json = json.dumps(update["state"])
        if "open_tail" in update:
            row.open_tail = update["open_tail"]
        if "stalls" in update:
            row.stalls = update["stalls"]
        row.turns += update.get("turns", 0)
        row.message_events += update.get("message_events", 0)
        row.remainder_events += update.get("remainder_events", 0)
        label: str | None = None
        if update.get("settled"):
            label = _crosscheck(session, account, digest, block)
            row.last_crosscheck = label
        row.last_seen = time.time()
        session.add(row)
        session.commit()
    return inserted, label


async def _commit_async(
    account: str,
    digest: str,
    events: list[UsageEventPush],
    update: dict[str, Any],
    block: dict[str, int] | None,
) -> tuple[int, str | None]:
    try:
        return await asyncio.to_thread(_commit, account, digest, events, update, block)
    except SQLAlchemyError as exc:
        raise _StorageError from exc


# -- network ------------------------------------------------------------------------------------


class _CollectorProbe(probe.Probe):
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
        async with support.gate:
            return await super().get(*args, **kwargs)


async def _list_rows(
    attempt: _CollectorProbe, headers: dict[str, str], report: CycleReport
) -> list[_Row]:
    """Every cloud-runner row of the session list (bridge rows are skipped)."""
    rows: list[_Row] = []
    listed_ids: set[str] = set()
    seen: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": LIST_LIMIT}
        if cursor is not None:
            params["cursor"] = cursor
        attempt.report["stage"] = "page_get"
        payload = await attempt.get("/v1/code/sessions", headers, params)
        attempt.report["stage"] = "page_shape"
        page, cursor = probe.page_with_terminal_omission(payload, LIST_LIMIT)
        report.pages += 1
        report.listed += len(page)
        for row in page:
            sid = row.get("id")
            if not isinstance(sid, str) or not probe.SESSION_ID.fullmatch(sid):
                attempt.report["stage"] = "row_id"
                raise probe.ProbeError("unsupported_contract")
            if sid in listed_ids:
                # One session twice would be read twice; the verifier rejects this too.
                attempt.report["stage"] = "row_duplicate"
                raise probe.ProbeError("unsupported_contract")
            listed_ids.add(sid)
            if row.get("environment_kind") != CLOUD_KIND:
                continue
            digest = session_digest(sid)
            raw = row.get("last_event_at")
            rows.append(
                _Row(
                    session_id=sid,
                    digest=digest,
                    facts=_facts(row, attempt.account, digest),
                    last_event_at=_timestamp(raw),
                    last_event_raw=raw if isinstance(raw, str) else None,
                    block=_block(row),
                )
            )
        if cursor is None:
            if len(page) == LIST_LIMIT:
                # A full last page is ambiguous: the end of the list, or paging that stopped
                # emitting cursors. The note lets the cycle log tell the two apart.
                report.notes.append("full_last_page")
            return rows
        if cursor in seen or report.pages >= MAX_LIST_PAGES:
            report.notes.append("cursor_repeat" if cursor in seen else "max_pages")
            return rows
        seen.add(cursor)
        await asyncio.sleep(PAGE_PAUSE)


def _needs_reading(row: _Row, checkpoint: _Checkpoint, now: float) -> bool:
    if not checkpoint.known or checkpoint.last_event_at is None:
        return True
    if row.last_event_raw is None:
        return checkpoint.open_tail
    if row.last_event_raw != checkpoint.last_event_at:
        return True
    # Unchanged, but messages waiting for a result may have gone quiet long enough to emit.
    return checkpoint.open_tail and _quiet(row, now)


def _quiet(row: _Row, now: float) -> bool:
    return row.last_event_at is not None and now - row.last_event_at.timestamp() >= SETTLE_SECONDS


def _page_budget(checkpoint: _Checkpoint) -> int:
    return HISTORY_PAGES_PER_CYCLE * (1 + min(checkpoint.stalls, MAX_STALL_FACTOR - 1))


async def _read_session(
    attempt: _CollectorProbe,
    headers: dict[str, str],
    row: _Row,
    checkpoint: _Checkpoint,
    report: CycleReport,
    pages_left: int,
) -> int:
    """Read one session's history from its checkpoint; returns the history pages used."""
    account = attempt.account
    reducer = ClaudeCloudHistoryReducer(
        row.facts, ReducerState.from_dict(checkpoint.state.to_dict())
    )
    skip_through = checkpoint.committed_sequence
    cursor = checkpoint.cursor
    used = 0
    previous: int | None = None
    pending: list[UsageEventPush] = []
    turns_closed = 0
    closed_any = False
    quiet = _quiet(row, time.time())
    while True:
        params: dict[str, Any] = {"limit": HISTORY_LIMIT, "sort_order": "asc"}
        if cursor is not None:
            params["cursor"] = cursor
        attempt.report["stage"] = "history_get"
        try:
            payload = await attempt.get(
                f"/v1/code/sessions/{quote(row.session_id, safe='')}/events", headers, params
            )
        except probe.ProbeError as exc:
            if str(exc) != "unavailable" or cursor is None or used > 0:
                raise
            # A stored cursor the API no longer accepts: read the history again from the start,
            # skipping what was already processed (every id is derived, so a replay is harmless).
            report.notes.append("cursor_rejected")
            cursor, previous = None, None
            reducer = ClaudeCloudHistoryReducer(
                row.facts, ReducerState.from_dict(checkpoint.state.to_dict())
            )
            continue
        attempt.report["stage"] = "history_shape"
        records, next_cursor = probe.page_with_terminal_omission(payload, HISTORY_LIMIT)
        used += 1
        report.history_pages += 1
        entry_cursor = cursor
        closed = None
        for record in records:
            attempt.report["stage"] = "sequence"
            number = _sequence(record)
            if previous is not None and number != previous + 1:
                raise probe.ProbeError("unsupported_contract")
            previous = number
            if number <= skip_through:
                continue
            turn_events = reducer.feed(record)
            if (record.get("payload") or {}).get("type") == "result":
                pending.extend(turn_events)
                turns_closed += 1
                closed = (number, reducer.state.to_dict())
        if closed is not None:
            closed_any = True
            inserted, _ = await _commit_async(
                account,
                row.digest,
                pending,
                {
                    "committed_sequence": closed[0],
                    "cursor": entry_cursor,
                    "state": closed[1],
                    "turns": turns_closed,
                    "message_events": sum(1 for e in pending if ":m:" in e.event_id),
                    "remainder_events": sum(1 for e in pending if ":r:" in e.event_id),
                },
                row.block,
            )
            report.events += inserted
            pending = []
            turns_closed = 0
            skip_through = closed[0]
            checkpoint.committed_sequence = closed[0]
        if next_cursor is None:
            break
        if used >= pages_left:
            report.stats.update({k: v for k, v in reducer.stats.items() if k != "turns"})
            stalls = 0 if closed_any else checkpoint.stalls + 1
            if stalls >= MAX_STALL_FACTOR:
                # No turn closed even with the largest budget: this one is longer than a cycle can
                # read, and re-reading it every cycle would starve the account. Skip the session
                # until it changes again (its last_event_at is recorded as handled) and say so.
                report.notes.append("stalled_turn_skipped")
                await _commit_async(
                    account,
                    row.digest,
                    [],
                    {"stalls": 0, "last_event_at": row.last_event_raw, "open_tail": False},
                    None,
                )
                return used
            # Out of budget with the history unfinished: keep what is committed and come back soon.
            report.backlog = True
            await _commit_async(account, row.digest, [], {"stalls": stalls}, None)
            return used
        cursor = next_cursor
        await asyncio.sleep(PAGE_PAUSE)
    # The whole history is read. Messages after the last result are emitted once the session is
    # quiet; their turn then has no remainder.
    tail = reducer.tail_events() if (reducer.open_tail and quiet) else []
    inserted, label = await _commit_async(
        account,
        row.digest,
        tail,
        {
            "last_event_at": row.last_event_raw,
            "open_tail": reducer.open_tail and not quiet,
            "stalls": 0,
            "message_events": len(tail),
            "settled": not (reducer.open_tail and not quiet),
        },
        row.block,
    )
    report.events += inserted
    if label is not None:
        report.crosscheck[label] += 1
    report.stats.update({k: v for k, v in reducer.stats.items() if k != "turns"})
    return used


async def _sync(attempt: _CollectorProbe, headers: dict[str, str], report: CycleReport) -> None:
    rows = _PENDING.get(attempt.account)
    if rows is None:
        rows = await _list_rows(attempt, headers, report)
    else:
        report.notes.append("backlog_resumed")
    report.cloud_sessions = len(rows)
    now = time.time()
    due: list[tuple[_Row, _Checkpoint]] = []
    for row in rows:
        checkpoint = await asyncio.to_thread(_load_checkpoint, attempt.account, row.digest)
        if _needs_reading(row, checkpoint, now):
            due.append((row, checkpoint))
    due.sort(
        key=lambda item: item[0].last_event_at or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    pages_used = 0
    for index, (row, checkpoint) in enumerate(due):
        left = REQUESTS_PER_CYCLE - int(attempt.report["requests"])
        if checkpoint.stalls:
            # A session that could not close a turn within the usual budget gets a larger one,
            # but never more than the cycle's request cap.
            left = min(_page_budget(checkpoint), left)
        else:
            left = min(_page_budget(checkpoint), HISTORY_PAGES_PER_CYCLE - pages_used, left)
        if left <= 0:
            report.backlog = True
            _PENDING[attempt.account] = [item[0] for item in due[index:]]
            return
        try:
            pages_used += await _read_session(attempt, headers, row, checkpoint, report, left)
        except probe.ProbeError as exc:
            if str(exc) != "unsupported_contract":
                raise
            # One session's history that breaks the contract does not stop the others.
            report.notes.append(f"session_{attempt.report.get('stage')}")
            continue
        if report.backlog:
            # Out of budget inside this session: it and everything after it wait for the next cycle.
            _PENDING[attempt.account] = [item[0] for item in due[index:]]
            return
        report.sessions_read += 1
    # Everything due was read: nothing is left to resume.
    _PENDING.pop(attempt.account, None)


async def _read(attempt: _CollectorProbe, source_id: str, report: CycleReport) -> bool:
    """The network and storage half of a cycle; sets ``report.outcome`` and returns False on failure."""
    try:
        async with asyncio.timeout(CYCLE_TIMEOUT):
            attempt.tokens, attempt.expected_org, attempt.authority = await probe.load_source(
                attempt.account, source_id
            )
            async with probe.token_cache.using_source("anthropic", attempt.account, source_id):
                headers = await attempt.identity()
                try:
                    await _sync(attempt, headers, report)
                except (probe.ProbeError, httpx.HTTPError, ValueError, RecursionError):
                    await attempt.check_source()
                    raise
                # Results are only trusted while the exact source is still the one used.
                attempt.report["stage"] = "recheck"
                await attempt.check_source()
                return True
    except probe.ProbeError as exc:
        report.outcome = str(exc)
        # Which step failed: a fixed label, never a value from the response.
        report.detail = str(attempt.report.get("stage") or "")
    except _StorageError:
        report.outcome = "storage_error"
    except (TimeoutError, httpx.TimeoutException):
        report.outcome = "timeout"
    except SQLAlchemyError:
        report.outcome = "source_unavailable"
    except httpx.HTTPError:
        report.outcome = "unavailable"
    except (ValueError, RecursionError):
        report.outcome = "unsupported_contract"
    return False


async def collect_account(
    account: str,
    source_id: str,
    client: httpx.AsyncClient,
    *,
    migration_pending: bool | None = None,
) -> CycleReport:
    """One bounded cycle for one account through one exact cached source.

    ``migration_pending`` lets a caller that runs several accounts check once; None checks here.
    """
    report = CycleReport()
    if support.gate.locked():
        # An operator-triggered probe or verification holds the endpoint: add no load.
        report.outcome = "busy"
        return report
    try:
        if migration_pending is None:
            migration_pending = await asyncio.to_thread(_migration_pending)
        if migration_pending:
            report.outcome = "migration_required"
            return report
    except SQLAlchemyError:
        report.outcome = "storage_error"
        return report
    attempt = _CollectorProbe(account, source_id, client)
    try:
        with probing():
            succeeded = await _read(attempt, source_id, report)
    except Exception as exc:  # one account's failure must not abort the others
        report.outcome = "storage_error" if isinstance(exc, SQLAlchemyError) else "internal_error"
        return report
    if succeeded:
        report.outcome = "partial" if report.backlog else "ok"
        if "max_pages" in report.notes or "cursor_repeat" in report.notes:
            report.outcome = "truncated"
    return report


async def collect_all(client: httpx.AsyncClient) -> dict[str, CycleReport]:
    """Run one cycle for every account that has a usable Claude Code login, once per account."""
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
    pending: bool | None = None
    if by_account:
        try:
            pending = await asyncio.to_thread(_migration_pending)
        except SQLAlchemyError:
            pending = None  # each account's cycle then reports the storage error itself
    for account, sources in by_account.items():
        attempts = logins.setdefault(account, [])
        # Two logins of one account see the same sessions: the first usable one is enough.
        for source_id, kind in sorted(sources):
            report = await collect_account(account, source_id, client, migration_pending=pending)
            # A short one-way digest tells two logins of one kind apart without naming either.
            detail = f"/{report.detail}" if report.detail else ""
            attempts.append(f"{kind}#{session_digest(source_id)[:6]}:{report.outcome}{detail}")
            if report.outcome in ("ok", "partial", "truncated", "migration_required"):
                reports[account] = report
                break
            reports.setdefault(account, report)
            if report.outcome == "busy":
                break
    for account, report in reports.items():
        logger.info(
            "Claude cloud collector: outcome=%s list_pages=%d history_pages=%d listed=%d cloud=%d "
            "read=%d new_events=%d crosscheck=%s stats=%s notes=%s logins=%s",
            report.outcome,
            report.pages,
            report.history_pages,
            report.listed,
            report.cloud_sessions,
            report.sessions_read,
            report.events,
            ",".join(f"{k}:{v}" for k, v in sorted(report.crosscheck.items())) or "-",
            ",".join(f"{k}:{v}" for k, v in sorted(report.stats.items())) or "-",
            ",".join(report.notes) or "-",
            ",".join(logins.get(account, [])) or "-",
        )
    return reports


class ClaudeCloudCollector:
    """Background task that runs ``collect_all`` on an interval (sooner while work is left)."""

    def __init__(self, interval_seconds: int):
        self._interval = max(60, interval_seconds)
        self._task: asyncio.Task | None = None
        self._running = False
        # Consecutive cycles that read nothing while sessions stayed pending.
        self._unread_cycles = 0

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

    def _next_delay(self, reports: dict[str, CycleReport]) -> float:
        """Catch-up pace while work is left, even if this cycle could not see its login."""
        backlog = any(report.backlog for report in reports.values())
        read = any(report.sessions_read for report in reports.values())
        pending = has_pending()
        if backlog:
            self._unread_cycles = 0
            return min(self._interval, CATCH_UP_DELAY)
        if not pending:
            self._unread_cycles = 0
            return self._interval
        if read:
            self._unread_cycles = 0
            return min(self._interval, CATCH_UP_DELAY)
        self._unread_cycles += 1
        if self._unread_cycles <= MAX_UNREAD_CATCH_UPS:
            return min(self._interval, CATCH_UP_DELAY)
        if self._unread_cycles == MAX_UNREAD_CATCH_UPS + 1:
            logger.info("Claude cloud collector: unread_catch_up_capped, back to the interval")
        return self._interval

    async def _run_loop(self) -> None:
        # The token cache fills as sidecars report; wait one interval-sized beat so the first
        # sweep finds the login rather than an empty cache.
        await asyncio.sleep(min(self._interval, 120))
        while self._running:
            delay: float = self._interval
            try:
                async with httpx.AsyncClient(
                    timeout=5, follow_redirects=False, trust_env=False
                ) as client:
                    reports = await collect_all(client)
                delay = self._next_delay(reports)
            except Exception as exc:
                logger.warning("Claude cloud collector error: %s", type(exc).__name__)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
