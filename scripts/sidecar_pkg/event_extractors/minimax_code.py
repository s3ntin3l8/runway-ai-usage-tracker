"""Parse the MiniMax Code runtime store into UsageEventPush records.

The MiniMax Code runtime (the ``mavis`` agent shipped with the MiniMax Code
CLI and desktop app) keeps its own usage ledger in a SQLite store under its
app data dir::

    ~/.minimax/v2/sqlite/runtime-state.sqlite   (+ -wal, -shm)

The runtime anchors that dir on ``homedir()`` — verified in the installed
bundle (``join(homeDir ?? homedir(), ".minimax")``) — so the path is the
same on Linux, macOS and Windows. That default is only the *default*: the
runtime also scopes the dir per profile (``~/.minimax-<profile>``), honours
environment overrides and used to live in ``~/.mavis`` before it migrated.
Every store that exists is read, each with its own cursor; see
:func:`discover_runtime_stores` and ``_data_roots``.

Path discovery lives in ``minimax_code_roots``, the ledger reads in
``minimax_code_ledger``, and the table names in ``minimax_code_schema``.
This module keeps the cursor and the parser.

The path and the extraction were live-verified on macOS (probe PASS, 99.97 %
of the ledger rows joined, the model resolved for every row) and on Linux
(full extraction verified against a live store). Windows is live-verified too:
the probe passes against a live store on a Windows host, the model resolves
for every row, and the cursor lands in ``%APPDATA%\\runway\\sidecar``.
The desktop app's macOS / Windows app-data roots are probed as well; the app's
Electron profile dir is ``MiniMax Agent`` on both platforms — it holds the
Chromium profile and never the ledger.

Three tables matter:

``local_runtime_token_usage``
    One row per **API request** — not per turn; a single turn issues dozens
    of requests. ``id`` is an AUTOINCREMENT primary key, ``ts`` is the
    request time in milliseconds, ``model`` is always NULL, and the token
    columns are Anthropic-style **per-request** values
    (``raw.totalTokens == input + output + cacheRead + cacheWrite`` for every
    row). ``cost_usd`` is always 0.0 — the M Plan is a subscription — so the
    events carry ``cost_usd=None`` and the server prices them from
    ``provider_pricing``.

``local_runtime_message_rows``
    One row per stored message. ``data_json`` carries ``usage``,
    ``tool_calls``, ``finish_reason``, ``canonical_message_id`` and
    ``context_usage_telemetry.model``.

``local_runtime_sessions``
    One row per session. ``workspace_dir`` is the working directory and
    ``extra_data_json`` carries ``effectiveModel`` plus
    ``effectiveModelThinking.effort``.

Why the cursor is an id and not a timestamp
-------------------------------------------

``ts`` is **not** globally monotonic — sessions interleave, so the table has
hundreds of backward steps. A ``ts > since`` cursor therefore drops rows:
simulating this extractor's cadence over a real store, a timestamp cursor
lost 14.3 % of the rows at a 5-row cadence and 1.4 % even at 100 rows per
cycle, while the ``id`` cursor lost none. The extractor keeps its own
**id-based cursor** in a provider-local state file
(``~/.config/runway/sidecar/minimax_code_watermark.json``), keyed by the
database path because ``id`` is only comparable within one file.

The cursor advances only after the server confirms the ingest (see
:func:`commit_pending_state` and :func:`discard_pending_state`), so a failed
push re-sends the same rows instead of skipping them.

Why the event id does not depend on the join
---------------------------------------------

``event_id`` is derived from the ledger row alone:
``mcode:<session>:<turn>:<ts>:<input>:<output>:<cache_read>``. The
runtime's own ``canonical_message_id`` would be prettier, but a request's
message row can land in the store *after* the request itself: the newest
rows are unmatched for a few seconds. An id that depends on the join
would then change between two attempts at the same row and the server
would count the request twice.

Every part of the id is content-derived, which buys two properties:

- **Stable across cycles.** Two requests of one turn that share a
  millisecond are told apart by their token counts, so a pair split by
  the per-cycle row cap still gets two distinct ids. A tiebreak that
  depended on the batch (e.g. "append the row id when the base id was
  already seen") would collapse the second one onto the first across a
  page boundary, and the server would silently drop its tokens.
- **Stable across a rebuilt store.** The runtime recreates its ledger
  when it migrates or resets; a request that comes back with the same
  timestamp and the same usage keeps its id, so the server deduplicates
  the replay instead of counting it twice. (The ledger's own row id
  would *not* survive that: it restarts with the table.)

The one case the id cannot separate is two rows with an identical
timestamp *and* identical usage — a duplicate log entry the server would
deduplicate anyway.

Why a background task is filed under its parent session
-------------------------------------------------------

The runtime spawns a background task as a **child session**: its own session
id, a ``parent_session_id`` pointing at the session that spawned it, an
``agent_name`` naming the agent and a ``purpose`` naming the task. The
desktop app runs these in the background, so their rows are real requests
against the user's plan quota that used to land as *main-thread* traffic of
a session nobody was looking at.

Claude Code models the same work as a sidechain: the message carries the
**parent's** session id plus an attribution agent (see the sidechain fixture
in ``tests/fixtures/anthropic-sidechain-sample.jsonl``). The extractor
reproduces that shape: a child's rows are filed under the parent and tagged
``subagent_type="Explore"``, so the Sessions view splits them out of the
parent's main thread exactly like a sidechain.

Two properties make this safe:

- The **event id keeps the child's own session id**. It is derived from the
  ledger row alone, so it does not change when the attribution changes —
  every row collected before this change keeps its id, and a plain re-push
  deduplicates on it instead of counting the row twice. A re-push under a
  *different* account is a retag: the server refreshes the stored row from
  the sidecar's payload, so a sidecar that has not been upgraded yet would
  put the row back on the child. The repair heals it.
- The parent-link columns are **optional**. A store written by a runtime
  that predates them keeps working (no attribution, no crash); a parent
  that is not in the store is never invented.
"""

from __future__ import annotations

import json
import logging
import os
import platform  # noqa: F401 — tests patch minimax_code.platform.system
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.core.source_apps import MINIMAX_CODE  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from scripts.sidecar_pkg.session_names import make_record  # noqa: E402
from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor  # noqa: E402

logger = logging.getLogger("runway.sidecar.minimax_code")

# The probe, the repair script and the tests import these from this module.
__all__ = [
    "INTERNAL_TURN_PREFIX",
    "PROVIDER_ID",
    "SUBAGENT_TYPE_PLAN",
    "_DATA_DIR_ENV_VARS",
    "_REQUIRED_COLUMNS",
    "_data_roots",
    "background_provenance",
    "background_session_links",
    "build_event_id",
    "commit_pending_state",
    "discard_pending_state",
    "discover_runtime_stores",
    "parse_minimax_code_events",
    "parse_minimax_code_session_names",
]

from scripts.sidecar_pkg.minimax_code_ledger import (
    _agent_label,
    _bootstrap_last_id,
    _child_attribution,
    _effort,
    _load_message_index,
    _load_sessions,
    _max_row_id,
    _resolve_model,
    _schema_ok,
    _stop_reason,
    _table_columns,
    _tool_names,
    background_provenance,
    background_session_links,
    build_event_id,
)
from scripts.sidecar_pkg.minimax_code_roots import (
    _DATA_DIR_ENV_VARS,
    _data_roots,
    _default_state_path,
    discover_runtime_stores,
)
from scripts.sidecar_pkg.minimax_code_schema import (
    _MAX_ABANDONED_RECORDS,
    _MAX_ROWS_PER_CYCLE,
    _MAX_SKIP_ATTEMPTS,
    _OPTIONAL_USAGE_COLUMNS,
    _REQUIRED_COLUMNS,
    _SESSIONS_TABLE,
    _TOKEN_USAGE_TABLE,
    INTERNAL_TURN_PREFIX,
    PROVIDER_ID,
    SUBAGENT_TYPE_PLAN,
)


def _load_state(path: Path) -> dict[str, Any]:
    """Read the cursor state file; a broken file reads as "no cursor"."""
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return {}
        data = json.loads(content)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read MiniMax Code cursor state from %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("MiniMax Code cursor state at %s is not a JSON object", path)
        return {}
    return data


def _save_state(path: Path, state: dict[str, Any]) -> bool:
    """Write the cursor state file atomically; False when it could not be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        logger.warning("Failed to save MiniMax Code cursor state to %s: %s", path, exc)
        return False


def _skipped_map(entry: Any) -> dict[int, int]:
    """Read the persisted skip bookkeeping for one store.

    The map is ``row id -> attempts``. A row that cannot be turned into a
    valid push stays in it until it is emitted or abandoned, and the cursor
    stops in front of it.
    """
    if not isinstance(entry, dict):
        return {}
    raw = entry.get("skipped")
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for key, attempts in raw.items():
        try:
            out[int(key)] = int(attempts)
        except (TypeError, ValueError):
            continue
    return out


def _abandoned_list(entry: Any) -> list[int]:
    """Read the persisted list of abandoned row ids for one store.

    An abandoned row is one that could not be turned into a usage event
    within the attempt bound. It is kept so the probe can tell the operator
    that this usage was never reported.
    """
    if not isinstance(entry, dict):
        return []
    raw = entry.get("abandoned")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


# state-file path -> {database path: last emitted id}. The cursor only moves
# once the server confirms the ingest, so the advance is parked here until
# :func:`commit_pending_state` runs.
_PENDING_ADVANCES: dict[str, dict[str, int]] = {}

# state-file path -> {database path: {"skipped": {row id: attempts},
# "abandoned": [row id]}}. The rows the cursor may not step over, and the rows
# it gave up on. Parked alongside the advance, so a failed push neither
# advances the cursor nor counts an attempt.
_PENDING_SKIPS: dict[str, dict[str, dict[str, Any]]] = {}


def parked_skip_counts(state_file: Path, db_path: Path) -> tuple[int, int] | None:
    """Return the (skipped, abandoned) counts the last extraction parked.

    The extractor parks its skip bookkeeping next to the cursor advance, and
    the advance is only written when the server confirms the ingest. A reader
    that runs the extractor without committing — the probe does exactly that
    — therefore cannot read the numbers from the cursor file: they only
    exist in memory. This is where such a run's own numbers come from.

    Returns None when the last extraction parked nothing for this store —
    no rows past the cursor, or the store could not be read at all. That is
    not "nothing is skipped": it means the run has no opinion, and the
    cursor file stays the only record.
    """
    try:
        state_key = str(Path(state_file).resolve())
        db_key = str(Path(db_path).resolve())
    except OSError:
        return None
    parked = _PENDING_SKIPS.get(state_key, {}).get(db_key)
    if not isinstance(parked, dict):
        return None
    skipped = parked.get("skipped")
    abandoned = parked.get("abandoned")
    return (
        len(skipped) if isinstance(skipped, dict) else 0,
        len(abandoned) if isinstance(abandoned, list) else 0,
    )


def commit_pending_state() -> bool:
    """Persist the cursor advances left behind by the last extraction.

    Called by the sidecar once the server has confirmed the ingest. Returns
    False when there was nothing to commit or the state file could not be
    written. A write failure leaves the cursor where it was, so the rows
    are extracted again on the next cycle — the server's
    ``(provider_id, event_id)`` uniqueness makes that re-push a no-op.
    """
    if not _PENDING_ADVANCES:
        return False
    committed = True
    for state_key, advances in list(_PENDING_ADVANCES.items()):
        state = _load_state(Path(state_key))
        databases = state.get("databases")
        if not isinstance(databases, dict):
            databases = {}
        skips = _PENDING_SKIPS.get(state_key, {})
        for db_key, last_id in advances.items():
            entry: dict[str, Any] = {"last_id": last_id}
            parked = skips.get(db_key)
            if parked:
                entry["skipped"] = parked.get("skipped") or {}
                entry["abandoned"] = parked.get("abandoned") or []
            else:
                # A store that was scanned without a single skip keeps the
                # bookkeeping it already had: the extractor's effective map
                # is pruned, not emptied, so a blocked row that was not
                # re-read this cycle is not forgotten.
                previous = databases.get(db_key)
                if isinstance(previous, dict):
                    entry["skipped"] = previous.get("skipped") or {}
                    entry["abandoned"] = previous.get("abandoned") or []
            # Overwrite, never raise: a rebuilt store restarts the id axis,
            # so the new cursor can be *lower* than the old one. Keeping
            # the maximum would leave the cursor past the end of the new
            # table and re-emit the whole store on every cycle.
            databases[db_key] = entry
        state["databases"] = databases
        if _save_state(Path(state_key), state):
            _PENDING_ADVANCES.pop(state_key, None)
            _PENDING_SKIPS.pop(state_key, None)
        else:
            committed = False
    return committed


def discard_pending_state() -> None:
    """Drop the parked cursor advances (the ingest failed).

    The rows stay un-advanced, so the next cycle re-extracts and re-pushes
    them. The skip bookkeeping goes with them: a failed push is not an
    attempt at the row that failed to become an event.
    """
    _PENDING_ADVANCES.clear()
    _PENDING_SKIPS.clear()


def parse_minimax_code_events(
    db_path: Path,
    account_id: str,
    since: datetime,
    state_file: Path | None = None,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from a MiniMax Code runtime store.

    One event per API request, with the request's own per-request token
    values. ``cost_usd`` is None: the runtime writes 0.0 because the M Plan
    is a subscription, so the server prices the event from
    ``provider_pricing`` instead.

    Args:
        db_path: Path to the runtime's ``runtime-state.sqlite``.
        account_id: Account the events are stamped with.
        since: Bootstrap boundary, used only on a first run (no cursor yet)
            to decide which rows are worth importing.
        state_file: Optional path to the JSON file holding the id cursor.
    """
    if not db_path.exists():
        return []

    state_path = state_file or _default_state_path()
    try:
        state_key = str(state_path.resolve())
    except OSError:
        state_key = str(state_path)
    state = _load_state(state_path)
    databases = state.get("databases")
    if not isinstance(databases, dict):
        databases = {}
    db_key = str(db_path.resolve())
    entry = databases.get(db_key)
    has_cursor = isinstance(entry, dict)
    persisted_last_id = int((entry or {}).get("last_id", 0) or 0)
    # The rows the cursor may not step over, and the rows it gave up on.
    # Both live in the cursor file next to the id they belong to.
    skipped = _skipped_map(entry)
    abandoned = _abandoned_list(entry)

    # The scan floor is the *persisted* cursor and nothing else. A pending
    # advance must never act as a floor: it is parked before the push, so
    # if the cycle dies between the extraction and the push (or the push
    # fails), the advance is dropped and the rows must come back on the
    # next cycle. Treating it as a floor would skip them forever.
    last_id = persisted_last_id

    try:
        # Read-only: the runtime keeps this store open (WAL) from a
        # long-lived process and we only ever select from it.
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception as exc:
        logger.warning("Failed to open MiniMax Code runtime store %s: %s", db_path, exc)
        return []

    events: list[UsageEventPush] = []
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if not _schema_ok(cur):
            logger.debug(
                "MiniMax Code runtime store %s does not have the expected schema; skipping",
                db_path,
            )
            return []

        max_id = _max_row_id(cur)
        rebuilt = has_cursor and last_id > max_id
        if rebuilt:
            # The runtime rebuilt its store: the id axis restarted, so the
            # old cursor points past the end of the new table. Reset and
            # re-emit. The rebuilt store holds the requests made after the
            # rebuild — new usage that has not been reported yet — and a
            # request that reappears with the same timestamp keeps its
            # event id, so the server deduplicates it instead of counting
            # it twice.
            logger.info(
                "MiniMax Code runtime store %s was rebuilt (cursor %d > max id %d); re-emitting",
                db_path,
                last_id,
                max_id,
            )
            last_id = 0
            # The skip bookkeeping belongs to the old store's rows. On the
            # new store the same ids mean different requests, so it is
            # dropped with the cursor.
            skipped = {}
            abandoned = []
        elif not has_cursor:
            # No cursor at all: the bootstrap window decides where to start.
            # The window is the *only* floor that is safe here — the shared
            # timestamp watermark is not about this store, and the runtime's
            # `ts` is not globally monotonic, so a timestamp floor would drop
            # the rows that fall outside it.
            logger.info(
                "MiniMax Code runtime store %s is being read for the first time; "
                "importing the rows newer than %s",
                db_path,
                since.isoformat(),
            )
            last_id = _bootstrap_last_id(cur, since) - 1

        usage_columns = [
            "id",
            "session_id",
            "turn_id",
            "ts",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ]
        present_usage = _table_columns(cur, _TOKEN_USAGE_TABLE)
        has_row_agent = "agent_name" in present_usage
        for name in _OPTIONAL_USAGE_COLUMNS:
            if name in present_usage:
                usage_columns.append(name)
        try:
            rows = list(
                iter_cursor(
                    cur.execute(
                        f"SELECT {', '.join(usage_columns)} "
                        f"FROM {_TOKEN_USAGE_TABLE} WHERE id > ? ORDER BY id LIMIT ?",
                        (last_id, _MAX_ROWS_PER_CYCLE),
                    )
                )
            )
        except CursorReadError as exc:
            logger.warning("Failed to read MiniMax Code runtime store %s: %s", db_path, exc)
            return []

        if not rows:
            _PENDING_ADVANCES.setdefault(state_key, {})[db_key] = last_id
            return []

        session_ids = sorted({row["session_id"] for row in rows if row["session_id"]})
        turn_pairs = {(row["session_id"], row["turn_id"]) for row in rows}
        sessions = _load_sessions(cur, session_ids)
        # A background task's parent is not necessarily part of this cycle's
        # window (the parent's own rows may all have been collected already),
        # so load it too — otherwise the rows would have nowhere to go. The
        # loop follows a chain of parents (a task spawned by a task); each
        # round either loads the missing ones or stops.
        while True:
            parent_ids = {
                entry["parent_session_id"]
                for entry in sessions.values()
                if entry.get("parent_session_id")
            }
            missing_parents = sorted(parent_ids - set(sessions))
            if not missing_parents:
                break
            loaded = _load_sessions(cur, missing_parents)
            if not loaded:
                # The parent is not in this store. It is never invented, so
                # the rows keep their own session id and are only tagged.
                break
            sessions.update(loaded)
        attributions = {
            sid: attribution
            for sid, entry in sessions.items()
            if (attribution := _child_attribution(entry, sessions)) is not None
        }
        message_index = _load_message_index(cur, turn_pairs)

        model_cache: dict[str, str | None] = {}
        seen_event_ids: set[str] = set()
        covered_id = last_id
        # The lowest row id the cursor may not step over. A skipped row is
        # retried on the next cycle, so the cursor stops in front of it.
        blocked_id: int | None = None

        def _skip(row_id: int, reason: str) -> None:
            """Record a row the extractor could not turn into an event.

            The cursor must not step over the row: stepping over it would
            lose its usage for good. It is retried on every cycle until
            ``_MAX_SKIP_ATTEMPTS`` attempts are spent, and only then is it
            abandoned — and then it is reported, never dropped quietly.
            """
            nonlocal blocked_id
            attempts = skipped.get(row_id, 0) + 1
            if attempts >= _MAX_SKIP_ATTEMPTS:
                skipped.pop(row_id, None)
                abandoned.append(row_id)
                logger.error(
                    "MiniMax Code request %s could not be turned into a usage event "
                    "after %d attempts (%s); abandoning it — this usage is not reported",
                    row_id,
                    attempts,
                    reason,
                )
                return
            skipped[row_id] = attempts
            if blocked_id is None or row_id < blocked_id:
                blocked_id = row_id
            logger.warning(
                "MiniMax Code request %s skipped (%s); the cursor stops before it and "
                "the row is retried on the next cycle",
                row_id,
                reason,
            )

        for row in rows:
            row_id = int(row["id"])
            covered_id = row_id
            session_id = row["session_id"] or ""
            turn_id = row["turn_id"] or ""
            try:
                ts_ms = int(row["ts"])
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                _skip(row_id, f"unusable timestamp {row['ts']!r}")
                continue

            key = (
                session_id,
                row["turn_id"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_read_tokens"],
            )
            candidates = message_index.get(key) or []
            data = candidates[0][1] if candidates else {}

            session = sessions.get(session_id) or {}
            model_id = _resolve_model(session_id, data, session, db_path, model_cache)
            tool_names = _tool_names(data)

            # Where the row is filed and how it is labelled.
            #
            # A background task's rows belong to the session that spawned it,
            # so they are filed under the parent and tagged as subagent work —
            # the same shape a Claude Code sidechain row has. The runtime's own
            # plan-review turns win the label: they are a different kind of
            # subagent work inside the same session.
            event_session_id = session_id or None
            subagent_type: str | None = None
            raw_json: str | None = None
            attribution = attributions.get(session_id)
            agent_name: str | None = None
            if attribution is not None:
                row_agent = row["agent_name"] if has_row_agent else None
                agent_name = session.get("agent_name") or row_agent
                parent_id = attribution["parent_session_id"]
                if parent_id:
                    # The work belongs to the session that spawned it, so the
                    # rows are filed under the parent — exactly like a Claude
                    # Code sidechain row.
                    event_session_id = parent_id
                else:
                    logger.debug(
                        "MiniMax Code background task %s names a parent that is not in "
                        "this store; keeping its own session id",
                        session_id,
                    )
                raw_json = background_provenance(
                    session_id, agent_name, parent_id, session.get("purpose")
                )
            if turn_id.startswith(INTERNAL_TURN_PREFIX):
                # The runtime's own plan-review sub-agent wins the label: it is
                # a different kind of subagent work inside the same session.
                subagent_type = SUBAGENT_TYPE_PLAN
            elif attribution is not None:
                subagent_type = _agent_label(agent_name)

            event_id = build_event_id(
                session_id,
                turn_id,
                ts_ms,
                int(row["input_tokens"] or 0),
                int(row["output_tokens"] or 0),
                int(row["cache_read_tokens"] or 0),
            )
            if event_id in seen_event_ids:
                # Two rows of the same turn share a millisecond *and* their
                # token counts — a true duplicate log entry. The ledger's own
                # row id is the last tiebreaker. It is batch-local (the id is
                # only known once the row is in the batch), but reaching it
                # needs two rows with an identical timestamp and identical
                # usage, which the server would deduplicate anyway.
                event_id = f"{event_id}:{row_id}"
                if event_id in seen_event_ids:
                    _skip(row_id, f"collides with the earlier event id {event_id}")
                    continue
            seen_event_ids.add(event_id)

            try:
                events.append(
                    UsageEventPush(
                        provider_id=PROVIDER_ID,
                        source_app=MINIMAX_CODE,
                        account_id=account_id,
                        event_id=event_id,
                        ts=ts.isoformat(),
                        model_id=model_id,
                        session_id=event_session_id,
                        subagent_type=subagent_type,
                        turn_id=turn_id or None,
                        raw_json=raw_json,
                        cwd=session.get("workspace_dir") or None,
                        tool_names=tool_names or None,
                        tokens_input=int(row["input_tokens"] or 0),
                        tokens_output=int(row["output_tokens"] or 0),
                        tokens_cache_read=int(row["cache_read_tokens"] or 0),
                        tokens_cache_create=int(row["cache_write_tokens"] or 0),
                        tokens_reasoning=int(row["reasoning_tokens"] or 0),
                        stop_reason=_stop_reason(data),
                        tool_calls=len(tool_names),
                        effort=_effort(session),
                        # The M Plan is a subscription: the runtime's own
                        # cost is 0.0, so the server prices the event from
                        # provider_pricing.
                        cost_usd=None,
                        kind="message",
                    )
                )
            except ValidationError as exc:
                _skip(row_id, f"the usage event is not valid: {exc}")
                continue

        if blocked_id is not None:
            # The cursor stops in front of the skipped row: the next cycle
            # reads it again. The rows after it are re-read too, and the
            # server deduplicates them — a re-push is a no-op, not a double
            # count.
            covered_id = min(covered_id, blocked_id - 1)
        if skipped:
            logger.warning(
                "MiniMax Code runtime store %s: %d request(s) skipped and will be retried; "
                "the cursor stops before id %s",
                db_path,
                len(skipped),
                blocked_id,
            )
        if abandoned:
            logger.error(
                "MiniMax Code runtime store %s: %d request(s) abandoned — their usage is "
                "not reported",
                db_path,
                len(abandoned),
            )

        # The skip bookkeeping only keeps the rows the cursor has not passed
        # yet: a row behind the cursor was either emitted or given up on.
        parked_skips = {
            row_id: attempts for row_id, attempts in skipped.items() if row_id > covered_id
        }
        parked_abandoned = abandoned[-_MAX_ABANDONED_RECORDS:]
        _PENDING_ADVANCES.setdefault(state_key, {})[db_key] = covered_id
        _PENDING_SKIPS.setdefault(state_key, {})[db_key] = {
            "skipped": parked_skips,
            "abandoned": parked_abandoned,
        }
        return events
    except (sqlite3.Error, CursorReadError) as exc:
        # A locked, corrupt or drifted store is a skipped source, not a
        # crash: the sidecar logs it and carries on.
        logger.warning("Failed to read MiniMax Code runtime store %s: %s", db_path, exc)
        return []
    finally:
        conn.close()


def parse_minimax_code_session_names(db_path: Path) -> list[dict[str, Any]] | None:
    """Session titles from one runtime store, as ``generated`` name records.

    ``local_runtime_sessions.title`` only: ``agent_name`` is the agent's label and
    the FTS index holds search terms, neither is a session name. A background-task
    session whose parent is in the store is skipped: its events are filed under
    the parent (see :func:`parse_minimax_code_events`), which carries the title
    the user sees. Read-only, like the event extraction.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception:
        return None
    try:
        cur = conn.cursor()
        columns = _table_columns(cur, _SESSIONS_TABLE)
        if not {"session_id", "title"} <= columns:
            return []
        parent = "parent_session_id" if "parent_session_id" in columns else "NULL"
        rows = cur.execute(
            f"SELECT session_id, title, {parent} FROM {_SESSIONS_TABLE}"  # noqa: S608
        ).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    known = {row[0] for row in rows if isinstance(row[0], str)}
    records: list[dict[str, Any]] = []
    for session_id, title, parent_id in rows:
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        if parent_id and parent_id in known:
            continue
        records.append(
            make_record(PROVIDER_ID, session_id, title.strip(), "generated", MINIMAX_CODE)
        )
    return records
