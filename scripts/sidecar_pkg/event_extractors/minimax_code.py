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

# The runtime's background-task sessions. It spawns them as *child* sessions:
# their own session id, a ``parent_session_id`` pointing at the session that
# spawned them, an ``agent_name`` naming the agent and a ``purpose`` naming
# the background task.
#
# Claude Code models the same work as a sidechain: the message carries the
# PARENT's session id plus an attribution agent (see the sidechain fixture in
# tests/fixtures/anthropic-sidechain-sample.jsonl). The rows here are filed
# under the parent session and tagged as subagent work, so the Sessions view
# splits them out of the parent's main thread exactly like a sidechain.
SUBAGENT_TYPE_TASK = "Task"

# The session columns the extractor reads when the store has them. They are
# optional on purpose: the runtime added them in a later migration than the
# columns in ``_REQUIRED_COLUMNS``, and a store without them must degrade to
# "no attribution" — never to "skipped source", which would drop every row.
_OPTIONAL_SESSION_COLUMNS: tuple[str, ...] = (
    "agent_name",
    "parent_session_id",
    "session_kind",
    "purpose",
)

# The ledger's own per-row agent name. It agrees with the session row's, and
# is the fallback when the session row has none. Optional for the same reason
# as the session columns above.
_OPTIONAL_USAGE_COLUMNS: tuple[str, ...] = ("agent_name",)

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

# How many cycles a row may be skipped before the extractor gives up on it.
#
# A skipped row (an unusable timestamp, a colliding event id, a payload the
# schema rejects) blocks the cursor: the cursor must not step over it, or the
# row's usage is lost for good. The row is retried on every cycle until this
# bound is reached; after that it is reported and abandoned, because a row
# that cannot be turned into a valid push will never become one.
_MAX_SKIP_ATTEMPTS = 3

# How many abandoned row ids the cursor file keeps per store. The list is a
# report for the operator — the rows are already gone from the ledger's
# perspective — so it is capped rather than allowed to grow forever.
_MAX_ABANDONED_RECORDS = 100

# The provider prefix the runtime writes into ``effectiveModel``. The
# pricing seed keys on the bare model id, so the prefix is stripped.
_PROVIDER_MODEL_PREFIX = "minimax/"


# The environment variables the runtime itself honours for its data dir.
#
# ``join(homeDir ?? homedir(), ".minimax")`` is only the *last* step of the
# runtime's resolution: every layer above it can move the dir. ``MINIMAX_DATA_DIR``
# and ``MAVIS_DATA_DIR`` are the public knobs; the ``__MAVIS_*`` ones are
# internal and are injected by the daemon supervisor into the processes it
# spawns. The runtime's own Python helper reads ``__MAVIS_PARENT_DATA_DIR`` and
# ``MINIMAX_DATA_DIR``; its JS side reads ``MINIMAX_DATA_DIR`` and
# ``MAVIS_DATA_DIR``.
_DATA_DIR_ENV_VARS = (
    "MINIMAX_DATA_DIR",
    "MAVIS_DATA_DIR",
    "__MAVIS_RUNTIME_DATA_DIR",
    "__MAVIS_PARENT_DATA_DIR",
)

# The data-dir basenames: the current one and the legacy one it migrated
# away from. A named profile gets ``<basename>-<profile>``.
_DATA_DIR_BASENAMES = (".minimax", ".mavis")

# The desktop app's app-data dir names.
#
# The Electron profile dir is named after the app's *product* name — on
# Windows and macOS alike it is "MiniMax Agent" — while the installer and
# the CLI ship under "MiniMax Code"; both are probed, so a rename on
# either platform cannot hide the store.
_APP_DATA_DIR_NAMES = ("MiniMax Agent", "MiniMax Code", "minimax")


def _env_data_roots() -> list[Path]:
    """Return the data dirs the runtime's environment points at."""
    roots: list[Path] = []
    for name in _DATA_DIR_ENV_VARS:
        raw = os.getenv(name)
        if raw and raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    return roots


def _profile_scoped_roots(home: Path) -> list[Path]:
    """Return the profile-scoped data dirs under the user's home.

    The runtime scopes its data dir per profile: ``basenameForProfile`` turns
    ``.minimax`` into ``.minimax-<profile>``. A profile comes from the desktop
    app, the CLI's ``--profile`` flag or the supervisor — and the runtime also
    derives one from the current branch when it runs inside its own
    repository, which gives each branch its own ledger.

    Globbing catches every profile without having to know how it was named.
    """
    roots: list[Path] = []
    for basename in _DATA_DIR_BASENAMES:
        try:
            matches = sorted(home.glob(f"{basename}-*"))
        except OSError:
            # An unreadable home directory simply yields no profile roots.
            continue
        roots.extend(matches)
    return roots


def _macos_app_data_roots(home: Path) -> list[Path]:
    """Return the desktop app's macOS app-data roots.

    The Electron profile lives in ``~/Library/Application Support/<name>``.
    It holds the Chromium profile and never the ledger, but it is probed as
    insurance.
    """
    base = home / "Library" / "Application Support"
    return [base / name for name in (*_APP_DATA_DIR_NAMES, "MiniMax")]


def _windows_app_data_root() -> Path:
    """Return the Windows roaming app-data root (``%APPDATA%``).

    Mirrors ``sidecar.get_sidecar_dir()``: on Windows it reads ``%APPDATA%``,
    so a host that redirects it — a roaming profile, a relocated ``AppData``
    — is probed where the app actually writes. The variable is a Windows one,
    so it is ignored elsewhere: an ``APPDATA`` exported on macOS or Linux
    cannot add a root the sidecar's own state dir would never use. Falls back
    to the conventional ``%USERPROFILE%\\AppData\\Roaming`` when it is unset.
    """
    if platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data and app_data.strip():
            return Path(app_data.strip())
    return Path.home() / "AppData" / "Roaming"


def _windows_app_data_roots() -> list[Path]:
    """Return the desktop app's Windows app-data roots."""
    base = _windows_app_data_root()
    return [base / name for name in _APP_DATA_DIR_NAMES]


def _data_roots() -> list[Path]:
    """Return the candidate MiniMax Code data roots, most likely first.

    The runtime resolves its data dir from ``join(homeDir ?? homedir(),
    ".minimax")``, so ``~/.minimax`` is the root on every platform — but that
    is only the *default*. The runtime also scopes the dir per profile
    (``~/.minimax-<profile>``), honours environment overrides, and used to
    live in ``~/.mavis`` before it migrated. The desktop app's macOS /
    Windows app-data roots are probed as insurance: the app's Electron
    profile is ``MiniMax Agent`` on both platforms, but that dir holds the
    Chromium profile, never the ledger.

    The list is deduplicated by *resolved* path, so the legacy ``~/.mavis``
    symlink to ``~/.minimax`` does not show up twice.
    """
    roots: list[Path] = []
    override = os.getenv("MINIMAX_CODE_HOME")
    if override:
        roots.append(Path(override).expanduser())
    roots.extend(_env_data_roots())
    home = Path.home()
    roots.append(home / ".minimax")
    roots.extend(_profile_scoped_roots(home))
    roots.append(home / ".mavis")
    roots.extend(_macos_app_data_roots(home))
    roots.extend(_windows_app_data_roots())
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


def discover_runtime_stores() -> list[Path]:
    """Return every MiniMax Code runtime store on this host.

    More than one can exist: a profile-scoped install keeps its ledger in
    ``~/.minimax-<profile>`` next to the default one, and the runtime's
    environment can point at a third. Each store carries its own cursor (they
    are keyed by database path), so all of them are read — reporting only the
    first would silently drop the usage in the others.

    The list is deduplicated by *resolved* path: on a migrated install the
    legacy ``~/.mavis`` is a symlink to ``~/.minimax``, and both would
    otherwise report the same requests twice.

    An empty list is the normal answer on a host where MiniMax Code never ran
    — the extractor then contributes nothing instead of raising.
    """
    stores: list[Path] = []
    seen: set[str] = set()
    for root in _data_roots():
        candidate = root / "v2" / "sqlite" / "runtime-state.sqlite"
        if not candidate.is_file():
            continue
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        stores.append(candidate)
    return stores


def _default_state_path() -> Path:
    """Return the provider-local cursor state file path.

    Mirrors the sidecar's config dir (``~/.config/runway/sidecar`` on
    Linux/macOS, ``%APPDATA%\\runway\\sidecar`` on Windows) so the cursor
    lands next to the rest of the sidecar's state.

    This mirrors ``sidecar.get_sidecar_dir()`` instead of importing it:
    the frozen app bundles ``scripts/sidecar.py`` as *data* and runs it as
    a script, so importing it as a module here would give the frozen
    binary a second copy of the sidecar's module state. The sidecar
    passes its own ``get_sidecar_dir() / "minimax_code_watermark.json"``
    in; this is the fallback for a direct call, and a test pins the two
    together.
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


def _table_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    """Return the columns a table actually has in this store."""
    return {info[1] for info in cur.execute(f"PRAGMA table_info({table})")}


def _load_sessions(cur: sqlite3.Cursor, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return the session metadata (cwd, effective model, history dir, parent link).

    The parent-link columns are optional: a store written by a runtime that
    predates them still loads — it simply has no parent to file the rows
    under, which is today's behaviour rather than a skipped source.
    """
    if not session_ids:
        return {}
    present = _table_columns(cur, _SESSIONS_TABLE)
    optional = [name for name in _OPTIONAL_SESSION_COLUMNS if name in present]
    select = ", ".join(
        ["session_id", "workspace_dir", "extra_data_json", "history_relative_dir", *optional]
    )
    sessions: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(session_ids, 200):
        placeholders = ",".join("?" for _ in chunk)
        query = f"SELECT {select} FROM {_SESSIONS_TABLE} WHERE session_id IN ({placeholders})"
        for row in iter_cursor(cur.execute(query, chunk)):
            extra: dict[str, Any] = {}
            try:
                parsed = json.loads(row["extra_data_json"] or "{}")
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                extra = parsed
            entry: dict[str, Any] = {
                "workspace_dir": row["workspace_dir"],
                "history_relative_dir": row["history_relative_dir"],
                "extra": extra,
            }
            for name in optional:
                entry[name] = row[name]
            sessions[row["session_id"]] = entry
    return sessions


def _agent_label(agent_name: str | None) -> str:
    """Return the dashboard label of a runtime agent name.

    The runtime spells its agents in lower case (``explore``); Claude Code
    spells its built-in agent ``Explore``, and the Sessions view groups the
    subagent cards by this label.
    """
    name = (agent_name or "").strip()
    if not name:
        return SUBAGENT_TYPE_TASK
    return name[:1].upper() + name[1:]


def _child_attribution(
    session: dict[str, Any], sessions: dict[str, dict[str, Any]]
) -> dict[str, str | None] | None:
    """Return the attribution of a background-task session, or None for a main session.

    A child session is one the runtime spawned for a background task: it
    carries a ``parent_session_id``. Its rows are filed under that parent and
    tagged with the agent's name, so the Sessions view shows the work as the
    parent's subagent traffic — the same shape a Claude Code sidechain has.

    A parent that is not in this store is **not** invented: the rows keep
    their own session id and are only tagged, so the work stays labelled as
    subagent traffic instead of landing on a session that does not exist.
    """
    parent_id = session.get("parent_session_id")
    if not parent_id:
        return None
    return {"parent_session_id": parent_id if parent_id in sessions else None}


def _all_session_ids(cur: sqlite3.Cursor) -> list[str]:
    """Return every session id in the store."""
    rows = cur.execute(f"SELECT session_id FROM {_SESSIONS_TABLE}").fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def background_session_links(cur: sqlite3.Cursor) -> dict[str, dict[str, Any]]:
    """Return every background-task session of the store and its attribution.

    A background task is a session the runtime spawned: it carries a
    ``parent_session_id`` pointing at the session that owns the work. The
    rows of such a session are filed under that parent and tagged with the
    agent's name.

    The mapping is keyed by the *child* session id and holds the parent to
    file the rows under, the label to tag them with and the provenance to
    record with them. A parent that is not in the store is not invented: the
    task keeps its own session id and is only tagged.

    The repair script (``scripts/link_minimax_background_sessions.py``) uses
    this to build the mapping it applies to the rows that were collected
    before the extractor learned the parent link.
    """
    cur.row_factory = sqlite3.Row
    sessions = _load_sessions(cur, _all_session_ids(cur))
    children = {sid: entry for sid, entry in sessions.items() if entry.get("parent_session_id")}
    if not children:
        return {}

    # A session row without an agent name falls back to the ledger's own
    # per-row agent name — the same fallback the extractor applies.
    ledger_agents: dict[str, str] = {}
    if any(not entry.get("agent_name") for entry in children.values()):
        if "agent_name" in _table_columns(cur, _TOKEN_USAGE_TABLE):
            placeholders = ",".join("?" for _ in children)
            rows = cur.execute(
                f"SELECT session_id, agent_name FROM {_TOKEN_USAGE_TABLE} "
                f"WHERE session_id IN ({placeholders})",
                list(children),
            ).fetchall()
            for row in rows:
                if row and row[1]:
                    ledger_agents.setdefault(str(row[0]), str(row[1]))

    links: dict[str, dict[str, Any]] = {}
    for sid, entry in children.items():
        attribution = _child_attribution(entry, sessions)
        if attribution is None:  # pragma: no cover - the filter above guarantees it
            continue
        agent_name = entry.get("agent_name") or ledger_agents.get(sid)
        links[sid] = {
            "parent_session_id": attribution["parent_session_id"],
            "label": _agent_label(agent_name),
            "agent_name": agent_name,
            "purpose": entry.get("purpose"),
        }
    return links


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


def background_provenance(
    source_session_id: str,
    agent_name: str | None,
    parent_session_id: str | None,
    purpose: str | None,
) -> str:
    """Return the provenance recorded with a background-task row.

    It names the session the work really came from, so the child session
    stays recoverable from the stored row even though the row is filed under
    the parent. The blob is kept to four short keys — it is materialized
    whenever a wide window is scanned.

    The repair script writes the same blob, through this helper.
    """
    provenance: dict[str, str] = {"source_session_id": source_session_id}
    if agent_name:
        provenance["agent_name"] = agent_name
    if parent_session_id:
        provenance["parent_session_id"] = parent_session_id
    if purpose:
        provenance["purpose"] = purpose
    return json.dumps(provenance)


def build_event_id(
    session_id: str,
    turn_id: str,
    ts_ms: int,
    tokens_input: int,
    tokens_output: int,
    tokens_cache_read: int,
) -> str:
    """Return the event id of one ledger request.

    Derived from the **ledger row** alone — never from the session the event
    is filed under. A background-task row is filed under its *parent* session,
    but its id keeps the child's own session id: the id must not change when
    the attribution changes, or every row collected before the change would
    look new to the server and be counted twice.

    The repair script (``scripts/link_minimax_background_sessions.py``) builds
    the same ids with this helper, so the two cannot drift apart.
    """
    return (
        f"mcode:{session_id}:{turn_id}:{ts_ms}:{tokens_input}:{tokens_output}:{tokens_cache_read}"
    )


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
