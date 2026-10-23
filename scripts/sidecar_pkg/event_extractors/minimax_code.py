"""Parse the MiniMax Code runtime store into UsageEventPush records.

The MiniMax Code runtime (the ``mavis`` agent shipped with the MiniMax Code
CLI and desktop app) keeps its own usage ledger in a SQLite store under its
app data dir::

    ~/.minimax/v2/sqlite/runtime-state.sqlite   (+ -wal, -shm)

The runtime anchors that dir on ``homedir()`` — verified in the installed
bundle (``join(homeDir ?? homedir(), ".minimax")``) — so the path is the
same on Linux, macOS and Windows. The desktop app's app-data roots are probed
as well (they are not covered by a live verification, see ``_data_roots``).

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

``event_id`` is derived from the ledger row alone
(``mcode:<session>:<turn>:<ts>``). The runtime's own
``canonical_message_id`` would be prettier, but a request's message row can
land in the store *after* the request itself: the newest rows are unmatched
for a few seconds. An id that depends on the join would then change between
two attempts at the same row and the server would count the request twice.
The ledger-derived id is stable for a given request — across re-runs, across
a failed push and across a re-read of the same row — which is what the
server's ``(provider_id, event_id)`` uniqueness needs for idempotent
re-emission. Two requests of the same turn that share a millisecond are
separated by the ledger's own (never-changing) row id.
"""

from __future__ import annotations

import json
import logging
import os
import platform
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
from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor  # noqa: E402

logger = logging.getLogger("runway.sidecar.minimax_code")

# The provider the events are filed under. The MiniMax Code runtime bills
# the same upstream account as the MiniMax quota gauge, so the events land
# on the same card instead of splitting it.
PROVIDER_ID = "minimax"

# The runtime's internal plan-review sub-agent calls. They are real API
# requests against the user's plan quota, so they are included and tagged as
# a "Plan" subagent — the Sessions view already splits subagent turns out
# of the main-thread message count.
INTERNAL_TURN_PREFIX = "mavis-internal:plan-review:"
SUBAGENT_TYPE_PLAN = "Plan"

_TOKEN_USAGE_TABLE = "local_runtime_token_usage"
_MESSAGE_ROWS_TABLE = "local_runtime_message_rows"
_SESSIONS_TABLE = "local_runtime_sessions"

# The columns the extractor needs. The runtime owns its schema (≈34
# migrations and counting), so a rename must degrade to "skipped source"
# rather than raise into the sidecar loop.
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    _TOKEN_USAGE_TABLE: (
        "id",
        "session_id",
        "turn_id",
        "ts",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
    _MESSAGE_ROWS_TABLE: ("id", "session_id", "turn_id", "data_json"),
    _SESSIONS_TABLE: (
        "session_id",
        "workspace_dir",
        "extra_data_json",
        "history_relative_dir",
    ),
}

# Rows scanned per cycle. A first run over a long-lived store can see tens
# of thousands of requests; the cursor advances to the last row this cycle
# covered, so the rest is picked up by the next cycle instead of producing
# one enormous push.
_MAX_ROWS_PER_CYCLE = 5000

# The provider prefix the runtime writes into ``effectiveModel``. The
# pricing seed keys on the bare model id, so the prefix is stripped.
_PROVIDER_MODEL_PREFIX = "minimax/"


def _data_roots() -> list[Path]:
    """Return the candidate MiniMax Code data roots, most likely first.

    The runtime resolves its data dir from ``join(homeDir ?? homedir(),
    ".minimax")``, so ``~/.minimax`` is the root on every platform. The
    desktop app's macOS / Windows app-data roots are probed as insurance:
    they are not covered by a live verification, but a miss costs nothing
    while a hit keeps the extractor working.
    """
    roots: list[Path] = []
    override = os.getenv("MINIMAX_CODE_HOME")
    if override:
        roots.append(Path(override).expanduser())
    home = Path.home()
    roots.extend(
        [
            home / ".minimax",
            home / "Library" / "Application Support" / "MiniMax Code",
            home / "Library" / "Application Support" / "minimax",
            home / "Library" / "Application Support" / "MiniMax",
            home / "AppData" / "Roaming" / "MiniMax Code",
            home / "AppData" / "Roaming" / "minimax",
        ]
    )
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def discover_runtime_store() -> Path | None:
    """Return the MiniMax Code runtime SQLite store, or None when absent.

    ``None`` is the normal answer on a host where MiniMax Code never ran —
    the extractor then contributes nothing instead of raising.
    """
    for root in _data_roots():
        candidate = root / "v2" / "sqlite" / "runtime-state.sqlite"
        if candidate.is_file():
            return candidate
    return None


def _default_state_path() -> Path:
    """Return the provider-local cursor state file path.

    Mirrors the sidecar's config dir (``~/.config/runway/sidecar`` on
    Linux/macOS, ``%APPDATA%\\runway\\sidecar`` on Windows) so the cursor
    lands next to the rest of the sidecar's state.
    """
    config_dir = os.getenv("RUNWAY_CONFIG_DIR")
    if config_dir:
        base = Path(config_dir) / "sidecar"
    elif platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        base = (
            Path(app_data) / "runway" / "sidecar"
            if app_data
            else Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
        )
    else:
        base = Path.home() / ".config" / "runway" / "sidecar"
    return base / "minimax_code_watermark.json"


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


# state-file path -> {database path: last emitted id}. The cursor only moves
# once the server confirms the ingest, so the advance is parked here until
# :func:`commit_pending_state` runs.
_PENDING_ADVANCES: dict[str, dict[str, int]] = {}


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
        for db_key, last_id in advances.items():
            current = int((databases.get(db_key) or {}).get("last_id", 0) or 0)
            if last_id > current:
                databases[db_key] = {"last_id": last_id}
        state["databases"] = databases
        if _save_state(Path(state_key), state):
            _PENDING_ADVANCES.pop(state_key, None)
        else:
            committed = False
    return committed


def discard_pending_state() -> None:
    """Drop the parked cursor advances (the ingest failed).

    The rows stay un-advanced, so the next cycle re-extracts and re-pushes
    them.
    """
    _PENDING_ADVANCES.clear()


def _chunks[T](items: list[T], size: int) -> list[list[T]]:
    """Split a list into chunks of at most ``size`` entries."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _schema_ok(cur: sqlite3.Cursor) -> bool:
    """True when every required table and column is present."""
    for table, columns in _REQUIRED_COLUMNS.items():
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            return False
        present = {info[1] for info in cur.execute(f"PRAGMA table_info({table})")}
        if not set(columns).issubset(present):
            return False
    return True


def _max_row_id(cur: sqlite3.Cursor) -> int:
    """Return the highest ledger id in the store (0 when it is empty)."""
    row = cur.execute(f"SELECT MAX(id) FROM {_TOKEN_USAGE_TABLE}").fetchone()
    return int(row[0] or 0) if row else 0


def _bootstrap_last_id(cur: sqlite3.Cursor, since: datetime) -> int:
    """Return the id to start from on a first run.

    The shared timestamp watermark's bootstrap window decides *which* rows
    are interesting, but it is applied on the **id** axis — the lowest id
    whose ``ts`` falls inside the window. Filtering the scan itself by
    ``ts`` would drop the interleaved rows that carry an older timestamp
    (see the module docstring), so the window is turned into an id
    boundary once and then forgotten.
    """
    since_ms = int(since.timestamp() * 1000)
    row = cur.execute(
        f"SELECT MIN(id) FROM {_TOKEN_USAGE_TABLE} WHERE ts > ?", (since_ms,)
    ).fetchone()
    if row is None or row[0] is None:
        # Nothing inside the window — skip to the end of the table so the
        # next cycle does not rescan it.
        return _max_row_id(cur) + 1
    return int(row[0])


def _load_sessions(cur: sqlite3.Cursor, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return the session metadata (cwd, effective model, history dir)."""
    sessions: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(session_ids, 200):
        placeholders = ",".join("?" for _ in chunk)
        query = (
            f"SELECT session_id, workspace_dir, extra_data_json, history_relative_dir "
            f"FROM {_SESSIONS_TABLE} WHERE session_id IN ({placeholders})"
        )
        for row in iter_cursor(cur.execute(query, chunk)):
            extra: dict[str, Any] = {}
            try:
                parsed = json.loads(row["extra_data_json"] or "{}")
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                extra = parsed
            sessions[row["session_id"]] = {
                "workspace_dir": row["workspace_dir"],
                "history_relative_dir": row["history_relative_dir"],
                "extra": extra,
            }
    return sessions


def _index_message_row(
    index: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]],
    row: sqlite3.Row,
) -> None:
    """Add one message row to the usage-tuple index."""
    try:
        data = json.loads(row["data_json"] or "{}")
    except (TypeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return
    key = (
        row["session_id"],
        row["turn_id"],
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_read"),
    )
    index.setdefault(key, []).append((int(row["id"]), data))


def _load_message_index(
    cur: sqlite3.Cursor,
    turn_pairs: set[tuple[str | None, str | None]],
) -> dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]]:
    """Index the message rows of the pending window by their usage tuple.

    The join key is ``(session_id, turn_id, input, output, cache_read)``.
    It matches the ledger's per-request values against the message's own
    ``usage`` block — the two agree value for value, which is what makes
    the message row the right twin of a request.

    Only the turns of the pending window are read: the store keeps every
    message of every session, and parsing all of them would cost more than
    the rest of the extraction put together.
    """
    index: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]] = {}
    pairs = sorted((session, turn) for session, turn in turn_pairs if session and turn)
    # A NULL turn never matches a row-value comparison, so those sessions
    # are read with their own predicate.
    null_turn_sessions = sorted({session for session, turn in turn_pairs if session and not turn})

    for chunk in _chunks(pairs, 200):
        placeholders = ",".join("(?,?)" for _ in chunk)
        params = [value for pair in chunk for value in pair]
        query = (
            f"SELECT id, session_id, turn_id, data_json FROM {_MESSAGE_ROWS_TABLE} "
            f"WHERE (session_id, turn_id) IN (VALUES {placeholders})"
        )
        for row in iter_cursor(cur.execute(query, params)):
            _index_message_row(index, row)

    if null_turn_sessions:
        placeholders = ",".join("?" for _ in null_turn_sessions)
        query = (
            f"SELECT id, session_id, turn_id, data_json FROM {_MESSAGE_ROWS_TABLE} "
            f"WHERE turn_id IS NULL AND session_id IN ({placeholders})"
        )
        for row in iter_cursor(cur.execute(query, null_turn_sessions)):
            _index_message_row(index, row)

    # Lowest id first: a stable choice when two message rows share a usage
    # tuple, so the enrichment never flips between two runs.
    for candidates in index.values():
        candidates.sort(key=lambda item: item[0])
    return index


def _normalize_model(model: str) -> str | None:
    """Strip the provider prefix so the id matches the pricing seed key."""
    name = (model or "").strip()
    if not name:
        return None
    if name.lower().startswith(_PROVIDER_MODEL_PREFIX):
        name = name[len(_PROVIDER_MODEL_PREFIX) :]
    return name or None


def _sessions_root(db_path: Path) -> Path | None:
    """Return the runtime's session-history root for a given store path."""
    try:
        root = db_path.resolve().parents[2]
    except IndexError:
        return None
    return root / "v2" / "sessions"


def _model_from_history(
    session_id: str,
    session: dict[str, Any],
    db_path: Path,
    cache: dict[str, str | None],
) -> str | None:
    """Return the model named by the session's ``messages.jsonl``.

    Only a fallback: the history JSONL is pruned, so it is not a reliable
    source of *usage* — but it still names the model it talked to.
    """
    cache_key = f"history:{session_id}"
    if cache_key in cache:
        return cache[cache_key]
    model: str | None = None
    relative = session.get("history_relative_dir")
    root = _sessions_root(db_path)
    if relative and root:
        path = root / relative / "messages.jsonl"
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    message = entry.get("message") if isinstance(entry, dict) else None
                    if not isinstance(message, dict):
                        continue
                    candidate = message.get("model")
                    if isinstance(candidate, str) and candidate.strip():
                        model = candidate.strip()
                        break
        except OSError:
            model = None
    cache[cache_key] = model
    return model


def _model_from_llm_call(
    session_id: str,
    session: dict[str, Any],
    db_path: Path,
    cache: dict[str, str | None],
) -> str | None:
    """Return the model named by the session's ``llm-call.json``."""
    cache_key = f"llm-call:{session_id}"
    if cache_key in cache:
        return cache[cache_key]
    model: str | None = None
    relative = session.get("history_relative_dir")
    root = _sessions_root(db_path)
    if relative and root:
        path = root / relative / "llm-call.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate.strip():
                model = candidate.strip()
    cache[cache_key] = model
    return model


def _resolve_model(
    session_id: str,
    data: dict[str, Any],
    session: dict[str, Any],
    db_path: Path,
    cache: dict[str, str | None],
) -> str | None:
    """Resolve the model of one request.

    First the message row's own telemetry, then the session's history
    JSONL, then the session's ``effectiveModel``, then ``llm-call.json``.
    The session-level fallbacks are always present for a live session, so
    a request whose message row has not landed yet still gets a model.
    """
    telemetry = data.get("context_usage_telemetry")
    if isinstance(telemetry, dict):
        candidate = telemetry.get("model")
        if isinstance(candidate, str) and candidate.strip():
            return _normalize_model(candidate)

    candidate = _model_from_history(session_id, session, db_path, cache)
    if candidate:
        return _normalize_model(candidate)

    extra = session.get("extra") or {}
    effective = extra.get("effectiveModel") if isinstance(extra, dict) else None
    if isinstance(effective, str) and effective.strip():
        return _normalize_model(effective)

    candidate = _model_from_llm_call(session_id, session, db_path, cache)
    if candidate:
        return _normalize_model(candidate)

    return None


def _tool_names(data: dict[str, Any]) -> list[str]:
    """Return the tool names the request's message row recorded."""
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("tool_name") or call.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _stop_reason(data: dict[str, Any]) -> str | None:
    """Return the message row's finish reason."""
    reason = data.get("finish_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def _effort(session: dict[str, Any]) -> str | None:
    """Return the session's configured thinking effort."""
    extra = session.get("extra") or {}
    thinking = extra.get("effectiveModelThinking") if isinstance(extra, dict) else None
    if isinstance(thinking, dict):
        effort = thinking.get("effort")
        if isinstance(effort, str) and effort.strip():
            return effort.strip().lower()
    return None


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
        elif not has_cursor:
            # No cursor at all: the bootstrap window decides where to start.
            last_id = _bootstrap_last_id(cur, since) - 1

        try:
            rows = list(
                iter_cursor(
                    cur.execute(
                        f"SELECT id, session_id, turn_id, ts, input_tokens, output_tokens, "
                        f"reasoning_tokens, cache_read_tokens, cache_write_tokens "
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
        message_index = _load_message_index(cur, turn_pairs)

        model_cache: dict[str, str | None] = {}
        seen_event_ids: set[str] = set()
        covered_id = last_id

        for row in rows:
            row_id = int(row["id"])
            covered_id = row_id
            session_id = row["session_id"] or ""
            turn_id = row["turn_id"] or ""
            try:
                ts_ms = int(row["ts"])
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                logger.warning(
                    "MiniMax Code request %s has an unusable timestamp %r; skipping",
                    row_id,
                    row["ts"],
                )
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

            event_id = f"mcode:{session_id}:{turn_id}:{ts_ms}"
            if event_id in seen_event_ids:
                # Two requests of the same turn share a millisecond. Keep
                # both: the ledger's own row id is a stable tiebreaker (an
                # AUTOINCREMENT id never changes for a given row).
                event_id = f"{event_id}:{row_id}"
                if event_id in seen_event_ids:
                    logger.warning(
                        "MiniMax Code request %s collides with an earlier event id %s; skipping",
                        row_id,
                        event_id,
                    )
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
                        session_id=session_id or None,
                        subagent_type=(
                            SUBAGENT_TYPE_PLAN if turn_id.startswith(INTERNAL_TURN_PREFIX) else None
                        ),
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
                logger.warning("Skipping MiniMax Code request %s: %s", row_id, exc)
                continue

        _PENDING_ADVANCES.setdefault(state_key, {})[db_key] = covered_id
        return events
    except (sqlite3.Error, CursorReadError) as exc:
        # A locked, corrupt or drifted store is a skipped source, not a
        # crash: the sidecar logs it and carries on.
        logger.warning("Failed to read MiniMax Code runtime store %s: %s", db_path, exc)
        return []
    finally:
        conn.close()
