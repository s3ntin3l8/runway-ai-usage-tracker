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
are skipped, so local scraping is never double counted. Remote Cowork sessions (``ccs``, Claude
Desktop's cloud-run Cowork) are missing from every list; a Mac's sidecar reports their ids and they are
fetched one by one and read the same way (``claude_remote_sessions``). While events of the first (coarse) collector
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
from app.core.source_apps import CLAUDE_CODE
from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.models.db import ClaudeCloudCollectorState, ClaudeCloudSessionCheckpoint, UsageEvent
from app.models.schemas import SessionNameIn, UsageEventPush
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services import claude_remote_sessions as remote_sessions
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
from app.services.session_names import upsert_session_names

logger = logging.getLogger(__name__)

CLOUD_KIND = "anthropic_cloud"
LIST_LIMIT = 20
HISTORY_LIMIT = 50
MAX_LIST_PAGES = 200
# Bytes per cycle, and per response. Remote Cowork histories carry tool output: one measured
# session was 185 MB over 230 pages of 50 events, pages up to 6.6 MB. A cycle stops reading
# before a further page could cross the cycle total, and the catch-up cycle continues.
MAX_BYTES = 96 * 1024 * 1024
HISTORY_RESPONSE_BYTES = 16 * 1024 * 1024
# A byte stop counts toward a stall only for a session that read at least this share of the cycle's
# usable bytes itself (see ``_stalls_after_stop``); after MAX_STALL_FACTOR it is skipped.
STALL_BYTE_SHARE = 0.5
CYCLE_TIMEOUT = 300.0
PAGE_PAUSE = 0.25
# Per cycle: history pages and total requests (list, identity and history together; the remote
# Cowork hint pass may add up to MAX_HINTS_PER_CYCLE on top).
HISTORY_PAGES_PER_CYCLE = 40
REQUESTS_PER_CYCLE = 200
# Remote Cowork hints fetched per list pass (one request each; see ``_hinted_rows``).
MAX_HINTS_PER_CYCLE = 50
# Listing the account's claude.ai conversations for remote Cowork ids (hourly; see ``_discover``).
DISCOVERY_TIMEOUT = 60.0
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
    names_updated: int = 0
    remote_sessions: int = 0
    discovered: int = 0
    backlog: bool = False
    stats: Counter[str] = field(default_factory=Counter)
    crosscheck: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class _Row:
    """What one list row says about a cloud session; the title only feeds the session name."""

    session_id: str
    digest: str
    facts: SessionFacts
    last_event_at: datetime | None
    last_event_raw: str | None
    block: dict[str, int] | None
    # Free text: stored as a session name, never logged (hence no repr).
    title: str | None = field(default=None, repr=False)


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


def clear_pending() -> None:
    """Forget the unfinished sessions, so the next cycle lists them afresh (after a reset)."""
    _PENDING.clear()


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


def _block(row: dict[str, Any]) -> dict[str, int] | None:
    metadata = row.get("external_metadata")
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {key: support.count(usage.get(key)) for key, _ in _COUNTERS}
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
            inserted = (
                EventIngestor(session)
                .ingest(events, sidecar_id=support.CLOUD_SIDECAR_ID)
                .events_inserted
            )
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
        super().__init__(
            cast(ClaudeCloudProbeRequest, request),
            client,
            total_byte_limit=MAX_BYTES,
            response_byte_limit=HISTORY_RESPONSE_BYTES,
        )
        # The account's claude.ai cookie, for remote Cowork discovery (read before the exact Code
        # source is selected: inside it the cache only answers for that source).
        self.claude_cookie: str | None = None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        # The endpoint lock is held per request, so a sweep never turns an operator-triggered
        # probe or verification away for longer than one page.
        async with support.gate:
            return await super().get(*args, **kwargs)


def new_probe(account: str, source_id: str, client: httpx.AsyncClient) -> probe.Probe:
    """A read-only probe with the collector's bounds, for callers that list sessions themselves."""
    return _CollectorProbe(account, source_id, client)


async def _list_rows(
    attempt: _CollectorProbe, headers: dict[str, str], report: CycleReport
) -> list[_Row]:
    """Every cloud-runner row of the session list (bridge rows are skipped)."""
    read = support.ListRead()
    try:
        await support.read_session_list(
            attempt,
            headers,
            parse_page=probe.page_with_terminal_omission,
            limit=LIST_LIMIT,
            max_pages=MAX_LIST_PAGES,
            pause=PAGE_PAUSE,
            keep=lambda row: row.get("environment_kind") == CLOUD_KIND,
            into=read,
        )
    finally:
        report.pages += read.pages
        report.listed += read.listed
        report.notes.extend(read.notes)
    return [_row_from(row, attempt.account) for row in read.rows]


def _row_from(row: dict[str, Any], account: str) -> _Row:
    sid = row["id"]
    digest = session_digest(sid)
    raw = row.get("last_event_at")
    return _Row(
        session_id=sid,
        digest=digest,
        facts=_facts(row, account, digest),
        last_event_at=_timestamp(raw),
        last_event_raw=raw if isinstance(raw, str) else None,
        block=_block(row),
        title=title if isinstance(title := row.get("title"), str) else None,
    )


async def _claude_cookie(account: str) -> str | None:
    """The account's claude.ai session cookie, found as ``anthropic_web`` finds it; None without one.

    Same keys in the same order, including a session key pasted into the OAuth token field.
    """
    for kind in ("cookie_sessionKey", "session_cookie"):
        value = await probe.token_cache.get_token("anthropic", kind, account_id=account)
        if isinstance(value, str) and value.strip():
            return value
    value = await probe.token_cache.get_token("anthropic", "oauth_token", account_id=account)
    return value if isinstance(value, str) and value.startswith("sk-ant-sid") else None


async def _discover(attempt: _CollectorProbe, report: CycleReport) -> None:
    """Hints from the account's claude.ai conversations; never fails the cycle."""
    cookie, attempt.claude_cookie = attempt.claude_cookie, None
    if cookie is None or attempt.account_uuid is None or attempt.org_uuid is None:
        return
    try:
        # Its own bound, so a slow claude.ai cannot spend the cycle's timeout.
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            ids, outcome = await remote_sessions.discover_from_conversations(
                attempt.client, cookie, attempt.account, attempt.org_uuid, pause=PAGE_PAUSE
            )
        report.discovered = await asyncio.to_thread(
            remote_sessions.store_discovered, ids, attempt.account_uuid, attempt.org_uuid
        )
    except TimeoutError:
        outcome = "timeout"
    except Exception as exc:  # noqa: BLE001 - discovery is a side input; type only
        outcome = type(exc).__name__
    if outcome != "ok":
        report.notes.append(f"discovery_{outcome}")


async def _hinted_rows(
    attempt: _CollectorProbe, headers: dict[str, str], listed: list[_Row], report: CycleReport
) -> list[_Row]:
    """Remote Cowork sessions a sidecar reported, which no session list returns.

    Only hints for the verified login's own account and organization are fetched, and a fetched
    session is kept only when it is a remote Cowork session (``remote_sessions.is_remote_cowork``),
    so a hint can never make the collector read a local (``bridge``) session. The pass is bounded by
    ``MAX_HINTS_PER_CYCLE`` on its own, not by what the list pass spent: an account whose list alone
    fills the cycle's request budget would otherwise never fetch a hint. History reads the remaining
    budget cannot cover wait for the catch-up cycle. The pass never fails the cycle: a missing
    session is skipped, and a rate limit, timeout or spent byte budget ends the pass, so the listed
    sessions are still read.
    """
    if attempt.account_uuid is None or attempt.org_uuid is None:
        return []
    await _discover(attempt, report)
    try:
        hints = await asyncio.to_thread(
            remote_sessions.load_hints, attempt.account_uuid, attempt.org_uuid, MAX_HINTS_PER_CYCLE
        )
    except SQLAlchemyError:
        report.notes.append("hints_unavailable")
        return []
    known = {row.session_id for row in listed}
    rows: list[_Row] = []
    checked: dict[str, float] = {}
    for index, hint in enumerate(hints):
        if hint.session_id in known:
            # Read through the list; stamp it so it stops taking one of this pass's slots.
            checked[hint.digest] = 0.0
            continue
        if index and PAGE_PAUSE:
            await asyncio.sleep(PAGE_PAUSE)
        attempt.report["stage"] = "hint_get"
        try:
            payload = await attempt.get(
                f"/v1/code/sessions/{quote(hint.session_id, safe='')}", headers
            )
        except probe.ProbeError as exc:
            label = str(exc)
            if label in ("unavailable", "rejected", "unsupported_contract"):
                # A deleted or unreadable session must not stop the others.
                report.notes.append(f"hint_{label}")
                checked[hint.digest] = 0.0
                continue
            if label in ("rate_limited", "response_limit"):
                report.notes.append(f"hint_{label}")
                break
            raise
        except httpx.HTTPError:
            report.notes.append("hint_unavailable")
            break
        checked[hint.digest] = 0.0
        # The single-session read wraps the row; the list returns it bare.
        row = payload.get("response_shape", payload) if isinstance(payload, dict) else None
        if not isinstance(row, dict) or row.get("id") != hint.session_id:
            report.notes.append("hint_unsupported_contract")
            continue
        if not remote_sessions.is_remote_cowork(row):
            report.notes.append("hint_skipped")
            continue
        found = _row_from(row, attempt.account)
        if found.last_event_at is not None:
            checked[hint.digest] = found.last_event_at.timestamp()
        rows.append(found)
    try:
        await asyncio.to_thread(remote_sessions.mark_checked, checked)
    except SQLAlchemyError:
        report.notes.append("hints_unavailable")
    report.remote_sessions = len(rows)
    return rows


def _bytes_spent(attempt: _CollectorProbe) -> bool:
    """Whether one more page could cross the cycle's byte total (the rest waits for catch-up)."""
    return attempt.total_bytes > attempt.total_byte_limit - attempt.response_byte_limit


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


def _stalls_after_stop(
    attempt: _CollectorProbe,
    checkpoint: _Checkpoint,
    *,
    closed_any: bool,
    out_of_pages: bool,
    start_bytes: int,
) -> int:
    """The session's stall count after a read stopped by the page or the byte budget.

    A stall is a turn that did not close within the budget the session had. One cut short because
    other sessions spent the cycle's bytes (it read less than STALL_BYTE_SHARE of the usable bytes) has not
    shown that, so its count is left alone; one that spent most of the bytes itself has, and after
    MAX_STALL_FACTOR it is skipped and noted instead of being re-read every cycle.
    """
    if closed_any:
        return 0
    own_bytes = attempt.total_bytes - start_bytes
    usable = attempt.total_byte_limit - attempt.response_byte_limit
    if out_of_pages or own_bytes >= usable * STALL_BYTE_SHARE:
        return checkpoint.stalls + 1
    return checkpoint.stalls


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
    start_bytes = attempt.total_bytes
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
        out_of_pages = used >= pages_left
        out_of_bytes = not out_of_pages and _bytes_spent(attempt)
        if out_of_pages or out_of_bytes:
            report.stats.update({k: v for k, v in reducer.stats.items() if k != "turns"})
            if out_of_bytes:
                report.notes.append("byte_capped")
            stalls = _stalls_after_stop(
                attempt,
                checkpoint,
                closed_any=closed_any,
                out_of_pages=out_of_pages,
                start_bytes=start_bytes,
            )
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


def _store_names(rows: list[_Row]) -> int:
    names = [
        SessionNameIn(
            provider_id=PROVIDER_ID,
            session_id=row.digest,
            source_app=CLAUDE_CODE,
            name=row.title,
            kind="generated",
        )
        for row in rows
        if row.title and row.title.strip()
    ]
    if not names:
        return 0
    with Session(engine) as session:
        return upsert_session_names(session, names)


async def _update_names(rows: list[_Row], report: CycleReport) -> None:
    """Titles from the list pass become session names; a failure never fails the usage sync."""
    try:
        report.names_updated = await asyncio.to_thread(_store_names, rows)
    except Exception as exc:  # noqa: BLE001 - names are a side output; type only, no message
        report.notes.append(f"names_{type(exc).__name__}")


async def _sync(attempt: _CollectorProbe, headers: dict[str, str], report: CycleReport) -> None:
    rows = _PENDING.get(attempt.account)
    if rows is None:
        rows = await _list_rows(attempt, headers, report)
        rows += await _hinted_rows(attempt, headers, rows, report)
        await _update_names(rows, report)
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
        out_of_bytes = _bytes_spent(attempt)
        if left <= 0 or out_of_bytes:
            if out_of_bytes:
                report.notes.append("byte_capped")
            report.backlog = True
            _PENDING[attempt.account] = [item[0] for item in due[index:]]
            return
        pages_before = report.history_pages
        try:
            pages_used += await _read_session(attempt, headers, row, checkpoint, report, left)
        except probe.ProbeError as exc:
            # The pages a skipped session did read still count against this cycle's budget.
            pages_used += report.history_pages - pages_before
            if str(exc) == "response_limit":
                # A single page larger than HISTORY_RESPONSE_BYTES (the cycle total cannot be the
                # cause: reading stops before it could be crossed). Skip this session, not the
                # others, and say so.
                report.notes.append("session_response_limit")
                continue
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
            if remote_sessions.discovery_due(attempt.account):
                attempt.claude_cookie = await _claude_cookie(attempt.account)
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


_SUCCESS_OUTCOMES = frozenset({"ok", "partial", "truncated"})


def _record_cycle(account: str, report: CycleReport) -> None:
    """Upsert the account's health row. A busy cycle only stamps the time and outcome."""
    with Session(engine) as session:
        row = session.get(ClaudeCloudCollectorState, account)
        if row is None:
            row = ClaudeCloudCollectorState(account_id=account)
        row.last_cycle_at = time.time()
        row.last_outcome = report.outcome
        if report.outcome != "busy":
            row.cycles += 1
            row.last_events = report.events
            row.backlog = 1 if report.backlog else 0
            if report.outcome in _SUCCESS_OUTCOMES:
                row.consecutive_failures = 0
            else:
                row.consecutive_failures += 1
        session.add(row)
        session.commit()


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
        try:
            await asyncio.to_thread(_record_cycle, account, report)
        except Exception as exc:  # never break the cycle or the other accounts
            logger.warning("Claude cloud collector: state write failed (%s)", type(exc).__name__)
        logger.info(
            "Claude cloud collector: outcome=%s list_pages=%d history_pages=%d listed=%d cloud=%d "
            "remote_cowork=%d discovered=%d read=%d new_events=%d names_updated=%d crosscheck=%s stats=%s "
            "notes=%s logins=%s",
            report.outcome,
            report.pages,
            report.history_pages,
            report.listed,
            report.cloud_sessions,
            report.remote_sessions,
            report.discovered,
            report.sessions_read,
            report.events,
            report.names_updated,
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
